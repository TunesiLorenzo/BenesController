"""Convert Arduino BNC samples while adapting the HP 8152A fixed range."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from benes.hardware.hp8152a import HPRangeUnavailable, RANGES_DBM
from benes.hardware.power_meter import RANGE_TRACKING_OFF


# The HP 8152A BNC output clips near 86 Arduino counts, so the high limit must
# sit below that level or a saturated reading never triggers a range change.
BNC_CLIP_ADC = 86.0
DEFAULT_LOW_ADC = 7.0
# Below one count on the lowest range the reading is noise around zero.
FLOOR_MIN_ADC = 1.0
DEFAULT_HIGH_ADC = 75.0
# Allow the HP range relay, analog output, and Arduino acquisition to settle.
DEFAULT_RANGE_SETTLE_S = 0.3


@dataclass(frozen=True)
class BNCReference:
    """One steady HP dBm reading paired with the Arduino BNC ADC mean."""

    adc: float
    dbm: float
    range_dbm: int
    resource: str
    channel: int

    def __post_init__(self):
        if not math.isfinite(self.adc) or self.adc <= 0:
            raise ValueError("The BNC reference needs a positive Arduino ADC reading")
        if not math.isfinite(self.dbm) or self.range_dbm not in RANGES_DBM:
            raise ValueError("The BNC reference needs a valid HP power and range")
        if self.channel not in (1, 2):
            raise ValueError("The BNC reference needs HP channel A or B")

    def mw(self, adc: float, range_dbm: int) -> float:
        """Convert linear BNC counts using the measured reference and known HP gain."""
        if range_dbm not in RANGES_DBM or not math.isfinite(adc) or adc < 0:
            raise ValueError("BNC conversion needs a valid ADC reading and HP range")
        return (10 ** (self.dbm / 10) * adc / self.adc
                * 10 ** ((range_dbm - self.range_dbm) / 10))

    def dbm_at(self, adc: float, range_dbm: int) -> float | None:
        mw = self.mw(adc, range_dbm)
        return None if mw <= 0 else 10 * math.log10(mw)

    def record(self) -> dict:
        return {"adc": self.adc, "dbm": self.dbm, "range_dbm": self.range_dbm,
                "resource": self.resource, "channel": self.channel}


class ArduinoHPRangeSweep:
    """Keep each voltage point in the Arduino ADC window with fixed HP ranges."""

    def __init__(self, arduino, hp, channel, start_range_dbm=None,
                 low_adc=DEFAULT_LOW_ADC, high_adc=DEFAULT_HIGH_ADC,
                 range_settle_s=DEFAULT_RANGE_SETTLE_S, sleep=time.sleep,
                 bnc_reference: BNCReference | None = None):
        if not 0 < low_adc < high_adc < arduino.full_scale or high_adc <= 10 * low_adc:
            raise ValueError("HP BNC ADC limits need 0 < low < high < Arduino full scale and high > 10 x low")
        if high_adc >= BNC_CLIP_ADC:
            raise ValueError(f"HP BNC high limit must stay below {BNC_CLIP_ADC:g} ADC counts, where "
                             "the HP analog output clips; a clipped reading would never change range")
        if not math.isfinite(range_settle_s) or range_settle_s < 0:
            raise ValueError("HP range settle time must be nonnegative")
        self.arduino = arduino
        self.hp = hp
        self.channel = channel
        self.low_adc = low_adc
        self.high_adc = high_adc
        self.range_settle_s = range_settle_s
        self.sleep = sleep
        self.bnc_reference = bnc_reference
        read_head_id = getattr(hp, "read_head_id", None)
        try:
            self.head_id = read_head_id(channel) if read_head_id is not None else ""
        except Exception:
            # Older firmware may not answer IDN?; readback still guards every
            # attempted range and repeated refusals are cached below.
            self.head_id = ""
        self.minimum_range_dbm = -50 if "81521B" in self.head_id.upper() else RANGES_DBM[0]
        self.reference_range_dbm = (hp.read_range_dbm(channel) if start_range_dbm is None
                                    else start_range_dbm)
        if self.reference_range_dbm not in RANGES_DBM:
            raise ValueError("Choose a valid initial HP 8152A range")
        if self.reference_range_dbm < self.minimum_range_dbm:
            raise ValueError(f"The HP 81521B lowest fixed range is {self.minimum_range_dbm} dBm")
        self.range_dbm = self.reference_range_dbm
        self.point_records = []
        self.history = {}
        # A head can silently refuse a fixed range. Confirm the refusal twice
        # before skipping the same request for the rest of this sweep.
        self._range_refusals = {}
        self.hp.set_fixed_analog_range(channel, self.range_dbm)
        self.sleep(self.range_settle_s)

    def begin_curve(self):
        """Start a new MZI without revisiting any voltage from the previous one."""
        self.point_records = []

    def _set_range(self, range_dbm):
        self.hp.set_fixed_analog_range(self.channel, range_dbm)
        self.range_dbm = range_dbm
        self.sleep(self.range_settle_s)

    def _read_adc(self):
        # read_current_power starts a fresh acquisition, so samples buffered
        # before the HP range change cannot enter the corrected measurement.
        reading = self.arduino.read_current_power(
            ceiling_override=self.arduino.full_scale + 1,
            range_tracking_mode=RANGE_TRACKING_OFF, ignore_jumps=True)
        raw = reading.raw_mean
        if not math.isfinite(raw) or raw < 0:
            raise ValueError("Arduino returned an invalid BNC ADC value")
        return raw

    def _next_range(self, raw, range_dbm):
        if raw >= self.high_adc and range_dbm < RANGES_DBM[-1]:
            return range_dbm + 10
        if raw < self.low_adc and range_dbm > self.minimum_range_dbm:
            return range_dbm - 10
        return None

    def read(self):
        """Read one voltage, adjust range as needed, and return Arduino power."""
        initial_range = self.range_dbm
        initial_raw = None
        visited = set()
        steps = 0
        limit = None
        range_error = None
        while True:
            raw = self._read_adc()
            if initial_raw is None:
                initial_raw = raw
            if self.low_adc <= raw < self.high_adc:
                break
            target = self._next_range(raw, self.range_dbm)
            if target is None:
                limit = "high" if raw >= self.high_adc else "low"
                if limit == "low" and self.minimum_range_dbm == -50:
                    range_error = "HP 81521B lowest fixed range is -50 dBm"
                break
            if target in visited:
                limit = "unstable"
                break
            visited.add(self.range_dbm)
            range_pair = (self.range_dbm, target)
            if self._range_refusals.get(range_pair, 0) >= 2:
                range_error = f"HP previously stayed at {self.range_dbm} dBm instead of {target} dBm"
                limit = "high" if raw >= self.high_adc else "low"
                break
            try:
                self._set_range(target)
            except HPRangeUnavailable as error:
                range_error = f"HP stayed at {error.actual_range_dbm} dBm instead of {target} dBm"
                if error.actual_range_dbm == self.range_dbm:
                    self._range_refusals[range_pair] = self._range_refusals.get(range_pair, 0) + 1
                if error.actual_range_dbm != self.range_dbm:
                    # The meter moved somewhere else; measure there before deciding.
                    self.range_dbm = error.actual_range_dbm
                    self.sleep(self.range_settle_s)
                    raw = self._read_adc()
                    if self.low_adc <= raw < self.high_adc:
                        break
                limit = "high" if raw >= self.high_adc else "low"
                break
            self._range_refusals.pop(range_pair, None)
            steps += 1
        if limit in ("high", "unstable"):
            # A clipped reading only gives a lower bound, so it cannot enter the fit.
            value = None
        elif limit == "low" and raw < FLOOR_MIN_ADC:
            value = None
        elif self.bnc_reference is not None:
            value = self.bnc_reference.mw(raw, self.range_dbm)
        else:
            factor = 10 ** ((self.range_dbm - self.reference_range_dbm) // 10)
            value = raw * factor * self.arduino.settings["scale"] + self.arduino.settings["offset"]
        self.point_records.append({
            "initial_raw_adc": initial_raw, "initial_range_dbm": initial_range,
            "final_raw_adc": raw, "range_dbm": self.range_dbm,
            "range_steps": steps, "range_limit": limit, "range_error": range_error,
            "corrected_power": value,
            "measurement_source": "Arduino",
        })
        return value

    def finish_curve_points(self, probe, voltages, points):
        """Record the single pass and reject curves without enough usable points."""
        if len(self.point_records) != len(voltages):
            raise RuntimeError("Arduino sweep did not produce one reading per voltage")
        self.history[probe.name] = [
            {"voltage_v": voltage, **record}
            for voltage, record in zip(voltages, self.point_records)
        ]
        # Floor-limited readings are kept for the fit but do not make a curve usable.
        usable = sum(point.power is not None and math.isfinite(point.power)
                     and point.power > 0 and record["range_limit"] is None
                     for point, record in zip(points, self.point_records))
        if usable < 2:
            raise ValueError(
                f"{probe.name}: only {usable}/{len(voltages)} Arduino points reached the usable "
                f"{self.low_adc:g}-{self.high_adc:g} ADC window during the single sweep; "
                "increase optical signal or select a supported HP range")
        return points


# Light check before a BNC reference: consecutive HP readings must agree this closely.
LIGHT_STEADY_DB = 0.2
LIGHT_POLL_S = 0.5
LIGHT_TIMEOUT_S = 30.0
# Darkness is steady too; below this the BNC sits under the window on every range.
LIGHT_MIN_DBM = -55.0
REFERENCE_MAX_RANGE_STEPS = 6


def measure_bnc_reference(arduino, hp, channel, resource, low_adc=DEFAULT_LOW_ADC,
                          high_adc=DEFAULT_HIGH_ADC, range_settle_s=DEFAULT_RANGE_SETTLE_S,
                          light_timeout_s=LIGHT_TIMEOUT_S, min_dbm=LIGHT_MIN_DBM,
                          sleep=time.sleep) -> BNCReference:
    """Pair one HP GPIB power reading with Arduino BNC counts on a fixed HP range.

    Waits until the HP reading is steady (a laser can emit seconds after its
    command), then fixes the HP range so the BNC counts sit inside the ADC
    window. The caller restores the HP settings afterwards.
    """
    def adc():
        return arduino.read_current_power(
            ceiling_override=arduino.full_scale + 1,
            range_tracking_mode=RANGE_TRACKING_OFF, ignore_jumps=True).raw_mean

    previous = None
    waited = 0.0
    while True:
        power = hp.read_dbm_or_none()
        if (previous is not None and power is not None and power >= min_dbm
                and abs(power - previous) <= LIGHT_STEADY_DB):
            break
        if waited >= light_timeout_s:
            seen = "nothing" if power is None else f"{power:.1f} dBm"
            raise ValueError(f"No steady light above {min_dbm:g} dBm on the HP after {light_timeout_s:g} s "
                             f"(last {seen}); check the laser, the JDSU output and the chip route")
        previous = power
        sleep(LIGHT_POLL_S)
        waited += LIGHT_POLL_S

    range_dbm = hp.read_range_dbm(channel)
    hp.set_fixed_analog_range(channel, range_dbm)
    sleep(range_settle_s)
    for _ in range(REFERENCE_MAX_RANGE_STEPS):
        raw = adc()
        if low_adc <= raw < high_adc:
            break
        target = range_dbm + 10 if raw >= high_adc else range_dbm - 10
        if target not in RANGES_DBM:
            raise ValueError(f"BNC reads {raw:.1f} ADC counts even on the {range_dbm} dBm range")
        try:
            hp.set_fixed_analog_range(channel, target)
        except HPRangeUnavailable as error:
            raise ValueError(f"BNC reads {raw:.1f} ADC counts and the HP stayed at "
                             f"{error.actual_range_dbm} dBm instead of {target} dBm") from error
        range_dbm = target
        sleep(range_settle_s)
    else:
        raise ValueError("The BNC counts never settled inside the ADC window; hold the light steady")

    first = adc()
    dbm = hp.read_dbm()
    second = adc()
    mean = (first + second) / 2
    if not low_adc <= mean < high_adc or abs(first - second) > max(3.0, 0.1 * mean):
        raise ValueError(f"Use a steady optical level between {low_adc:g} and {high_adc:g} ADC "
                         "counts, then retry")
    return BNCReference(mean, dbm, range_dbm, resource, channel)
