"""MZI data, fitting, calibration extraction, and detector adapters."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from benes.measurements.measurement_control import check_stop, select_output, wait
from benes.measurements.osa_reading import read_output_spectrum
from benes.measurements.wdm_comb import line_powers

# A probed MZI must change the reading by at least this much to be trusted.
DEFAULT_MIN_EXTINCTION_DB = 3.0
# Points within this share of the swing of the brightest count as bright.
EXTREME_TIE_FRACTION = 0.02
# Points within this many dB of the darkest count as dark.
DARK_TIE_DB = 1.0
# The fit only has to find the right fringe, so it is used while its rms stays
# below this share of the amplitude.
FIT_TRUST_FRACTION = 0.15
# The measured extreme is selected within this phase of a fitted null.
EXTREME_WINDOW_RAD = math.pi / 4
LIGHT_TOLERANCE, LIGHT_POLL_S, LIGHT_MAX_READS = 0.1, 0.2, 25


@dataclass(frozen=True)
class SweepPoint:
    voltage: float
    power: float | None
    reference: float | None = None

    @property
    def contrast_db(self) -> float | None:
        """Probed line over the other-state line, when both were measured."""
        if not self.power or not self.reference or self.power <= 0 or self.reference <= 0:
            return None
        return 10 * math.log10(self.power / self.reference)


@dataclass(frozen=True)
class MZIFit:
    """T(x) = offset + amplitude*(1 + cos(pi*x/x_pi + phase0))/2."""

    offset: float
    amplitude: float
    x_pi: float
    phase0: float
    rms: float
    # "mW" of heater power, or "V²" when the heater resistance is unknown.
    x_unit: str

    def phase(self, x):
        return math.pi * x / self.x_pi + self.phase0

    def model(self, x):
        return self.offset + self.amplitude * (1 + math.cos(self.phase(x))) / 2

    @property
    def phase_per_x(self):
        """Victim phase per unit of its own heater drive [rad/x_unit]."""
        return math.pi / self.x_pi


@dataclass(frozen=True)
class MZISweep:
    probe: object
    points: tuple[SweepPoint, ...]
    cross: SweepPoint
    bar: SweepPoint
    extinction_db: float
    fit: MZIFit | None
    unit: str

    @property
    def name(self):
        return self.probe.name


@dataclass(frozen=True)
class Reading:
    power: float | None
    reference: float | None = None


def dbm_to_mw(dbm):
    return None if dbm is None else 10 ** (dbm / 10)


def mw_to_dbm(mw):
    return None if mw is None or mw <= 0 else 10 * math.log10(mw)


class OSALineDetector:
    label, unit = "OSA · all lasers on", "mW"

    def __init__(self, switch, read_spectrum, wavelengths, half_window_nm):
        self.switch, self.read_spectrum = switch, read_spectrum
        self.wavelengths, self.half_window_nm = dict(wavelengths), half_window_nm

    @property
    def inputs(self):
        return tuple(sorted(self.wavelengths))

    @property
    def outputs(self):
        return tuple(range(6))

    @property
    def background_settle_s(self):
        return getattr(self.switch, "settings", {}).get("settle_s", 0.0)

    def begin(self):
        pass

    def prepare(self, probe):
        if probe.input not in self.wavelengths:
            raise ValueError(f"Input {probe.input + 1} has no WDM line for {probe.name}")
        select_output(self.switch, probe.output)

    def read(self, probe):
        lines = line_powers(*read_output_spectrum(self.read_spectrum, probe.output), self.wavelengths,
                            self.half_window_nm)
        return Reading(dbm_to_mw(lines.get(probe.input)), dbm_to_mw(lines.get(probe.other_input)))

    def finish(self):
        pass


class PowerMeterDetector:
    def __init__(self, read_power, lasers, unit="mW", switch=None, fixed_output=None, label="Power meter",
                 begin_curve=None, end_curve=None, rescan_points=None, finish_curve_points=None):
        if (switch is None) == (fixed_output is None):
            raise ValueError("Read the power meter through the JDSU or on one fixed output")
        self.read_power, self.lasers, self.unit = read_power, lasers, unit
        self.switch, self.fixed_output, self.label = switch, fixed_output, label
        self.begin_curve, self.end_curve = begin_curve, end_curve
        self.rescan_points, self.finish_curve_points = rescan_points, finish_curve_points

    @property
    def inputs(self):
        return tuple(range(6))

    @property
    def outputs(self):
        return tuple(range(6)) if self.switch else (self.fixed_output,)

    @property
    def background_settle_s(self):
        return max(getattr(self.lasers, "settings", {}).get("settle_s", 0.0),
                   getattr(self.switch, "settings", {}).get("settle_s", 0.0) if self.switch else 0.0)

    def begin(self):
        self.lasers.begin()

    def prepare(self, probe):
        if probe.output not in self.outputs:
            raise ValueError(f"{probe.name} needs the power meter on output {probe.output + 1}")
        if self.switch:
            select_output(self.switch, probe.output)
        self.lasers.light_only(probe.input)
        if self.begin_curve:
            self.begin_curve()

    def read(self, probe):
        return Reading(self.read_power())

    def wait_for_light(self, probe, stop_event=None, sleep=time.sleep):
        previous = None
        for _ in range(LIGHT_MAX_READS):
            check_stop(stop_event)
            power = self.read_power()
            settled = (previous is not None and power is not None and min(previous, power) > 0
                       and abs(power - previous) <= LIGHT_TOLERANCE * max(previous, power))
            if settled:
                if self.begin_curve:
                    self.begin_curve()
                return True
            previous = power
            wait(LIGHT_POLL_S, stop_event, sleep)
        if self.begin_curve:
            self.begin_curve()
        return False

    def finish(self):
        try:
            if self.end_curve:
                self.end_curve()
        finally:
            self.lasers.finish()


def linear_fit(basis, values):
    """Least-squares a, b in values ~= a + b*basis; returns (a, b, SSE)."""
    n = len(values)
    mean_x, mean_y = sum(basis) / n, sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in basis)
    if sxx <= 0:
        return mean_y, 0.0, sum((y - mean_y) ** 2 for y in values)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(basis, values)) / sxx
    intercept = mean_y - slope * mean_x
    return intercept, slope, sum((intercept + slope * x - y) ** 2 for x, y in zip(basis, values))


def fit_mzi_response(xs, powers, x_unit) -> MZIFit | None:
    """Fit a cos² response by grid search over x_pi and phase."""
    data = [(x, power) for x, power in zip(xs, powers) if power is not None and math.isfinite(power)]
    if len(data) < 4:
        return None
    xs, powers = [x for x, _ in data], [power for _, power in data]
    span = max(xs) - min(xs)
    if span <= 0 or max(powers) <= min(powers):
        return None

    def evaluate(x_pi, phase0):
        basis = [(1 + math.cos(math.pi * x / x_pi + phase0)) / 2 for x in xs]
        offset, amplitude, error = linear_fit(basis, powers)
        if amplitude < 0:
            return error, x_pi, (phase0 + math.pi) % (2 * math.pi), offset + amplitude, -amplitude
        return error, x_pi, phase0, offset, amplitude

    best = None
    for step in range(120):
        x_pi = span / 12 * (60 ** (step / 119))
        for phase_step in range(36):
            candidate = evaluate(x_pi, phase_step * math.pi / 36)
            if best is None or candidate[0] < best[0]:
                best = candidate
    for _ in range(3):
        _, x_pi, phase0 = best[:3]
        for scale in [1 + step / 200 for step in range(-12, 13)]:
            for shift in [step * math.pi / 360 for step in range(-6, 7)]:
                candidate = evaluate(x_pi * scale, phase0 + shift)
                if candidate[0] < best[0]:
                    best = candidate
    error, x_pi, phase0, offset, amplitude = best
    return MZIFit(offset, amplitude, x_pi, phase0 % (2 * math.pi), math.sqrt(error / len(powers)), x_unit)


def _extreme(points, value, highest, tie):
    target = max(map(value, points)) if highest else min(map(value, points))
    close = [point for point in points if abs(value(point) - target) <= tie(target)]
    return min(close, key=lambda point: (abs(point.voltage), -value(point) if highest else value(point)))


def fit_trusted(fit) -> bool:
    return fit is not None and fit.amplitude > 0 and fit.rms < FIT_TRUST_FRACTION * fit.amplitude


def fitted_extremes(fit, xs, points):
    """Return the measured points nearest the fit's first maximum and null."""
    measured = [(x, point) for x, point in zip(xs, points) if point.power is not None]
    start, stop = min(x for x, _ in measured), max(x for x, _ in measured)
    start_phase = fit.phase(start)
    targets = []
    for offset in (0.0, math.pi):
        turns = math.ceil((start_phase - math.pi / 2 - offset) / (2 * math.pi))
        target = (offset + 2 * math.pi * turns - fit.phase0) / fit.phase_per_x
        targets.append(min(max(target, start), stop))
    bright, null = (min(measured, key=lambda item: (abs(item[0] - target), item[0])) for target in targets)
    window = EXTREME_WINDOW_RAD / fit.phase_per_x
    near = [item for item in measured if abs(item[0] - targets[1]) <= window] or [null]
    return bright[1], min(near, key=lambda item: (item[1].power, item[0]))[1]


