"""MZI calibration and thermal crosstalk with either detection scheme.

Two detectors read a probe (see ``mzi_probing``):

* ``OSALineDetector``: every input carries its own WDM line; the JDSU picks
  the output and the OSA measures the probed input's line there, with the
  other-state input's line as a reference.
* ``PowerMeterDetector``: the Newport lasers are lit one at a time, so the
  power meter's reading belongs to the probed input alone. The meter sits on
  the JDSU common port (any output) or on one fixed output.

Both return linear power, so the same sweeps, fits and crosstalk analysis
serve both.

Calibration sweeps each MZI with the others in the probe's background and
saves the bright and dark extremes as its cross/bar voltages.

Crosstalk biases a victim MZI at quadrature, found from a fit of its own
sweep, then sweeps each aggressor heater that is off the lit path. The
aggressor also warms the other MZIs on the lit path; a reference sweep with
the victim at its bright extreme, where it is insensitive to phase, measures
that path change, and the quadrature sweep is divided by it. The victim's
corrected power is turned into a phase shift with the fit; its slope against
aggressor heater power, divided by the victim's own phase per heater power,
is the crosstalk ratio kappa: the fraction of the aggressor's heater power
the victim effectively feels.
"""

from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from benes.hardware.osa_output_switch import fiber_number
from benes.measurements.laser_calibration import (CalibrationStopped, _read, _select, _wait, line_powers,
                                                  reset_all)
from benes.measurements.mzi_probing import Probe

CROSSTALK_SUBFOLDER = "Crosstalk_matrix"
# A probed MZI must change the reading by at least this much to be trusted.
DEFAULT_MIN_EXTINCTION_DB = 3.0
# Points within this share of the swing of an extreme count as that extreme.
EXTREME_TIE_FRACTION = 0.02
# Pretest: the aggressor alternates between its sweep limits in low-high-high-low
# blocks, so a linear drift of the reading cancels from the high-low difference.
SCREEN_PATTERN = (False, True, True, False) * 2
# A pretest step must exceed this many standard errors of the readings to count.
SCREEN_SIGMA = 4.0
DEFAULT_SCREEN_MIN_PHASE_RAD = 0.02
# Before a power-meter curve starts, the first point is re-read until two
# readings agree: a laser can take seconds past its command to emit.
LIGHT_TOLERANCE = 0.1
LIGHT_POLL_S = 0.2
LIGHT_MAX_READS = 25
# A crosstalk curve is not taken as thermal when light through the aggressor moves it:
# the quadrature readout folds back as a shift nears ±π/2,
READOUT_LIMIT_RAD = 1.3
# heat moves the victim one way, so a curve may not turn back by more than this,
PATH_REVERSAL_RAD = 0.5
# and leaked light follows the aggressor's own cos(π·x/x_pi) switching. Such a term
# must reach this amplitude and leave at most this share of a straight line's residual.
PATH_MIN_AMPLITUDE_RAD = 0.15
PATH_RESIDUAL_RATIO = 0.4


def dbm_to_mw(dbm):
    return None if dbm is None else 10 ** (dbm / 10)


def mw_to_dbm(mw):
    return None if mw is None or mw <= 0 else 10 * math.log10(mw)


@dataclass(frozen=True)
class Reading:
    # Linear power in the detector's unit; None when the detector had no value.
    power: float | None
    # The other-state input's line at the same output (OSA only).
    reference: float | None = None


class OSALineDetector:
    """All lasers on; the OSA reads the probed input's WDM line at the probed output."""

    label = "OSA · all lasers on"
    unit = "mW"

    def __init__(self, switch, read_spectrum, wavelengths, half_window_nm):
        self.switch = switch
        self.read_spectrum = read_spectrum
        self.wavelengths = dict(wavelengths)
        self.half_window_nm = half_window_nm

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
        _select(self.switch, probe.output)

    def read(self, probe) -> Reading:
        lines = line_powers(*_read(self.read_spectrum, probe.output), self.wavelengths, self.half_window_nm)
        return Reading(dbm_to_mw(lines.get(probe.input)), dbm_to_mw(lines.get(probe.other_input)))

    def finish(self):
        pass


class PowerMeterDetector:
    """One Newport laser lit at a time; a power meter reads one output."""

    def __init__(self, read_power, lasers, unit="mW", switch=None, fixed_output=None, label="Power meter",
                 begin_curve=None, end_curve=None, rescan_points=None, finish_curve_points=None):
        if (switch is None) == (fixed_output is None):
            raise ValueError("Read the power meter through the JDSU or on one fixed output")
        self.read_power = read_power
        self.lasers = lasers
        self.unit = unit
        self.switch = switch
        self.fixed_output = fixed_output
        self.label = label
        self.begin_curve = begin_curve
        self.end_curve = end_curve
        self.rescan_points = rescan_points
        self.finish_curve_points = finish_curve_points

    @property
    def inputs(self):
        return tuple(range(6))

    @property
    def outputs(self):
        return tuple(range(6)) if self.switch is not None else (self.fixed_output,)

    @property
    def background_settle_s(self):
        laser_wait = getattr(self.lasers, "settings", {}).get("settle_s", 0.0)
        switch_wait = (getattr(self.switch, "settings", {}).get("settle_s", 0.0)
                       if self.switch is not None else 0.0)
        return max(laser_wait, switch_wait)

    def begin(self):
        self.lasers.begin()

    def prepare(self, probe):
        if probe.output not in self.outputs:
            raise ValueError(f"{probe.name} needs the power meter on output {probe.output + 1}")
        if self.switch is not None:
            _select(self.switch, probe.output)
        self.lasers.light_only(probe.input)
        if self.begin_curve is not None:
            self.begin_curve()

    def read(self, probe) -> Reading:
        return Reading(self.read_power())

    def wait_for_light(self, probe, stop_event=None, sleep=time.sleep) -> bool:
        """Re-read the first point until two readings agree within ``LIGHT_TOLERANCE``.

        Returns False if the reading never settled; the curve then starts anyway.
        """
        previous = None
        stable = False
        for _ in range(LIGHT_MAX_READS):
            _check_stop(stop_event)
            power = self.read_power()
            if (previous is not None and power is not None and min(previous, power) > 0
                    and abs(power - previous) <= LIGHT_TOLERANCE * max(previous, power)):
                stable = True
                break
            previous = power
            _wait(LIGHT_POLL_S, stop_event, sleep)
        if self.begin_curve is not None:
            # The warm-up readings are not sweep points.
            self.begin_curve()
        return stable

    def finish(self):
        try:
            if self.end_curve is not None:
                self.end_curve()
        finally:
            self.lasers.finish()


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
    """T(x) = offset + amplitude·(1 + cos(π·x/x_pi + phase0))/2 against heater drive x."""

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


def heater_drive(controller, name, voltage) -> tuple[float, str]:
    """Heater power in mW when the resistances are known, otherwise V²."""
    power = controller.heater_power_mw(name, voltage) if hasattr(controller, "heater_power_mw") else None
    return (power, "mW") if power is not None else (voltage * voltage, "V²")


