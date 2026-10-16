"""Trace download from a VIAVI OSA 500RS, shared with the VoltageTuner app."""

import math
import socket


C = 299792458
SPURIOUS_TAIL_POINTS = 2


def trim_spurious_tail(wavelength, power, count=SPURIOUS_TAIL_POINTS):
    """Discard the final OSA bins before plotting or calculating power."""
    if len(wavelength) != len(power):
        raise ValueError("The OSA returned mismatched wavelength and power arrays")
    if count < 0 or len(power) <= count:
        raise ValueError("The OSA trace is too short to discard its final bins")
    if count == 0:
        return list(wavelength), list(power)
    return list(wavelength[:-count]), list(power[:-count])


def parse_curve(buffer):
    buffer = buffer.strip()
    if not buffer.startswith("#"):
        raise ValueError("Unexpected OSA curve header")
    digits = int(buffer[1])
    length = int(buffer[2:2 + digits])
    data = buffer[2 + digits:]
    if len(data) < length:
        raise ValueError(f"Incomplete OSA curve: {len(data)} of {length} bytes received")
    values = [int(data[index:index + 4], 16) for index in range(0, length - length % 4, 4)]
    return [value - 65536 if value >= 32768 else value for value in values]


def scale_curve(raw, xoff, xscale, yoff, yscale):
    return ([C / (xoff + index * xscale) / 1000 for index in range(len(raw))],
            [yoff + yscale * value for value in raw])


def simulated_spectrum(points=4001):
    xoff, xscale = 181.692, (239.834 - 181.692) / (points - 1)
    raw = [round(-6000 + 4500 * math.exp(-((index - points * 0.2) / 40) ** 2)) for index in range(points)]
    return scale_curve(raw, xoff, xscale, 0.0, 0.01)


def get_spectrum(host="192.168.0.200", port=8002, timeout=10, curve=1):
    with socket.create_connection((host, port), timeout=timeout) as connection:
        stream = connection.makefile("rwb")

        def write(command):
            stream.write(command.encode("ascii") + b"\n")
            stream.flush()

        def query(command):
            write(command)
            reply = stream.readline()
            if not reply.endswith(b"\n"):
                raise OSError(f"OSA closed the connection during {command}")
            return reply.decode("ascii").strip()

        write("*REM")
        query("*IDN?")
        if query("STAT:ACQ?") == "STOPPED":
            write("KEY START")
        write(f":CUR:SELECT {curve}")
        query(":CUR:SIZE?")
        raw = parse_curve(query(":CUR:BUFF?"))
        xoff, xscale, yoff, yscale = (float(query(f":CUR:{name}?")) for name in ("XOFF", "XSCale", "YOFF", "YSCale"))
    return scale_curve(raw, xoff, xscale, yoff, yscale)
