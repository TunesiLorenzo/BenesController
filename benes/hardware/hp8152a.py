"""HP 8152A optical average power meter over its legacy HP-IB command set."""

from __future__ import annotations

import math
import threading
import time


DEFAULT_RESOURCE = "GPIB0::23::INSTR"
RANGES_DBM = tuple(range(-90, 31, 10))


class HPRangeUnavailable(RuntimeError):
    """The HP range readback did not match a requested fixed range."""

    def __init__(self, requested_range_dbm, actual_range_dbm):
        self.requested_range_dbm = requested_range_dbm
        self.actual_range_dbm = actual_range_dbm
        super().__init__(f"HP 8152A did not select {requested_range_dbm} dBm; "
                         f"it reports {actual_range_dbm} dBm")


def validate_stream_rate(value):
    """Requested GPIB read cadence in Hz; actual throughput is instrument-dependent."""
    try:
        rate = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Enter an HP meter read rate in Hz") from error
    if not math.isfinite(rate) or not 0.1 <= rate <= 100:
        raise ValueError("HP meter read rate must be between 0.1 and 100 Hz")
    return rate


class HP8152A:
    """One-channel dBm readings with autoranging or a selected fixed range."""

    def __init__(self, resource=DEFAULT_RESOURCE, resource_manager_factory=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.resource = str(resource).strip()
        self._factory = resource_manager_factory
        self._clock = clock
        self._sleep = sleep
        self.manager = None
        self.instrument = None
        self.channel = 1
        self.wavelength_nm = 1550
        self.range_dbm = None
        self._previous = {}
        self._configured = False
        self._io_lock = threading.RLock()

    @property
    def connected(self):
        return self.instrument is not None

    def connect(self, timeout_s=10):
        if not self.resource:
            raise ValueError("Enter the HP 8152A VISA resource")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("HP 8152A timeout must be positive")
        if self.connected:
            return
        if self._factory is None:
            try:
                import pyvisa
            except ImportError as error:
                raise RuntimeError("Install PyVISA and NI-VISA to use the HP 8152A") from error
            factory = pyvisa.ResourceManager
        else:
            factory = self._factory
        manager = factory()
        instrument = None
        try:
            instrument = manager.open_resource(self.resource)
            instrument.timeout = round(timeout_s * 1000)
            instrument.write_termination = "\n"
            # GPIB EOI ends reads; the legacy meter need not send a newline.
            instrument.read_termination = None
        except Exception:
            if instrument is not None:
                instrument.close()
            raise
        self.manager, self.instrument = manager, instrument
        for command in ("M?", "CH?", "U?", "T?", "AR?", "RNG? 1", "RNG? 2", "WVL? 1", "WVL? 2"):
            try:
                self._previous[command] = instrument.query(command).strip()
            except Exception:
                # Some heads/firmware reject channel-specific queries.
                pass

    def configure(self, channel=1, wavelength_nm=1550, range_dbm=None):
        with self._io_lock:
            return self._configure_unlocked(channel, wavelength_nm, range_dbm)

    def _configure_unlocked(self, channel=1, wavelength_nm=1550, range_dbm=None):
        if not self.connected:
            raise RuntimeError("Connect the HP 8152A before configuring it")
        channel = int(channel)
        wavelength_nm = float(wavelength_nm)
        if channel not in (1, 2):
            raise ValueError("HP 8152A channel must be A or B")
        if not math.isfinite(wavelength_nm) or not 400 <= wavelength_nm <= 1700:
            raise ValueError("HP 8152A wavelength must be between 400 and 1700 nm")
        if range_dbm is not None:
            range_dbm = int(range_dbm)
            if range_dbm not in RANGES_DBM:
                raise ValueError("Choose a fixed HP 8152A range from -90 to +30 dBm in 10 dB steps")
        self.channel, self.wavelength_nm, self.range_dbm = channel, wavelength_nm, range_dbm
        self._configured = True
        self.instrument.write(f"M2;CH{channel};U0;T1;WVL {channel},{wavelength_nm:g}NM")
        if range_dbm is None:
            self.instrument.write("AR1")
        else:
            self.instrument.write(f"CH{channel};AR0;RNG {channel},{range_dbm}DBM")

    def read_range_dbm(self, channel=1):
        with self._io_lock:
            return self._read_range_dbm_unlocked(channel)

    def read_head_id(self, channel=1):
        """Identify the connected head so its fixed-range limits can be used."""
        with self._io_lock:
            if not self.connected:
                raise RuntimeError("Connect the HP 8152A before reading its head ID")
            if channel not in (1, 2):
                raise ValueError("HP 8152A channel must be A or B")
            return self.instrument.query(f"IDN?{channel}").strip()

    def _read_range_dbm_unlocked(self, channel=1):
        """Read the active range without triggering an optical measurement."""
        if not self.connected:
            raise RuntimeError("Connect the HP 8152A before reading its range")
        if channel not in (1, 2):
            raise ValueError("HP 8152A channel must be A or B")
        response = self.instrument.query(f"RNG? {channel}").strip()
        try:
            value = float(response)
        except ValueError as error:
            raise ValueError(f"HP 8152A returned an invalid range: {response!r}") from error
        if not math.isfinite(value) or int(round(value)) not in RANGES_DBM or abs(value - round(value)) > 0.01:
            raise ValueError(f"HP 8152A returned an unsupported range: {response!r}")
        return int(round(value))

    def set_fixed_analog_range(self, channel, range_dbm):
        with self._io_lock:
            return self._set_fixed_analog_range_unlocked(channel, range_dbm)

    def _set_fixed_analog_range_unlocked(self, channel, range_dbm):
        """Disable autoranging and set the range used by the analog BNC."""
        if not self.connected:
            raise RuntimeError("Connect the HP 8152A before setting its range")
        if channel not in (1, 2) or range_dbm not in RANGES_DBM:
            raise ValueError("Choose a valid HP 8152A channel and 10 dB range")
        self.channel = channel
        self._configured = True
        self.instrument.write(f"CH{channel};AR0;RNG {channel},{range_dbm}DBM")
        deadline = self._clock() + 2.0
        while True:
            actual = self.read_range_dbm(channel)
            if actual == range_dbm:
                self.range_dbm = range_dbm
                return
            if self._clock() >= deadline:
                self.range_dbm = actual
                raise HPRangeUnavailable(range_dbm, actual)
            self._sleep(0.02)

    def diagnose_fixed_range(self, channel, range_dbm=-60):
        """Try one fixed range over GPIB, report the head/error, then restore settings."""
        with self._io_lock:
            if not self.connected:
                raise RuntimeError("Connect the HP 8152A before testing its range")
            if channel not in (1, 2) or range_dbm not in RANGES_DBM:
                raise ValueError("Choose a valid HP channel and fixed range")
            instrument = self.instrument
            old_timeout = instrument.timeout
            old_channel_setting, old_range_setting = self.channel, self.range_dbm
            instrument.timeout = min(old_timeout, 2000)

            def optional_query(command):
                try:
                    return instrument.query(command).strip()
                except Exception as error:
                    return f"unavailable ({type(error).__name__})"

            try:
                selected_before = int(instrument.query("CH?").strip())
                autorange_before = instrument.query("AR?").strip()
                range_before = self._read_range_dbm_unlocked(channel)
                head_id = optional_query(f"IDN?{channel}")
                error_before = optional_query("ERR?")
                if selected_before != channel:
                    instrument.write(f"CH{channel}")
                    try:
                        target_autorange_before = instrument.query("AR?").strip()
                    finally:
                        instrument.write(f"CH{selected_before}")
                else:
                    target_autorange_before = autorange_before
                try:
                    # Send distinct writes to distinguish a rejected lower range
                    # from an unprocessed command in the combined scan write.
                    instrument.write(f"CH{channel}")
                    instrument.write("AR0")
                    steps = []
                    targets = (list(range(range_before - 10, range_dbm - 1, -10))
                               if range_dbm < range_before else [range_dbm])
                    for target in targets:
                        instrument.write(f"RNG {channel},{target}DBM")
                        deadline = self._clock() + 2.0
                        while True:
                            actual = self._read_range_dbm_unlocked(channel)
                            if actual == target or self._clock() >= deadline:
                                break
                            self._sleep(0.02)
                        steps.append({"requested_dbm": target, "actual_dbm": actual,
                                      "error": optional_query("ERR?")})
                        if actual != target:
                            break
                    return {
                        "head_id": head_id, "selected_before": selected_before,
                        "autorange_before": autorange_before, "range_before_dbm": range_before,
                        "requested_dbm": range_dbm,
                        "actual_dbm": self._read_range_dbm_unlocked(channel),
                        "autorange_after": instrument.query("AR?").strip(),
                        "error_before": error_before, "error_after": optional_query("ERR?"),
                        "steps": steps,
                    }
                finally:
                    # Restore the target range/autoranging and the selected channel.
                    try:
                        instrument.write(f"CH{channel};AR0;RNG {channel},{range_before}DBM;"
                                         f"AR{target_autorange_before}")
                    finally:
                        if selected_before != channel:
                            instrument.write(f"CH{selected_before};AR{autorange_before}")
            finally:
                instrument.timeout = old_timeout
                self.channel, self.range_dbm = old_channel_setting, old_range_setting

    def read_dbm(self, timeout_s=10):
        power = self.read_dbm_or_none(timeout_s)
        if power is None:
            raise ValueError("HP 8152A reports an invalid or out-of-range reading; check the optical input and range")
        return power

    def read_dbm_or_none(self, timeout_s=10):
        """Return None for the meter's out-of-range sentinel, otherwise dBm."""
        power = self._read_value(timeout_s)
        return None if power <= -900 else power

    def _read_value(self, timeout_s=10):
        with self._io_lock:
            return self._read_value_unlocked(timeout_s)

    def _read_value_unlocked(self, timeout_s=10):
        if not self.connected:
            raise RuntimeError("Connect the HP 8152A before reading power")
        deadline = self._clock() + timeout_s
        self.instrument.write("CSB")
        self.instrument.write("TRG")
        while not self.instrument.read_stb() & 4:
            if self._clock() >= deadline:
                raise TimeoutError("HP 8152A did not finish its triggered measurement")
            self._sleep(0.005)
        response = self.instrument.read().strip()
        try:
            power = float(response)
        except ValueError as error:
            raise ValueError(f"HP 8152A returned an invalid power reading: {response!r}") from error
        if not math.isfinite(power):
            raise ValueError("HP 8152A returned a non-finite power reading")
        return power

    def sample_rate(self, count=12):
        """Measure the actual triggered GPIB read rate, including invalid values."""
        if not 2 <= count <= 100:
            raise ValueError("Sample-rate check requires 2 to 100 readings")
        start = self._clock()
        values = [self._read_value() for _ in range(count)]
        elapsed = self._clock() - start
        return {"samples": count, "elapsed_s": elapsed, "rate_hz": count / elapsed,
                "valid_values_dbm": [value for value in values if value > -900]}

    def disconnect(self):
        with self._io_lock:
            self._disconnect_unlocked()

    def _disconnect_unlocked(self):
        try:
            if self.instrument is not None:
                try:
                    if self._configured:
                        self._restore_settings()
                finally:
                    self.instrument.close()
        finally:
            # PyVISA shares a ResourceManager between callers using the same
            # VISA library. Closing it here also closes laser and switch sessions.
            self.instrument = self.manager = None
            self._previous = {}
            self._configured = False

    def _restore_settings(self):
        old = self._previous
        if not old:
            return
        commands = []
        for channel in (self.channel,):
            wavelength = old.get(f"WVL? {channel}")
            if wavelength:
                commands.append(f"WVL {channel},{float(wavelength) * 1e9:g}NM")
            range_dbm = old.get(f"RNG? {channel}")
            if range_dbm:
                commands.append(f"RNG {channel},{float(range_dbm):g}DBM")
        for query, mnemonic in (("M?", "M"), ("CH?", "CH"), ("U?", "U"),
                                ("AR?", "AR"), ("T?", "T")):
            if query in old:
                commands.append(f"{mnemonic}{old[query]}")
        if commands:
            self.instrument.write(";".join(commands))