def _linear_fit(basis, values):
    """Least-squares a, b in values ≈ a + b·basis; returns (a, b, sum of squares)."""
    n = len(values)
    mean_x, mean_y = sum(basis) / n, sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in basis)
    if sxx <= 0:
        return mean_y, 0.0, sum((y - mean_y) ** 2 for y in values)
    b = sum((x - mean_x) * (y - mean_y) for x, y in zip(basis, values)) / sxx
    a = mean_y - b * mean_x
    return a, b, sum((a + b * x - y) ** 2 for x, y in zip(basis, values))


def fit_mzi_response(xs, powers, x_unit) -> MZIFit | None:
    """Fit a cos² response by grid search over x_pi and phase, then a finer grid.

    The phase grows linearly with heater power. Returns None with fewer than
    four usable points or no swing.
    """
    data = [(x, p) for x, p in zip(xs, powers) if p is not None and math.isfinite(p)]
    if len(data) < 4:
        return None
    xs, ps = [x for x, _ in data], [p for _, p in data]
    span = max(xs) - min(xs)
    if span <= 0 or max(ps) <= min(ps):
        return None

    def evaluate(x_pi, phase0):
        basis = [(1 + math.cos(math.pi * x / x_pi + phase0)) / 2 for x in xs]
        offset, amplitude, error = _linear_fit(basis, ps)
        if amplitude < 0:
            # The same curve with the phase shifted by π and a positive amplitude.
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
        for scale in [1 + k / 200 for k in range(-12, 13)]:
            for shift in [k * math.pi / 360 for k in range(-6, 7)]:
                candidate = evaluate(x_pi * scale, phase0 + shift)
                if candidate[0] < best[0]:
                    best = candidate
    error, x_pi, phase0, offset, amplitude = best
    return MZIFit(offset, amplitude, x_pi, phase0 % (2 * math.pi), math.sqrt(error / len(ps)), x_unit)


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


def _extreme(points, value, highest, tie):
    target = max(map(value, points)) if highest else min(map(value, points))
    close = [point for point in points if abs(value(point) - target) <= tie]
    return min(close, key=lambda point: (abs(point.voltage), -value(point) if highest else value(point)))


def fitted_extremes(fit, xs, points):
    """The measured points nearest the fit's first maximum and first null.

    Counting from the sweep start, an extreme already passed by less than
    π/2 is still the first one; the start point is then the best reachable.
    """
    measured = [(x, point) for x, point in zip(xs, points) if point.power is not None]
    start, stop = min(x for x, _ in measured), max(x for x, _ in measured)
    start_phase = fit.phase(start)
    chosen = []
    for offset in (0.0, math.pi):
        turns = math.ceil((start_phase - math.pi / 2 - offset) / (2 * math.pi))
        target = (offset + 2 * math.pi * turns - fit.phase0) / fit.phase_per_x
        target = min(max(target, start), stop)
        chosen.append(min(measured, key=lambda item: (abs(item[0] - target), item[0]))[1])
    return tuple(chosen)


def extract_calibration(probe, points, xs, unit="mW", x_unit="mW",
                        min_extinction_db=DEFAULT_MIN_EXTINCTION_DB) -> MZISweep:
    """Bright and dark extremes become cross/bar by the probe's bright state.

    Power is flat around its extremes, so a good cos² fit of the probed power
    places the first maximum and null. Otherwise the data decide: with a
    reference line (OSA) the contrast between the two lines, else the power,
    with near-equal points resolved to the lowest voltage.
    """
    points = tuple(points)
    fit = fit_mzi_response(xs, [point.power for point in points], x_unit)
    with_contrast = [point for point in points if point.contrast_db is not None]
    contrast = len(with_contrast) >= 2 and len(with_contrast) == len([p for p in points if p.power is not None])
    if contrast:
        measured, value, tie = with_contrast, (lambda point: point.contrast_db), 0.5
        extinction = max(map(value, measured)) - min(map(value, measured))
    else:
        measured = [point for point in points if point.power is not None and math.isfinite(point.power)]
        if len(measured) < 2:
            raise ValueError(f"{probe.name}: the detector returned fewer than two readings")
        value = lambda point: point.power
        high, low = max(map(value, measured)), min(map(value, measured))
        tie = EXTREME_TIE_FRACTION * (high - low)
        extinction = math.inf if low <= 0 < high else 10 * math.log10(high / low) if low > 0 else 0.0
    if extinction < min_extinction_db:
        raise ValueError(f"{probe.name}: only {extinction:.1f} dB change at output {probe.output + 1}; "
                         f"check that input {probe.input + 1} is lit and the detector is on that output")
    if fit is not None and fit.rms < 0.05 * fit.amplitude:
        bright, dark = fitted_extremes(fit, xs, points)
    else:
        bright = _extreme(measured, value, True, tie)
        dark = _extreme(measured, value, False, tie)
    cross, bar = (bright, dark) if probe.bright_crossed else (dark, bright)
    return MZISweep(probe, points, cross, bar, extinction, fit, unit)


@dataclass(frozen=True)
class CrosstalkSweep:
    aggressor: str
    victim: str
    probe: object
    bias_voltage: float
    victim_fit: MZIFit
    points: tuple[SweepPoint, ...]
    # Aggressor heater drive per point, in ``x_unit``.
    drive: tuple[float, ...]
    x_unit: str
    # Victim phase change from the first point [rad]; None where unreadable.
    phase_shift: tuple[float | None, ...]
    slope_rad_per_x: float | None
    # Victim-equivalent heater drive per aggressor heater drive.
    kappa: float | None
    # The same aggressor sweep with the victim at its bright extreme; empty when not taken.
    reference_points: tuple[SweepPoint, ...] = ()
    # κ from the quadrature sweep alone. Where the victim's bright and dark routes meet
    # again at a heated path MZI, the reference can over-correct; the two then differ.
    kappa_uncorrected: float | None = None
    # Why the curve is not thermal crosstalk (``path_contamination``); ``kappa`` is then None.
    contamination: str | None = None


@dataclass(frozen=True)
class ScreenResult:
    """Pretest of one aggressor: does a full swing move the victim at all?"""

    aggressor: str
    victim: str
    probe: object
    low_voltage: float
    high_voltage: float
    low: tuple[float | None, ...]
    high: tuple[float | None, ...]
    # The same swing with the victim at its bright extreme; empty without a path reference.
    reference_low: tuple[float | None, ...]
    reference_high: tuple[float | None, ...]
    # Victim phase change over the full aggressor swing, linearised at quadrature [rad].
    phase_shift_rad: float | None
    # Smallest phase change the readings could tell apart from noise [rad].
    resolution_rad: float | None
    min_phase_rad: float
    moved: bool


def _step(low, high):
    """Mean low reading, high/low ratio and its relative standard error; None if unusable."""
    lows = [power for power in low if power is not None and math.isfinite(power)]
    highs = [power for power in high if power is not None and math.isfinite(power)]
    if len(lows) < 2 or len(highs) < 2:
        return None
    mean_low, mean_high = sum(lows) / len(lows), sum(highs) / len(highs)
    if mean_low <= 0:
        return None

    def variance(values, mean):
        return sum((value - mean) ** 2 for value in values) / (len(values) - 1)

    spread = math.sqrt((variance(lows, mean_low) + variance(highs, mean_high)) / 2)
    return mean_low, mean_high / mean_low, spread * math.sqrt(1 / len(lows) + 1 / len(highs)) / mean_low