def extract_calibration(probe, points, xs, unit="mW", x_unit="mW",
                        min_extinction_db=DEFAULT_MIN_EXTINCTION_DB) -> MZISweep:
    """Extract cross/bar voltages from measured sweep extremes and an optional fit."""
    points = tuple(points)
    fit = fit_mzi_response(xs, [point.power for point in points], x_unit)
    with_contrast = [point for point in points if point.contrast_db is not None]
    contrast = len(with_contrast) >= 2 and len(with_contrast) == len([point for point in points if point.power is not None])
    if contrast:
        measured, value = with_contrast, lambda point: point.contrast_db
        bright_tie = dark_tie = lambda target: 0.5
        extinction = max(map(value, measured)) - min(map(value, measured))
    else:
        measured = [point for point in points if point.power is not None and math.isfinite(point.power)]
        if len(measured) < 2:
            raise ValueError(f"{probe.name}: the detector returned fewer than two readings")
        value = lambda point: point.power
        high, low = max(map(value, measured)), min(map(value, measured))
        bright_tie = lambda target: EXTREME_TIE_FRACTION * (high - low)
        dark_tie = lambda target: (target * (10 ** (DARK_TIE_DB / 10) - 1) if target > 0
                                   else EXTREME_TIE_FRACTION * (high - low))
        extinction = math.inf if low <= 0 < high else 10 * math.log10(high / low) if low > 0 else 0.0
    if extinction < min_extinction_db:
        raise ValueError(f"{probe.name}: only {extinction:.1f} dB change at output {probe.output + 1}; "
                         f"check that input {probe.input + 1} is lit and the detector is on that output")
    if fit_trusted(fit):
        bright, dark = fitted_extremes(fit, xs, points)
    else:
        bright = _extreme(measured, value, True, bright_tie)
        dark = _extreme(measured, value, False, dark_tie)
    cross, bar = (bright, dark) if probe.bright_crossed else (dark, bright)
    return MZISweep(probe, points, cross, bar, extinction, fit, unit)
