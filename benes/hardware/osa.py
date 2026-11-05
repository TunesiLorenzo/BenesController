"""Trace download from a VIAVI OSA 500RS, shared with the VoltageTuner app."""

import math
import socket
import statistics
import time
from dataclasses import dataclass


C = 299792458
SPURIOUS_TAIL_POINTS = 2


def find_laser_wavelengths(wavelength, power, count=6, min_separation_nm=0.1,
                           min_prominence_db=3.0, wavelength_range_nm=None):
    """Return the ``count`` strongest distinct OSA peaks, high wavelength first.

    The six-laser calibration needs the physical wavelength ordering, rather
    than a pre-entered WDM plan. A laser line is a local maximum which rises
    above its nearby baseline by ``min_prominence_db``. The separation rule
    keeps neighbouring OSA bins belonging to one line from being reported as
    multiple channels.
    """
    if len(wavelength) != len(power):
        raise ValueError("The OSA returned mismatched wavelength and power arrays")
    if not isinstance(count, int) or count < 1:
        raise ValueError("The number of laser channels must be a positive integer")
    if not math.isfinite(min_separation_nm) or min_separation_nm <= 0:
        raise ValueError("The minimum peak separation must be positive")
    if not math.isfinite(min_prominence_db) or min_prominence_db < 0:
        raise ValueError("The minimum peak prominence cannot be negative")
    if wavelength_range_nm is not None:
        lower_nm, upper_nm = wavelength_range_nm
        if not (math.isfinite(lower_nm) and math.isfinite(upper_nm) and lower_nm < upper_nm):
            raise ValueError("The laser wavelength range must have finite increasing limits")

    samples = []
    for item_wavelength, item_power in zip(wavelength, power):
        try:
            item_wavelength, item_power = float(item_wavelength), float(item_power)
        except (TypeError, ValueError) as error:
            raise ValueError("The OSA trace contains a non-numeric value") from error
        if math.isfinite(item_wavelength) and math.isfinite(item_power):
            samples.append((item_wavelength, item_power))
    samples.sort()
    if len(samples) < 3:
        raise ValueError("The OSA trace is too short to find laser channels")

    steps = [right[0] - left[0] for left, right in zip(samples, samples[1:])
             if right[0] > left[0]]
    if not steps:
        raise ValueError("The OSA trace does not contain distinct wavelength bins")
    window = max(1, math.ceil(min_separation_nm / statistics.median(steps)))

    candidates = []
    for index in range(1, len(samples) - 1):
        wavelength_nm, power_dbm = samples[index]
        if wavelength_range_nm is not None and not lower_nm <= wavelength_nm <= upper_nm:
            continue
        if power_dbm < samples[index - 1][1] or power_dbm < samples[index + 1][1]:
            continue
        # At least one strict neighbour prevents a flat portion of the noise
        # floor from becoming a channel.
        if power_dbm == samples[index - 1][1] == samples[index + 1][1]:
            continue
        nearby = [value for _, value in samples[max(0, index - window):index]
                  + samples[index + 1:min(len(samples), index + window + 1)]]
        if nearby and power_dbm - min(nearby) >= min_prominence_db:
            candidates.append((power_dbm, wavelength_nm))

    selected = []
    for power_dbm, wavelength_nm in sorted(candidates, reverse=True):
        if all(abs(wavelength_nm - existing) >= min_separation_nm for existing in selected):
            selected.append(wavelength_nm)
            if len(selected) == count:
                break
    if len(selected) != count:
        raise ValueError(
            f"Found only {len(selected)} of {count} distinct laser peaks. Ensure all six lasers are visible "
            "in the OSA trace and their peaks are at least 3 dB above the local baseline."
            + ("" if wavelength_range_nm is None else f" Search range: {lower_nm:g}–{upper_nm:g} nm.")
        )
    return sorted(selected, reverse=True)


def distinct_laser_wavelengths(wavelengths, count=6, min_separation_nm=0.1,
                               wavelength_range_nm=None):
    """Validate and order peak positions collected from multiple OSA outputs."""
    if not math.isfinite(min_separation_nm) or min_separation_nm <= 0:
        raise ValueError("The minimum peak separation must be positive")
    if wavelength_range_nm is not None:
        lower_nm, upper_nm = wavelength_range_nm
        if not (math.isfinite(lower_nm) and math.isfinite(upper_nm) and lower_nm < upper_nm):
            raise ValueError("The laser wavelength range must have finite increasing limits")
    values = []
    for value in wavelengths:
        try:
            value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("A detected laser wavelength is not numeric") from error
        if math.isfinite(value) and (wavelength_range_nm is None or lower_nm <= value <= upper_nm):
            values.append(value)
    selected = []
    for value in sorted(values, reverse=True):
        if all(abs(value - existing) >= min_separation_nm for existing in selected):
            selected.append(value)
            if len(selected) == count:
                return selected
    raise ValueError(
        f"Found only {len(selected)} of {count} distinct laser peaks across the OSA outputs. "
        "Check the laser lines and OSA/JDSU connections."
    )


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