def relative_slope(fit: MZIFit, phase) -> float | None:
    """Relative change of the fitted response per radian of victim phase, T'/T."""
    level = fit.offset + fit.amplitude * (1 + math.cos(phase)) / 2
    return -fit.amplitude * math.sin(phase) / 2 / level if level > 0 else None


def evaluate_screen(aggressor, probe, victim_fit, low_voltage, high_voltage, low, high,
                    reference_low=(), reference_high=(),
                    min_phase_rad=DEFAULT_SCREEN_MIN_PHASE_RAD, bias_x=None, reference_x=None) -> ScreenResult:
    """The aggressor moved the victim if the step beats both the noise and ``min_phase_rad``.

    The aggressor also warms the other MZIs on the lit path. With the
    victim at its bright extreme mostly that path change shows, so the
    quadrature step is divided by the reference step, as in the full sweep.
    The bright extreme is seldom exactly stationary, so with ``bias_x`` and
    ``reference_x`` the victim's own response at both biases comes from the
    fit; without them the reference is taken as path only. Too few readings
    to decide count as moved, so the pair is measured rather than dropped.
    """
    quadrature = _step(low, high)
    has_reference = bool(reference_low or reference_high)
    reference = _step(reference_low, reference_high) if has_reference else (None, 1.0, 0.0)
    shift = resolution = None
    moved = True
    if quadrature and reference and victim_fit is not None and victim_fit.amplitude > 0:
        level, ratio, error = quadrature
        _, path_ratio, path_error = reference
        if bias_x is None:
            # At quadrature the response changes by amplitude/2 per radian.
            sensitivity = victim_fit.amplitude / 2 / level
        else:
            at_bias = relative_slope(victim_fit, victim_fit.phase(bias_x))
            at_reference = (relative_slope(victim_fit, victim_fit.phase(reference_x))
                            if has_reference and reference_x is not None else 0.0)
            sensitivity = (None if at_bias is None or at_reference is None
                           else at_bias - at_reference)
        if sensitivity:
            shift = (ratio / path_ratio - 1) / sensitivity
            resolution = SCREEN_SIGMA * math.hypot(error, path_error) / abs(sensitivity)
            moved = abs(shift) > max(min_phase_rad, resolution)
    return ScreenResult(aggressor, probe.name, probe, low_voltage, high_voltage, tuple(low), tuple(high),
                        tuple(reference_low), tuple(reference_high), shift, resolution, min_phase_rad, moved)


def screen_aggressor(controller, detector, probe, victim_fit, aggressor, low_voltage, high_voltage,
                     bias_voltage, settle_s, bright_voltage=None, min_phase_rad=DEFAULT_SCREEN_MIN_PHASE_RAD,
                     on_point=None, stop_event=None, sleep=time.sleep, bias_x=None, bright_x=None) -> ScreenResult:
    """Swing ``aggressor`` between its limits a few times with the victim at quadrature.

    With ``bright_voltage`` the swing is first repeated with the victim at its
    bright extreme, as a path reference. ``bias_x`` and ``bright_x`` are the
    victim's heater drive at both biases, for ``evaluate_screen``.
    """
    biases = ((bright_voltage, True),) if bright_voltage is not None else ()
    biases += ((bias_voltage, False),)
    total = len(SCREEN_PATTERN) * len(biases)
    readings = {True: ([], []), False: ([], [])}
    count = 0
    for victim_voltage, is_reference in biases:
        controller.drive_voltage(probe.name, victim_voltage)
        low, high = readings[is_reference]
        for raised in SCREEN_PATTERN:
            _check_stop(stop_event)
            voltage = high_voltage if raised else low_voltage
            controller.drive_voltage(aggressor, voltage)
            _wait(settle_s, stop_event, sleep)
            power = detector.read(probe).power
            (high if raised else low).append(power)
            count += 1
            if on_point is not None:
                on_point(aggressor, count, total, SweepPoint(voltage, power))
    return evaluate_screen(aggressor, probe, victim_fit, low_voltage, high_voltage, *readings[False],
                           *readings[True], min_phase_rad, bias_x, bright_x)


def phase_shift_from_power(fit: MZIFit, bias_x, powers, absolute=False) -> list[float | None]:
    """Victim phase change from the first reading, inverting the fitted cos².

    The branch is the one the bias sits on, so shifts stay continuous while
    the victim remains within ±π/2 of quadrature. ``absolute`` returns the
    phases themselves, in (-π, π].
    """
    bias_phase = fit.phase(bias_x) % (2 * math.pi)
    sign = 1 if bias_phase < math.pi else -1

    def phase(power):
        if power is None or not fit.amplitude:
            return None
        cosine = max(-1.0, min(1.0, 2 * (power - fit.offset) / fit.amplitude - 1))
        return sign * math.acos(cosine)

    phases = [phase(power) for power in powers]
    if absolute:
        return phases
    start = next((value for value in phases if value is not None), None)
    return [None if value is None or start is None else value - start for value in phases]


def _wrapped(phase):
    return (phase + math.pi) % (2 * math.pi) - math.pi


def phase_shift_with_reference(fit: MZIFit, bias_x, reference_x, powers, reference_powers):
    """Victim phase change from the first reading, with the path change removed.

    The same aggressor sweep was taken twice, with the victim at quadrature
    (``powers``) and near its bright extreme (``reference_powers``). Both see
    one path factor; the victim's own response at each bias comes from the
    fit, so a bright bias that is not exactly stationary still cancels. Each
    point solves T(φq+Δ)/T(φq) ÷ T(φb+Δ)/T(φb) = measured ratio for Δ.
    """
    pairs = list(zip(powers, reference_powers))
    start = next(((q, b) for q, b in pairs if q and b and q > 0 and b > 0), None)
    absolute = phase_shift_from_power(fit, bias_x, [start[0]] if start else [None], absolute=True)[0]
    if start is None or absolute is None:
        return [None] * len(pairs)
    q0, b0 = start
    # The fit was taken earlier; any drift since moves both biases alike.
    drift = absolute - _wrapped(fit.phase(bias_x))
    quadrature, bright = absolute, fit.phase(reference_x) + drift

    def model_ratio(delta):
        values = [fit.offset + fit.amplitude * (1 + math.cos(phase)) / 2
                  for phase in (quadrature + delta, quadrature, bright + delta, bright)]
        if min(values) <= 0:
            return None
        return math.log(values[0] / values[1]) - math.log(values[2] / values[3])

    grid = [index * math.pi / 400 - math.pi / 2 for index in range(401)]
    curve = [(delta, model_ratio(delta)) for delta in grid]
    shifts, previous = [], 0.0
    for q, b in pairs:
        if not q or not b or q <= 0 or b <= 0:
            shifts.append(None)
            continue
        target = math.log(q / q0) - math.log(b / b0)
        brackets = [(low, high) for (low, f_low), (high, f_high) in zip(curve, curve[1:])
                    if f_low is not None and f_high is not None and (f_low - target) * (f_high - target) <= 0]
        if not brackets:
            shifts.append(None)
            continue
        low, high = min(brackets, key=lambda pair: abs((pair[0] + pair[1]) / 2 - previous))
        for _ in range(40):
            middle = (low + high) / 2
            value = model_ratio(middle)
            if value is None:
                break
            if (model_ratio(low) - target) * (value - target) <= 0:
                high = middle
            else:
                low = middle
        previous = (low + high) / 2
        shifts.append(previous)
    return shifts


