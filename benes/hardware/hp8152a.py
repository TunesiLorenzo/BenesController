"""HP 8152A optical average power meter over its legacy HP-IB command set."""

from __future__ import annotations

import math
import time


DEFAULT_RESOURCE = "GPIB0::23::INSTR"
RANGES_DBM = tuple(range(-90, 31, 10))


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
        try:
            instrument = manager.open_resource(self.resource)
            instrument.timeout = round(timeout_s * 1000)
            instrument.write_termination = "\n"
            # GPIB EOI ends reads; the legacy meter need not send a newline.
            instrument.read_termination = None
        except Exception:
            manager.close()
            raise
        self.manager, self.instrument = manager, instrument
        for command in ("M?", "CH?", "U?", "T?", "AR?", "RNG? 1", "RNG? 2", "WVL? 1", "WVL? 2"):
            try:
                self._previous[command] = instrument.query(command).strip()
            except Exception:
                # Some heads/firmware reject channel-specific queries.
                pass

    def configure(self, channel=1, wavelength_nm=1550, range_dbm=None):
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
            self.instrument.write(f"AR0;RNG {channel},{range_dbm}DBM")

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
        try:
            if self.instrument is not None:
                try:
                    if self._configured:
                        self._restore_settings()
                finally:
                    self.instrument.close()
        finally:
            if self.manager is not None:
                self.manager.close()
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