class OSASession:
    """One remote-control connection: LF-terminated ASCII commands, one reply line per query."""

    def __init__(self, host, port, timeout=10):
        self.timeout = timeout
        self.connection = socket.create_connection((host, port), timeout=timeout)
        self.stream = self.connection.makefile("rwb")
        self.write("*REM")
        self.identity = self.query("*IDN?")

    def write(self, command):
        self.stream.write(command.encode("ascii") + b"\n")
        self.stream.flush()

    def query(self, command):
        self.write(command)
        reply = self.stream.readline()
        if not reply.endswith(b"\n"):
            raise OSError(f"OSA closed the connection during {command}")
        return reply.decode("ascii").strip()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def single_mode(session) -> bool:
    """``OSASetup:MODe 1``: each ``KEY START`` runs one acquisition, then the OSA stops."""
    return session.query("OSASetup:MODe?").strip() == "1"


def wait_until_stopped(session, max_wait_s, clock=time.monotonic):
    """``STAT:WEAcq?`` until the running acquisition ends, in calls shorter than the socket timeout."""
    deadline = clock() + max_wait_s
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError(f"The OSA acquisition did not finish within {max_wait_s:g} s")
        chunk = max(1, min(int(remaining), int(session.timeout) - 1))
        if session.query(f"STAT:WEAcq? {chunk}").strip() == "STOPPED":
            return


def single_acquisition(session, max_wait_s=30.0, start_wait_s=2.0, clock=time.monotonic, sleep=time.sleep):
    """Start one acquisition and return its buffer: it lies wholly after this call.

    An acquisition already running may have started before the last change, so it
    is let finish and discarded. The status, not a changed buffer, shows the new one
    ran: with no light above the floor two sweeps return identical buffers. The lab
    OSA reports IN_PROGRESS 0.1-0.25 s after ``KEY START`` and sweeps for about 2 s.
    """
    if session.query("STAT:ACQ?") != "STOPPED":
        wait_until_stopped(session, max_wait_s, clock)
    session.write("KEY START")
    deadline = clock() + start_wait_s
    while session.query("STAT:ACQ?") != "IN_PROGRESS":
        if clock() >= deadline:
            raise TimeoutError(f"The OSA did not start a single acquisition within {start_wait_s:g} s")
        sleep(0.02)
    wait_until_stopped(session, max_wait_s, clock)
    return session.query(":CUR:BUFF?")


def get_spectrum(host="192.168.0.200", port=8002, timeout=10, curve=1, fresh_sweeps=0, max_wait_s=30.0):
    """Download a trace; ``fresh_sweeps`` waits for that many sweeps to complete first.

    In single mode (``OSASetup:MODe 1``) the call starts one acquisition and waits
    for it, which is wholly fresh, so ``fresh_sweeps`` is not needed. Sweeping
    continuously, after moving the output switch use ``fresh_sweeps=2``: the first
    new sweep may have started before the move, the second lies wholly after it.
    When the continuous OSA was stopped, the sweep this call starts is already
    wholly fresh, so one is enough, and the stale buffer is never returned.
    """
    with OSASession(host, port, timeout) as session:
        session.write(f":CUR:SELECT {curve}")
        session.query(":CUR:SIZE?")
        if single_mode(session):
            buffer = single_acquisition(session, max_wait_s)
        elif session.query("STAT:ACQ?") == "STOPPED":
            # Capture the stale buffer first so the new sweep is recognised.
            stale = session.query(":CUR:BUFF?")
            session.write("KEY START")
            buffer = wait_for_fresh_trace(lambda: session.query(":CUR:BUFF?"), 1, max_wait_s, baseline=stale)
        else:
            buffer = wait_for_fresh_trace(lambda: session.query(":CUR:BUFF?"), fresh_sweeps, max_wait_s)
        raw = parse_curve(buffer)
        xoff, xscale, yoff, yscale = (float(session.query(f":CUR:{name}?"))
                                      for name in ("XOFF", "XSCale", "YOFF", "YSCale"))
    return scale_curve(raw, xoff, xscale, yoff, yscale)


# Acquisition settings (Programming the MTS/T-BERD rev 007, ch. 5, "OSASetup").
MODES = {"Continuous": "0", "Single": "1"}
RESOLUTIONS = ("FULL", "R01nm", "R02nm", "R03nm", "R04nm", "R05nm")
SENSITIVITIES = ("AUTO", "NO", "YES")
AVERAGING = ("NO", "LOW", "MEDium", "HIGH")
# Manual start/end range; the OSA takes it in THz, start (short wavelength) above end.
MANUAL_RANGE = "SEManual"
RANGE_NM = (1260.0, 1650.0)


@dataclass
class OSASettings:
    mode: str           # a key of MODES, or the raw reply for another mode
    resolution: str
    sensitivity: str
    averaging: str
    range_preset: str
    start_nm: float     # the manual range, shortest wavelength first
    end_nm: float
    points: int