def _phase_slope(drive, shifts):
    pairs = [(x, shift) for x, shift in zip(drive, shifts) if shift is not None]
    if len(pairs) < 2 or max(x for x, _ in pairs) <= min(x for x, _ in pairs):
        return None
    return _linear_fit([x for x, _ in pairs], [shift for _, shift in pairs])[1]


def _least_squares(columns, values):
    """Coefficients c in values ≈ Σ c_k·columns_k and the residual sum of squares; None if singular."""
    size = len(columns)
    rows = [[sum(a * b for a, b in zip(columns[i], columns[j])) for j in range(size)]
            + [sum(a * v for a, v in zip(columns[i], values))] for i in range(size)]
    scale = max(abs(rows[index][index]) for index in range(size)) or 1.0
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12 * scale:
            return None
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(size):
            if row != column:
                factor = rows[row][column] / rows[column][column]
                rows[row] = [a - factor * b for a, b in zip(rows[row], rows[column])]
    coefficients = [rows[index][size] / rows[index][index] for index in range(size)]
    residual = sum((value - sum(c * column[index] for c, column in zip(coefficients, columns))) ** 2
                   for index, value in enumerate(values))
    return coefficients, residual


def path_contamination(aggressor, drive, shifts, aggressor_x_pi=None, x_unit="mW") -> str | None:
    """Why a victim phase curve is not thermal crosstalk, or None when it can be.

    Heat shifts the victim smoothly and nearly in proportion to the aggressor's
    drive. Light leaking through the aggressor instead varies with its own
    transfer, cos(π·x/x_pi) with ``aggressor_x_pi`` its half period in drive
    units, and a shift near ±π/2 or points without a phase mean the quadrature
    readout folded back. Heat also moves the victim one way only.
    """
    pairs = [(x, shift) for x, shift in zip(drive, shifts) if shift is not None]
    missing = len(drive) - len(pairs)
    if missing >= 2:
        return f"{missing} of {len(drive)} points give no phase: the quadrature readout folded back"
    peak = max((abs(shift) for _, shift in pairs), default=0.0)
    if peak > READOUT_LIMIT_RAD:
        return f"the shift reaches {peak:.2f} rad, where the quadrature readout folds back"
    pairs.sort()
    xs, values = [x for x, _ in pairs], [shift for _, shift in pairs]
    # Below about one period the cos term is nearly a straight line itself.
    if aggressor_x_pi and len(pairs) >= 6 and max(xs) - min(xs) >= 1.5 * aggressor_x_pi:
        phases = [math.pi * x / aggressor_x_pi for x in xs]
        linear = _least_squares([[1.0] * len(xs), xs], values)
        periodic = _least_squares([[1.0] * len(xs), xs, [math.cos(phase) for phase in phases],
                                   [math.sin(phase) for phase in phases]], values)
        if linear is not None and periodic is not None:
            amplitude = math.hypot(*periodic[0][2:])
            if amplitude >= PATH_MIN_AMPLITUDE_RAD and periodic[1] <= PATH_RESIDUAL_RATIO * linear[1]:
                return (f"follows {aggressor}'s own switching (±{amplitude:.2f} rad, period "
                        f"{2 * aggressor_x_pi:.3g} {x_unit}): light through {aggressor} reaches the detector")
    if len(pairs) >= 3:
        trend = 1 if values[-1] >= values[0] else -1
        reached, reversal = -math.inf, 0.0
        for value in values:
            reached = max(reached, trend * value)
            reversal = max(reversal, reached - trend * value)
        if reversal > PATH_REVERSAL_RAD:
            return f"the shift turns back by {reversal:.2f} rad; heat moves the victim one way"
    return None


def analyse_crosstalk(aggressor, probe, bias_voltage, victim_fit, bias_x, points, drive, x_unit,
                      reference_points=(), reference_x=None, aggressor_x_pi=None):
    powers = [point.power for point in points]
    raw = phase_shift_from_power(victim_fit, bias_x, powers)
    shifts = raw
    if reference_points and reference_x is not None:
        shifts = phase_shift_with_reference(victim_fit, bias_x, reference_x, powers,
                                            [point.power for point in reference_points])
    slope, raw_slope = _phase_slope(drive, shifts), _phase_slope(drive, raw)
    contamination = path_contamination(aggressor, drive, shifts, aggressor_x_pi, x_unit)

    def kappa(value):
        return None if value is None or x_unit != victim_fit.x_unit else value / victim_fit.phase_per_x

    return CrosstalkSweep(aggressor, probe.name, probe, bias_voltage, victim_fit, tuple(points), tuple(drive),
                          x_unit, tuple(shifts), slope, None if contamination else kappa(slope),
                          tuple(reference_points), kappa(raw_slope), contamination)


def aggressor_half_period(controller, name, x_unit, fits=()) -> float | None:
    """Heater drive for a π change of ``name``: from a fit of it, else its calibrated cross and bar."""
    for fit in fits:
        if fit is not None and fit.x_unit == x_unit:
            return abs(fit.x_pi)
    calibration = controller.settings["calibration"].get(name) or {}
    cross, bar = calibration.get("cross"), calibration.get("straight")
    if cross is None or bar is None:
        return None
    (cross_x, unit), (bar_x, _) = heater_drive(controller, name, cross), heater_drive(controller, name, bar)
    if unit != x_unit or bar_x == cross_x:
        return None
    return abs(bar_x - cross_x)


def quadrature_point(sweep: MZISweep, xs) -> SweepPoint:
    """The swept point closest to the steepest part of the fitted response, lowest voltage first."""
    fit = sweep.fit
    if fit is None:
        raise ValueError(f"{sweep.name}: its response could not be fitted, so it cannot be biased at quadrature")
    scored = [(abs(math.cos(fit.phase(x))), point) for x, point in zip(xs, sweep.points) if point.power is not None]
    best = min(score for score, _ in scored)
    return min((point for score, point in scored if score <= max(best, 0.2)), key=lambda point: abs(point.voltage))


def background_voltage(controller, name, crossed, found=None, zero_cross=False) -> float:
    """Return a path bias; calibration can keep background crosses at 0 V.

    A calibrated extreme leaves the MZI least sensitive to stray heat.
    """
    if crossed and zero_cross:
        return 0.0
    if found and name in found:
        return (found[name].cross if crossed else found[name].bar).voltage
    voltage = controller.settings["calibration"][name]["cross" if crossed else "straight"]
    if voltage is None and not crossed:
        raise ValueError(f"{name} must be calibrated before it can be held at bar")
    return 0.0 if voltage is None else voltage


def apply_background(controller, probe, found=None, zero_cross=False) -> dict[str, float]:
    voltages = {name: background_voltage(controller, name, crossed, found, zero_cross)
                for name, crossed in probe.background}
    for name, voltage in voltages.items():
        if controller.applied_voltages.get(name) != voltage:
            controller.drive_voltage(name, voltage)
    return voltages


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise CalibrationStopped("Measurement stopped")


