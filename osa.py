"""Trace download from a VIAVI OSA 500RS, shared with the VoltageTuner app."""

import math
import socket
import time


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


def wait_for_fresh_trace(fetch, needed, max_wait_s=30.0, baseline=None, poll_s=0.1,
                         clock=time.monotonic, sleep=time.sleep):
    """Return the curve buffer once it has been republished ``needed`` times.

    The OSA replaces the whole buffer when a sweep completes (every 1.5–3 s on
    the lab instrument), so a changed buffer marks one finished sweep.
    ``baseline`` is a buffer read earlier, e.g. just before starting a sweep.
    """
    buffer = fetch() if baseline is None else baseline
    deadline = clock() + max_wait_s
    while needed > 0:
        if clock() >= deadline:
            raise TimeoutError(f"The OSA trace did not refresh within {max_wait_s:g} s; check that it is sweeping")
        sleep(poll_s)
        latest = fetch()
        if latest != buffer:
            buffer = latest
            needed -= 1
    return buffer


def get_spectrum(host="192.168.0.200", port=8002, timeout=10, curve=1, fresh_sweeps=0, max_wait_s=30.0):
    """Download a trace; ``fresh_sweeps`` waits for that many sweeps to complete first.

    After moving the output switch use ``fresh_sweeps=2``: the first new sweep
    may have started before the move, the second lies wholly after it.  When the
    OSA was stopped, the sweep this call starts is already wholly fresh, so one
    is enough, and the stale buffer from before the start is never returned.
    """
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
        write(f":CUR:SELECT {curve}")
        query(":CUR:SIZE?")
        started = query("STAT:ACQ?") == "STOPPED"
        if started:
            # Capture the stale buffer first so the new sweep is recognised.
            stale = query(":CUR:BUFF?")
            write("KEY START")
            buffer = wait_for_fresh_trace(lambda: query(":CUR:BUFF?"), 1, max_wait_s, baseline=stale)
        else:
            buffer = wait_for_fresh_trace(lambda: query(":CUR:BUFF?"), fresh_sweeps, max_wait_s)
        raw = parse_curve(buffer)
        xoff, xscale, yoff, yscale = (float(query(f":CUR:{name}?")) for name in ("XOFF", "XSCale", "YOFF", "YSCale"))
    return scale_curve(raw, xoff, xscale, yoff, yscale)