def _keyword(reply, choices):
    """Map the firmware's abbreviated reply (``AUT``, ``SEM``) back to its keyword."""
    reply = reply.strip().upper()
    for choice in choices:
        if choice.upper().startswith(reply) or reply.startswith(choice.upper()):
            return choice
    return reply


def _thz(nm):
    return C / nm / 1000


def read_settings(session) -> OSASettings:
    mode = session.query("OSASetup:MODe?").strip()
    return OSASettings(
        mode=next((name for name, value in MODES.items() if value == mode), mode),
        resolution=_keyword(session.query("OSASetup:RESO?"), RESOLUTIONS),
        sensitivity=_keyword(session.query("OSASetup:HSEns?"), SENSITIVITIES),
        averaging=_keyword(session.query("OSASetup:AVG?"), AVERAGING),
        range_preset=_keyword(session.query("OSASetup:SEAcq?"), (MANUAL_RANGE,)),
        start_nm=_thz(float(session.query("OSASetup:MSAcq?"))),
        end_nm=_thz(float(session.query("OSASetup:MEAcq?"))),
        points=int(session.query(":CUR:SIZE?")))


def validate_settings(mode, resolution, sensitivity, averaging, start_nm, end_nm):
    if mode not in MODES:
        raise ValueError(f"OSA acquisition must be one of {', '.join(MODES)}")
    for value, choices, name in ((resolution, RESOLUTIONS, "resolution"), (sensitivity, SENSITIVITIES, "sensitivity"),
                                 (averaging, AVERAGING, "averaging")):
        if value not in choices:
            raise ValueError(f"OSA {name} must be one of {', '.join(choices)}")
    try:
        start_nm, end_nm = float(start_nm), float(end_nm)
    except (TypeError, ValueError) as error:
        raise ValueError("Enter the OSA range start and end in nm") from error
    if not RANGE_NM[0] <= start_nm < end_nm <= RANGE_NM[1]:
        raise ValueError(f"The OSA range must run from low to high within {RANGE_NM[0]:g}–{RANGE_NM[1]:g} nm")
    if _thz(start_nm) - _thz(end_nm) < 0.1:
        raise ValueError("The OSA range must span at least 0.1 THz (about 0.8 nm at 1550 nm)")
    return mode, resolution, sensitivity, averaging, start_nm, end_nm


def apply_settings(session, mode, resolution, sensitivity, averaging, start_nm, end_nm,
                   max_wait_s=30.0, clock=time.monotonic, sleep=time.sleep) -> OSASettings:
    """Stop the acquisition, write the settings, read them back; continuous mode restarts.

    Raises if any setting reads back different, so a value the module rejects is caught.
    """
    mode, resolution, sensitivity, averaging, start_nm, end_nm = validate_settings(
        mode, resolution, sensitivity, averaging, start_nm, end_nm)
    if session.query("STAT:ACQ?") != "STOPPED":
        if single_mode(session):
            wait_until_stopped(session, max_wait_s, clock)
        else:
            session.write("KEY START")  # Toggles the running acquisition off.
            deadline = clock() + max_wait_s
            while session.query("STAT:ACQ?") != "STOPPED":
                if clock() >= deadline:
                    raise TimeoutError("The OSA did not stop its acquisition")
                sleep(0.1)
    start_thz, end_thz = round(_thz(start_nm), 3), round(_thz(end_nm), 3)
    session.write(f"OSASetup:MODe {MODES[mode]}")
    session.write(f"OSASetup:RESO {resolution}")
    session.write(f"OSASetup:HSEns {sensitivity}")
    session.write(f"OSASetup:AVG {averaging}")
    session.write(f"OSASetup:SEAcq {MANUAL_RANGE}")
    # Start must stay above end at every step, so move first whichever keeps them apart.
    if start_thz > float(session.query("OSASetup:MEAcq?")):
        session.write(f"OSASetup:MSAcq {start_thz:.3f}")
        session.write(f"OSASetup:MEAcq {end_thz:.3f}")
    else:
        session.write(f"OSASetup:MEAcq {end_thz:.3f}")
        session.write(f"OSASetup:MSAcq {start_thz:.3f}")
    settings = read_settings(session)
    wrong = [name for name, wanted, got in (
        ("acquisition", mode, settings.mode), ("resolution", resolution, settings.resolution),
        ("sensitivity", sensitivity, settings.sensitivity), ("averaging", averaging, settings.averaging),
        ("range", MANUAL_RANGE, settings.range_preset)) if wanted != got]
    if not (math.isclose(_thz(settings.start_nm), start_thz, abs_tol=0.002)
            and math.isclose(_thz(settings.end_nm), end_thz, abs_tol=0.002)):
        wrong.append("range start/end")
    if wrong:
        raise ValueError(f"The OSA did not accept the {', '.join(wrong)} setting; read back: {settings}")
    if mode == "Continuous":
        session.write("KEY START")
    return settings