def sweep(controller, detector, probe, name, voltages, settle_s, on_point=None, stop_event=None,
          sleep=time.sleep) -> list[SweepPoint]:
    """Drive ``name`` through ``voltages`` and read the probe after each settle."""
    wait_for_light = getattr(detector, "wait_for_light", None)
    if wait_for_light is not None and voltages:
        controller.drive_voltage(name, voltages[0])
        _wait(settle_s, stop_event, sleep)
        wait_for_light(probe, stop_event, sleep)
    points = []
    for voltage in voltages:
        _check_stop(stop_event)
        controller.drive_voltage(name, voltage)
        _wait(settle_s, stop_event, sleep)
        reading = detector.read(probe)
        point = SweepPoint(voltage, reading.power, reading.reference)
        points.append(point)
        if on_point is not None:
            on_point(name, len(points), len(voltages), point)
    return points


def scan_with_detector(controller, open_detector, names, source, output, voltages, settle_s, on_point=None,
                       on_rescan=None, sleep=time.sleep) -> dict[str, list[SweepPoint]]:
    """Sweep each switch in turn on a manual route, read by a calibration detector.

    ``open_detector`` returns (detector, close), as ``detection_setup()["open"]``
    does. Only input ``source`` is lit and the reading is taken at ``output``
    (0-based). A detector may finalize each curve or request legacy rescans.
    Every scanned switch ends at 0 V.
    """
    detector, close = open_detector()
    results = {}
    try:
        detector.begin()
        try:
            for name in names:
                probe = Probe(name, source, output, True, (), source)
                detector.prepare(probe)
                points = sweep(controller, detector, probe, name, voltages, settle_s, on_point, sleep=sleep)
                rescan = getattr(detector, "rescan_points", None)
                if rescan is not None:
                    points = rescan(controller, probe, voltages, settle_s, points, sleep=sleep)
                    if on_rescan is not None:
                        on_rescan(name, points)
                finish_curve_points = getattr(detector, "finish_curve_points", None)
                if finish_curve_points is not None:
                    points = finish_curve_points(probe, voltages, points)
                controller.drive_voltage(name, 0.0)
                results[name] = points
        finally:
            for name in names:
                controller.drive_voltage(name, 0.0)
            detector.finish()
    finally:
        close()
    return results


def require_ready(controller, network, voltages, settle_s):
    if not controller.connected:
        raise RuntimeError("Connect the voltage board first")
    for name in network.switches:
        if controller.settings["mapping"].get(name) is None:
            raise ValueError(f"Assign a voltage-board pin to {name} first")
    if not voltages:
        raise ValueError("Choose at least one sweep voltage")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be a nonnegative number")


def _session(controller, network, detector, body):
    """Run ``body`` with the detector open; every MZI ends at 0 V and the lasers are restored."""
    error = None
    try:
        reset_all(controller, network)
        detector.begin()
        return body()
    except BaseException as failure:
        error = failure
        raise
    finally:
        try:
            try:
                reset_all(controller, network)
            finally:
                detector.finish()
        except Exception as cleanup:
            if error is None:
                raise
            error.add_note(f"Cleanup also failed: {cleanup}")


def run_calibration(controller, detector, network, steps, voltages, settle_s, save=True,
                    min_extinction_db=DEFAULT_MIN_EXTINCTION_DB, results=None, on_step=None, on_point=None,
                    on_result=None, stop_event=None, sleep=time.sleep) -> dict[str, MZISweep]:
    """Sweep each planned probe in order; ``results`` fills as MZIs finish."""
    require_ready(controller, network, voltages, settle_s)
    results = {} if results is None else results

    def body():
        for index, probe in enumerate(steps, start=1):
            _check_stop(stop_event)
            if on_step is not None:
                on_step(probe, index, len(steps))
            before = dict(controller.applied_voltages)
            background = apply_background(controller, probe, results, zero_cross=True)
            background_changed = any(before.get(name) != voltage for name, voltage in background.items())
            detector.prepare(probe)
            if background_changed:
                _wait(max(settle_s, getattr(detector, "background_settle_s", 0.0)), stop_event, sleep)
            points = sweep(controller, detector, probe, probe.name, voltages, settle_s,
                           on_point, stop_event, sleep)
            rescan = getattr(detector, "rescan_points", None)
            if rescan is not None:
                points = rescan(controller, probe, voltages, settle_s, points, on_point, stop_event, sleep)
            finish_curve_points = getattr(detector, "finish_curve_points", None)
            if finish_curve_points is not None:
                points = finish_curve_points(probe, voltages, points)
            controller.drive_voltage(probe.name, 0.0)
            xs, x_unit = zip(*(heater_drive(controller, probe.name, voltage) for voltage in voltages))
            result = extract_calibration(probe, points, xs, detector.unit, x_unit[0], min_extinction_db)
            if save:
                controller.set_calibration(probe.name, result.bar.voltage, result.cross.voltage)
            results[probe.name] = result
            if on_result is not None:
                on_result(result)
        return results

    return _session(controller, network, detector, body)


@dataclass
class CrosstalkRun:
    """Filled step by step, so a stopped run keeps what was measured."""

    victims: dict = None
    pairs: dict = None
    # (aggressor, victim) -> ScreenResult, when the pretest ran.
    screens: dict = None
    # (aggressor, victim) -> why its victim probe could not be used.
    failed: dict = None

    def __post_init__(self):
        self.victims = {} if self.victims is None else self.victims
        self.pairs = {} if self.pairs is None else self.pairs
        self.screens = {} if self.screens is None else self.screens
        self.failed = {} if self.failed is None else self.failed

    @property
    def static_pairs(self) -> set[tuple[str, str]]:
        """Pairs the pretest saw no movement for, so they were not swept."""
        return {key for key, screen in self.screens.items() if not screen.moved}


def run_crosstalk(controller, detector, network, groups, voltages, settle_s, path_reference=True, run=None,
                  on_step=None, on_point=None, on_victim=None, on_pair=None, stop_event=None,
                  sleep=time.sleep, screen_min_phase_rad=None, on_screen=None,
                  min_extinction_db=DEFAULT_MIN_EXTINCTION_DB, on_failed=None) -> CrosstalkRun:
    """For each victim probe: sweep the victim, bias it at quadrature, then sweep each aggressor.

    With ``path_reference`` each aggressor is swept twice, first with the
    victim at its bright extreme, to remove heating of the rest of the path.
    With ``screen_min_phase_rad`` each aggressor is first swung between its
    limits a few times; one that moves the victim by less is recorded in
    ``run.screens`` and not swept. A victim whose sweep changes the reading
    by less than ``min_extinction_db``, or cannot be fitted, is recorded in
    ``run.failed`` for its aggressors and the run moves on.
    """
    require_ready(controller, network, voltages, settle_s)
    run = CrosstalkRun() if run is None else run

    def body():
        for index, group in enumerate(groups, start=1):
            probe = group.probe
            _check_stop(stop_event)
            if on_step is not None:
                on_step(group, index, len(groups))
            detector.prepare(probe)
            background = apply_background(controller, probe)
            points = sweep(controller, detector, probe, probe.name, voltages, settle_s, on_point, stop_event, sleep)
            xs, units = zip(*(heater_drive(controller, probe.name, voltage) for voltage in voltages))
            try:
                victim = extract_calibration(probe, points, xs, detector.unit, units[0], min_extinction_db)
                bias = quadrature_point(victim, xs)
            except ValueError as error:
                controller.drive_voltage(probe.name, background_voltage(controller, probe.name, True))
                for aggressor in group.aggressors:
                    run.failed[aggressor, probe.name] = str(error)
                if on_failed is not None:
                    on_failed(group, str(error))
                continue
            run.victims[probe.name, probe.input, probe.output] = victim
            if on_victim is not None:
                on_victim(victim, bias)
            bias_x = heater_drive(controller, probe.name, bias.voltage)[0]
            bright = victim.cross if probe.bright_crossed else victim.bar
            bright_x = heater_drive(controller, probe.name, bright.voltage)[0]
            for aggressor in group.aggressors:
                if screen_min_phase_rad is not None:
                    screen = screen_aggressor(controller, detector, probe, victim.fit, aggressor, voltages[0],
                                              voltages[-1], bias.voltage, settle_s,
                                              bright.voltage if path_reference else None,
                                              screen_min_phase_rad, on_point, stop_event, sleep,
                                              bias_x=bias_x, bright_x=bright_x)
                    controller.drive_voltage(aggressor, background.get(aggressor, 0.0))
                    run.screens[aggressor, probe.name] = screen
                    if on_screen is not None:
                        on_screen(screen)
                    if not screen.moved:
                        continue
                reference = ()
                if path_reference:
                    controller.drive_voltage(probe.name, bright.voltage)
                    _wait(settle_s, stop_event, sleep)
                    reference = sweep(controller, detector, probe, aggressor, voltages, settle_s,
                                      on_point, stop_event, sleep)
                    controller.drive_voltage(aggressor, background.get(aggressor, 0.0))
                controller.drive_voltage(probe.name, bias.voltage)
                _wait(settle_s, stop_event, sleep)
                points = sweep(controller, detector, probe, aggressor, voltages, settle_s,
                               on_point, stop_event, sleep)
                controller.drive_voltage(aggressor, background.get(aggressor, 0.0))
                drive, units = zip(*(heater_drive(controller, aggressor, voltage) for voltage in voltages))
                half_period = aggressor_half_period(
                    controller, aggressor, units[0],
                    [item.fit for key, item in run.victims.items() if key[0] == aggressor])
                result = analyse_crosstalk(aggressor, probe, bias.voltage, victim.fit, bias_x, points,
                                           drive, units[0], reference, bright_x, half_period)
                run.pairs[aggressor, probe.name] = result
                if on_pair is not None:
                    on_pair(result)
            controller.drive_voltage(probe.name, background_voltage(controller, probe.name, True))
        return run

    return _session(controller, network, detector, body)


def create_crosstalk_folder(project_dir: Path, kind: str) -> Path:
    folder = Path(project_dir) / "Results" / CROSSTALK_SUBFOLDER / f"{datetime.now():%Y%m%d_%H%M%S_%f}_{kind}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _probe_record(probe):
    return {"mzi": probe.name, "input": probe.input + 1, "output": probe.output + 1,
            "output_fiber": fiber_number(probe.output + 1),
            "bright_state": "cross" if probe.bright_crossed else "bar",
            "other_input": probe.other_input + 1, "background_bar": list(probe.biased)}


def _fit_record(fit):
    return None if fit is None else {"offset": fit.offset, "amplitude": fit.amplitude, "x_pi": fit.x_pi,
                                     "phase0_rad": fit.phase0, "rms": fit.rms, "x_unit": fit.x_unit}


def _header(context, title):
    lines = [f"# {title} saved {datetime.now().isoformat(timespec='seconds')}\n"]
    return lines + [f"# Comment: {line}\n" for line in context.get("comment", "").splitlines() or [""]]


def _sweep_rows(writer, label, probe, points, unit):
    for point in points:
        writer.writerow([label, probe.name, probe.input + 1, probe.output + 1, point.voltage,
                         "" if point.power is None else point.power, mw_to_dbm(point.power) if unit == "mW" else "",
                         "" if point.reference is None else point.reference])


def _plot_values(points, unit):
    if unit == "mW":
        return [float("nan") if mw_to_dbm(point.power) is None else mw_to_dbm(point.power) for point in points], "dBm"
    return [float("nan") if point.power is None else point.power for point in points], unit


def save_calibration(folder: Path, context: dict, results: dict[str, MZISweep]) -> list[str]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    record = {**context, "mzi_calibrations": {
        name: {**_probe_record(item.probe), "cross_voltage_v": item.cross.voltage, "bar_voltage_v": item.bar.voltage,
               "extinction_db": None if math.isinf(item.extinction_db) else item.extinction_db,
               "fit": _fit_record(item.fit)}
        for name, item in results.items()}}
    (folder / "mzi_calibration.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    files = ["mzi_calibration.json"]
    if not results:
        return files
    unit = next(iter(results.values())).unit
    with (folder / "mzi_sweeps.csv").open("w", encoding="utf-8", newline="") as file:
        file.writelines(_header(context, "MZI calibration"))
        writer = csv.writer(file)
        writer.writerow(["sweep", "mzi", "input", "output", "voltage_v", f"power_{unit}", "power_dbm",
                         f"reference_{unit}"])
        for name, item in results.items():
            _sweep_rows(writer, "calibration", item.probe, item.points, unit)
    files.append("mzi_sweeps.csv")
    hp_bnc = context.get("hp_bnc") or {}
    range_points = hp_bnc.get("point_ranges") or {}
    arduino_settings = context.get("arduino_settings") or {}
    bnc_reference = hp_bnc.get("bnc_reference")
    initial_range_dbm = hp_bnc.get("reference_range_dbm")

    def initial_power(raw, range_dbm):
        if bnc_reference is not None:
            return (10 ** (bnc_reference["dbm"] / 10) * raw / bnc_reference["adc"]
                    * 10 ** ((range_dbm - bnc_reference["range_dbm"]) / 10))
        return raw * arduino_settings.get("scale", 1.0) + arduino_settings.get("offset", 0.0)

    if range_points:
        with (folder / "mzi_hp_bnc_points.csv").open("w", encoding="utf-8", newline="") as file:
            file.writelines(_header(context, "HP BNC Arduino range comparison"))
            writer = csv.writer(file)
            adaptive = hp_bnc.get("recovery_method") == "adaptive_fixed_range"
            heading = ["mzi", "voltage_v", "initial_mean_adc", "initial_range_dbm",
                       f"initial_power_{unit}", "final_mean_adc", "final_range_dbm",
                       f"adjusted_power_{unit}", "range_steps" if adaptive else "rescans", "range_limit"]
            writer.writerow(heading + (["range_error"] if adaptive else
                                       ["measurement_source", "gpib_dbm"]))
            for name, item in results.items():
                for point, record_point in zip(item.points, range_points.get(name, [])):
                    point_initial_range = record_point.get("initial_range_dbm", initial_range_dbm)
                    row = [name, point.voltage, record_point["initial_raw_adc"],
                           point_initial_range,
                           initial_power(record_point["initial_raw_adc"], point_initial_range),
                           record_point["final_raw_adc"], record_point["range_dbm"],
                           point.power, record_point.get("range_steps", 0) if adaptive
                           else record_point["rescans"], record_point.get("range_limit") or ""]
                    writer.writerow(row + [record_point.get("range_error") or ""] if adaptive else row +
                                    [record_point.get("measurement_source", "Arduino"),
                                     record_point.get("gpib_dbm")])
        files.append("mzi_hp_bnc_points.csv")
    from matplotlib.figure import Figure

    for name, item in results.items():
        figure = Figure(figsize=(7, 4))
        axis = figure.add_subplot(111)
        if arduino_settings and item.unit != "mW":
            values = [float("nan") if point.power is None else point.power for point in item.points]
            label = item.unit
            axis.set_yscale("symlog", base=10,
                            linthresh=max(abs(arduino_settings.get("scale", 1.0)), 1e-12))
        else:
            values, label = _plot_values(item.points, item.unit)
        point_ranges = range_points.get(name)
        if (point_ranges and len(point_ranges) == len(item.points)
                and hp_bnc.get("recovery_method") != "adaptive_fixed_range"):
            initial_values = [initial_power(record_point["initial_raw_adc"],
                                            record_point.get("initial_range_dbm", initial_range_dbm))
                              for record_point in point_ranges]
            if item.unit == "mW":
                initial_values = [float("nan") if mw_to_dbm(value) is None else mw_to_dbm(value)
                                  for value in initial_values]
            axis.plot([point.voltage for point in item.points], initial_values, ".-",
                      label="Initial HP range")
            axis.plot([point.voltage for point in item.points], values, ".--",
                      label="After GPIB check" if hp_bnc.get("recovery_method") == "gpib_out_of_window"
                      else "After range adjustment")
        else:
            axis.plot([point.voltage for point in item.points], values, ".-", label=f"In {item.probe.input + 1}")
        axis.axvline(item.cross.voltage, color="tab:blue", linestyle="--", label=f"Cross {item.cross.voltage:g} V")
        axis.axvline(item.bar.voltage, color="tab:orange", linestyle="--", label=f"Bar {item.bar.voltage:g} V")
        axis.set(xlabel="Voltage [V]", ylabel=f"Power [{label}]",
                 title=f"{name}: {item.probe.describe()}")
        axis.grid(True)
        axis.legend(fontsize=8)
        figure.tight_layout()
        filename = f"{name.replace('.', '_')}_sweep.png"
        figure.savefig(folder / filename, dpi=150)
        files.append(filename)
    return files


def kappa_matrix(network, pairs) -> dict[str, dict[str, float | None]]:
    """{aggressor: {victim: kappa}} over every switch, None where unmeasured."""
    return {aggressor: {victim: (pairs[aggressor, victim].kappa if (aggressor, victim) in pairs else None)
                        for victim in network.switches}
            for aggressor in network.switches}


def save_crosstalk(folder: Path, context: dict, run: CrosstalkRun, network, plan=None) -> list[str]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    matrix = kappa_matrix(network, run.pairs)
    static = run.static_pairs
    marks = crosstalk_marks(run, plan)
    leaks = {} if plan is None else {(aggressor, group.probe.name): group.leak_order(aggressor)
                                     for group in plan.groups for aggressor in group.aggressors}

    def leak_record(item):
        key = item.aggressor, item.victim
        if key not in leaks:
            return None
        return "never" if leaks[key] is None else leaks[key]

    record = {**context,
              "kappa_definition": "victim-equivalent heater drive per aggressor heater drive (phase slope ratio)",
              "kappa": matrix,
              "leak_order_definition": "held MZIs that must leak before light through the aggressor reaches "
                                       "the detector; never: no such light reaches it",
              "pairs": [{"aggressor": item.aggressor, **_probe_record(item.probe), "victim": item.victim,
                         "bias_voltage_v": item.bias_voltage, "x_unit": item.x_unit,
                         "slope_rad_per_x": item.slope_rad_per_x, "kappa": item.kappa,
                         "kappa_uncorrected": item.kappa_uncorrected,
                         "contamination": item.contamination, "leak_order": leak_record(item),
                         "max_phase_shift_rad": max((abs(value) for value in item.phase_shift if value is not None),
                                                    default=None)}
                        for item in run.pairs.values()],
              "pretest": [_screen_record(item) for item in run.screens.values()],
              "static_pairs": [{"aggressor": aggressor, "victim": victim} for aggressor, victim in sorted(static)],
              "failed_pairs": [{"aggressor": aggressor, "victim": victim, "reason": reason}
                               for (aggressor, victim), reason in sorted(run.failed.items())],
              "victims": [{**_probe_record(item.probe), "fit": _fit_record(item.fit),
                           "extinction_db": None if math.isinf(item.extinction_db) else item.extinction_db}
                          for item in run.victims.values()]}
    (folder / "crosstalk_matrix.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with (folder / "crosstalk_matrix.csv").open("w", encoding="utf-8", newline="") as file:
        file.writelines(_header(context, "Thermal crosstalk matrix"))
        file.write(f"# kappa: rows are aggressors, columns victims. {MARK_LEGEND}\n")
        writer = csv.writer(file)
        writer.writerow(["aggressor"] + list(network.switches))
        for aggressor, row in matrix.items():
            writer.writerow([aggressor] + [marks.get((aggressor, victim), "") if value is None else f"{value:.6g}"
                                           for victim, value in row.items()])
    unit = context.get("unit", "mW")
    with (folder / "crosstalk_sweeps.csv").open("w", encoding="utf-8", newline="") as file:
        file.writelines(_header(context, "Thermal crosstalk sweeps"))
        writer = csv.writer(file)
        writer.writerow(["swept", "victim", "input", "output", "voltage_v", f"power_{unit}", "power_dbm",
                         f"reference_{unit}", "aggressor_drive", "drive_unit", "victim_phase_shift_rad"])
        for item in run.victims.values():
            for point in item.points:
                writer.writerow([item.name, item.name, item.probe.input + 1, item.probe.output + 1, point.voltage,
                                 "" if point.power is None else point.power,
                                 mw_to_dbm(point.power) if unit == "mW" else "", "", "", "", ""])
        for item in run.pairs.values():
            for point, drive in zip(item.reference_points, item.drive):
                writer.writerow([f"{item.aggressor} (victim bright)", item.victim, item.probe.input + 1,
                                 item.probe.output + 1, point.voltage, "" if point.power is None else point.power,
                                 mw_to_dbm(point.power) if unit == "mW" else "", "", drive, item.x_unit, ""])
            for point, drive, shift in zip(item.points, item.drive, item.phase_shift):
                writer.writerow([item.aggressor, item.victim, item.probe.input + 1, item.probe.output + 1,
                                 point.voltage, "" if point.power is None else point.power,
                                 mw_to_dbm(point.power) if unit == "mW" else "", "", drive, item.x_unit,
                                 "" if shift is None else shift])
    files = ["crosstalk_matrix.json", "crosstalk_matrix.csv", "crosstalk_sweeps.csv"]
    if run.screens:
        with (folder / "crosstalk_pretest.csv").open("w", encoding="utf-8", newline="") as file:
            file.writelines(_header(context, "Thermal crosstalk pretest"))
            file.write("# Victim at quadrature (reference: at its bright extreme); aggressor swung "
                       "low-high-high-low twice. moved=0 pairs were not swept.\n")
            writer = csv.writer(file)
            writer.writerow(["aggressor", "victim", "input", "output", "low_v", "high_v",
                             f"low_{unit}", f"high_{unit}", f"reference_low_{unit}", f"reference_high_{unit}",
                             "phase_shift_rad", "resolution_rad", "min_phase_rad", "moved"])
            for item in run.screens.values():
                writer.writerow([item.aggressor, item.victim, item.probe.input + 1, item.probe.output + 1,
                                 item.low_voltage, item.high_voltage,
                                 " ".join("" if power is None else f"{power:.6g}" for power in item.low),
                                 " ".join("" if power is None else f"{power:.6g}" for power in item.high),
                                 " ".join("" if power is None else f"{power:.6g}" for power in item.reference_low),
                                 " ".join("" if power is None else f"{power:.6g}" for power in item.reference_high),
                                 "" if item.phase_shift_rad is None else f"{item.phase_shift_rad:.6g}",
                                 "" if item.resolution_rad is None else f"{item.resolution_rad:.6g}",
                                 item.min_phase_rad, int(item.moved)])
        files.append("crosstalk_pretest.csv")
    from matplotlib.figure import Figure

    figure = Figure(figsize=(8, 7))
    draw_kappa_heatmap(figure.add_subplot(111), network, matrix, marks)
    figure.tight_layout(rect=(0, 0.03, 1, 1))
    figure.savefig(folder / "crosstalk_matrix.png", dpi=150)
    files.append("crosstalk_matrix.png")
    victims = sorted({item.victim for item in run.pairs.values()})
    for victim in victims:
        figure = Figure(figsize=(8, 4))
        draw_phase_shifts(figure.add_subplot(111), victim,
                          [item for item in run.pairs.values() if item.victim == victim])
        figure.tight_layout()
        filename = f"{victim.replace('.', '_')}_crosstalk.png"
        figure.savefig(folder / filename, dpi=150, bbox_inches="tight")
        files.append(filename)
    return files


def _screen_record(item):
    return {"aggressor": item.aggressor, **_probe_record(item.probe), "victim": item.victim,
            "low_voltage_v": item.low_voltage, "high_voltage_v": item.high_voltage,
            "low_readings": list(item.low), "high_readings": list(item.high),
            "reference_low_readings": list(item.reference_low),
            "reference_high_readings": list(item.reference_high),
            "phase_shift_rad": item.phase_shift_rad, "resolution_rad": item.resolution_rad,
            "min_phase_rad": item.min_phase_rad, "moved": item.moved}


# Cell markers of the κ heatmap, for pairs without a value.
MARK_STATIC, MARK_DISTANT, MARK_BLOCKED, MARK_FAILED, MARK_PATH = "n.s.", "far", "×", "!", "path"
MARK_LEGEND = (f"{MARK_STATIC} pretest saw no shift · {MARK_DISTANT} more than one stage apart, not measured · "
               f"{MARK_BLOCKED} no probe keeps the aggressor's light from the detector · "
               f"{MARK_FAILED} victim probe failed · {MARK_PATH} curve follows the aggressor's switching, no κ · "
               "blank: not measured yet")


def crosstalk_marks(run: CrosstalkRun, plan=None) -> dict[tuple[str, str], str]:
    """(aggressor, victim) -> heatmap marker for every pair the run has no κ for."""
    marks = {}
    if plan is not None:
        marks.update(dict.fromkeys(plan.distant, MARK_DISTANT))
        marks.update(dict.fromkeys(plan.uncovered, MARK_BLOCKED))
    marks.update(dict.fromkeys(run.failed, MARK_FAILED))
    marks.update(dict.fromkeys(run.static_pairs, MARK_STATIC))
    for key, pair in run.pairs.items():
        marks.pop(key, None)
        if pair.contamination:
            marks[key] = MARK_PATH
    return marks


def _percent(value):
    return f"{value:.1f}" if abs(value) >= 1 else f"{value:.2f}"


def draw_kappa_heatmap(axis, network, matrix, marks=None):
    """κ in percent, aggressors as rows; pairs without a value carry their ``crosstalk_marks`` marker."""
    from matplotlib.patches import Rectangle

    marks = marks or {}
    names = list(network.switches)
    values = [[float("nan") if matrix[aggressor][victim] is None else matrix[aggressor][victim] * 100
               for victim in names] for aggressor in names]
    finite = [abs(value) for row in values for value in row if math.isfinite(value)]
    limit = max(finite, default=1.0) or 1.0
    axis.set_facecolor("#f4f4f4")
    image = axis.imshow(values, cmap="coolwarm", vmin=-limit, vmax=limit)
    count = len(names)
    axis.set_xticks(range(count), names, rotation=90)
    axis.set_yticks(range(count), names)
    axis.set_xticks([index - 0.5 for index in range(1, count)], minor=True)
    axis.set_yticks([index - 0.5 for index in range(1, count)], minor=True)
    axis.grid(which="minor", color="white", linewidth=1)
    axis.tick_params(which="minor", length=0)
    # Heavier lines between stages.
    stages = [network.switches[name][0] for name in names]
    for index in range(1, count):
        if stages[index] != stages[index - 1]:
            axis.axhline(index - 0.5, color="#555555", linewidth=1.2)
            axis.axvline(index - 0.5, color="#555555", linewidth=1.2)
    for index in range(count):
        axis.add_patch(Rectangle((index - 0.5, index - 0.5), 1, 1, color="#9a9a9a", zorder=2))
    for row, line in enumerate(values):
        for column, value in enumerate(line):
            if math.isfinite(value):
                axis.text(column, row, _percent(value), ha="center", va="center", fontsize=6.5,
                          color="white" if abs(value) > 0.65 * limit else "black")
            elif (names[row], names[column]) in marks:
                axis.text(column, row, marks[names[row], names[column]], ha="center", va="center",
                          fontsize=6, color="#777777")
    axis.set(xlabel="Victim", ylabel="Aggressor", title="Thermal crosstalk κ [%]")
    axis.set_xlim(-0.5, count - 0.5)
    axis.set_ylim(count - 0.5, -0.5)
    # The colorbar hangs off the image, so the pair stays centred in a wide window.
    from mpl_toolkits.axes_grid1 import make_axes_locatable

    axis.figure.colorbar(image, cax=make_axes_locatable(axis).append_axes("right", size="4%", pad=0.1),
                         label="κ [%]")
    axis.annotate(MARK_LEGEND, (0, 0), xycoords="figure fraction", xytext=(6, 4), textcoords="offset points",
                  fontsize=6, color="#555555", wrap=True)


def draw_phase_shifts(axis, victim, items):
    """Victim phase shift against each aggressor's heater drive, one line per aggressor."""
    from matplotlib import colormaps

    # Up to eleven aggressors per victim: ten distinct hues, then a second set.
    colors = colormaps["tab10"].colors + colormaps["Dark2"].colors
    for index, item in enumerate(sorted(items, key=lambda item: item.aggressor)):
        label = item.aggressor + ("  path, no κ" if item.contamination else
                                  "" if item.kappa is None else f"  κ {_percent(item.kappa * 100)} %")
        axis.plot(item.drive, [float("nan") if value is None else value for value in item.phase_shift],
                  ".:" if item.contamination else ".-", markersize=3, linewidth=1.2,
                  color=colors[index % len(colors)], label=label)
    unit = items[0].x_unit if items else "mW"
    axis.axhline(0, color="#999999", linewidth=0.8)
    axis.set(xlabel=f"Aggressor heater drive [{unit}]", ylabel="Victim phase shift [rad]",
             title=f"{victim}: phase shift at quadrature")
    axis.grid(True, alpha=0.4)
    if items:
        axis.legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0, frameon=False)
