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

    def __init__(self, read_power, lasers, unit="mW", switch=None, fixed_output=None, label="Power meter"):
        if (switch is None) == (fixed_output is None):
            raise ValueError("Read the power meter through the JDSU or on one fixed output")
        self.read_power = read_power
        self.lasers = lasers
        self.unit = unit
        self.switch = switch
        self.fixed_output = fixed_output
        self.label = label

    @property
    def inputs(self):
        return tuple(range(6))

    @property
    def outputs(self):
        return tuple(range(6)) if self.switch is not None else (self.fixed_output,)

    def begin(self):
        self.lasers.begin()

    def prepare(self, probe):
        if probe.output not in self.outputs:
            raise ValueError(f"{probe.name} needs the power meter on output {probe.output + 1}")
        if self.switch is not None:
            _select(self.switch, probe.output)
        self.lasers.light_only(probe.input)

    def read(self, probe) -> Reading:
        return Reading(self.read_power())

    def finish(self):
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


def evaluate_screen(aggressor, probe, victim_fit, low_voltage, high_voltage, low, high,
                    reference_low=(), reference_high=(),
                    min_phase_rad=DEFAULT_SCREEN_MIN_PHASE_RAD) -> ScreenResult:
    """The aggressor moved the victim if the step beats both the noise and ``min_phase_rad``.

    The aggressor also warms the other MZIs on the lit path. With the
    victim at its bright extreme only that path change shows, so the
    quadrature step is divided by the reference step, as in the full sweep.
    Too few readings to decide count as moved, so the pair is measured
    rather than dropped.
    """
    quadrature = _step(low, high)
    reference = _step(reference_low, reference_high) if reference_low or reference_high else (None, 1.0, 0.0)
    shift = resolution = None
    moved = True
    if quadrature and reference and victim_fit is not None and victim_fit.amplitude > 0:
        level, ratio, error = quadrature
        _, path_ratio, path_error = reference
        # At quadrature the response changes by amplitude/2 per radian.
        slope = victim_fit.amplitude / 2
        shift = level * (ratio / path_ratio - 1) / slope
        resolution = SCREEN_SIGMA * level * math.hypot(error, path_error) / slope
        moved = abs(shift) > max(min_phase_rad, resolution)
    return ScreenResult(aggressor, probe.name, probe, low_voltage, high_voltage, tuple(low), tuple(high),
                        tuple(reference_low), tuple(reference_high), shift, resolution, min_phase_rad, moved)


def screen_aggressor(controller, detector, probe, victim_fit, aggressor, low_voltage, high_voltage,
                     bias_voltage, settle_s, bright_voltage=None, min_phase_rad=DEFAULT_SCREEN_MIN_PHASE_RAD,
                     on_point=None, stop_event=None, sleep=time.sleep) -> ScreenResult:
    """Swing ``aggressor`` between its limits a few times with the victim at quadrature.

    With ``bright_voltage`` the swing is first repeated with the victim at its
    bright extreme, as a path reference.
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
                           *readings[True], min_phase_rad)


def phase_shift_from_power(fit: MZIFit, bias_x, powers) -> list[float | None]:
    """Victim phase change from the first reading, inverting the fitted cos².

    The branch is the one the bias sits on, so shifts stay continuous while
    the victim remains within ±π/2 of quadrature.
    """
    bias_phase = fit.phase(bias_x) % (2 * math.pi)
    sign = 1 if bias_phase < math.pi else -1

    def phase(power):
        if power is None or not fit.amplitude:
            return None
        cosine = max(-1.0, min(1.0, 2 * (power - fit.offset) / fit.amplitude - 1))
        return sign * math.acos(cosine)

    phases = [phase(power) for power in powers]
    start = next((value for value in phases if value is not None), None)
    return [None if value is None or start is None else value - start for value in phases]


def path_corrected(points, reference_points):
    """Quadrature powers divided by the reference sweep's change from its first point."""
    if not reference_points:
        return [point.power for point in points]
    start = reference_points[0].power
    corrected = []
    for point, reference in zip(points, reference_points):
        if point.power is None or not start or not reference.power or reference.power <= 0:
            corrected.append(None)
        else:
            corrected.append(point.power * start / reference.power)
    return corrected


def analyse_crosstalk(aggressor, probe, bias_voltage, victim_fit, bias_x, points, drive, x_unit,
                      reference_points=()):
    shifts = phase_shift_from_power(victim_fit, bias_x, path_corrected(points, reference_points))
    pairs = [(x, shift) for x, shift in zip(drive, shifts) if shift is not None]
    slope = None
    if len(pairs) >= 2 and max(x for x, _ in pairs) > min(x for x, _ in pairs):
        _, slope, _ = _linear_fit([x for x, _ in pairs], [shift for _, shift in pairs])
    kappa = None
    if slope is not None and x_unit == victim_fit.x_unit:
        kappa = slope / victim_fit.phase_per_x
    return CrosstalkSweep(aggressor, probe.name, probe, bias_voltage, victim_fit, tuple(points), tuple(drive),
                          x_unit, tuple(shifts), slope, kappa, tuple(reference_points))


def quadrature_point(sweep: MZISweep, xs) -> SweepPoint:
    """The swept point closest to the steepest part of the fitted response, lowest voltage first."""
    fit = sweep.fit
    if fit is None:
        raise ValueError(f"{sweep.name}: its response could not be fitted, so it cannot be biased at quadrature")
    scored = [(abs(math.cos(fit.phase(x))), point) for x, point in zip(xs, sweep.points) if point.power is not None]
    best = min(score for score, _ in scored)
    return min((point for score, point in scored if score <= max(best, 0.2)), key=lambda point: abs(point.voltage))


def background_voltage(controller, name, crossed, found=None) -> float:
    """The calibrated voltage, from this run first; an uncalibrated cross stays at 0 V.

    A calibrated extreme leaves the MZI least sensitive to stray heat.
    """
    if found and name in found:
        return (found[name].cross if crossed else found[name].bar).voltage
    voltage = controller.settings["calibration"][name]["cross" if crossed else "straight"]
    if voltage is None and not crossed:
        raise ValueError(f"{name} must be calibrated before it can be held at bar")
    return 0.0 if voltage is None else voltage


def apply_background(controller, probe, found=None) -> dict[str, float]:
    voltages = {name: background_voltage(controller, name, crossed, found) for name, crossed in probe.background}
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
            detector.prepare(probe)
            apply_background(controller, probe, results)
            points = sweep(controller, detector, probe, probe.name, voltages, settle_s,
                           on_point, stop_event, sleep)
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

    def __post_init__(self):
        self.victims = {} if self.victims is None else self.victims
        self.pairs = {} if self.pairs is None else self.pairs
        self.screens = {} if self.screens is None else self.screens

    @property
    def static_pairs(self) -> set[tuple[str, str]]:
        """Pairs the pretest saw no movement for, so they were not swept."""
        return {key for key, screen in self.screens.items() if not screen.moved}


def run_crosstalk(controller, detector, network, groups, voltages, settle_s, path_reference=True, run=None,
                  on_step=None, on_point=None, on_victim=None, on_pair=None, stop_event=None,
                  sleep=time.sleep, screen_min_phase_rad=None, on_screen=None) -> CrosstalkRun:
    """For each victim probe: sweep the victim, bias it at quadrature, then sweep each aggressor.

    With ``path_reference`` each aggressor is swept twice, first with the
    victim at its bright extreme, to remove heating of the rest of the path.
    With ``screen_min_phase_rad`` each aggressor is first swung between its
    limits a few times; one that moves the victim by less is recorded in
    ``run.screens`` and not swept.
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
            victim = extract_calibration(probe, points, xs, detector.unit, units[0], 0.0)
            run.victims[probe.name, probe.input, probe.output] = victim
            if on_victim is not None:
                on_victim(victim)
            bias = quadrature_point(victim, xs)
            bias_x = heater_drive(controller, probe.name, bias.voltage)[0]
            bright = victim.cross if probe.bright_crossed else victim.bar
            for aggressor in group.aggressors:
                if screen_min_phase_rad is not None:
                    screen = screen_aggressor(controller, detector, probe, victim.fit, aggressor, voltages[0],
                                              voltages[-1], bias.voltage, settle_s,
                                              bright.voltage if path_reference else None,
                                              screen_min_phase_rad, on_point, stop_event, sleep)
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
                result = analyse_crosstalk(aggressor, probe, bias.voltage, victim.fit, bias_x, points,
                                           drive, units[0], reference)
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
    from matplotlib.figure import Figure

    for name, item in results.items():
        figure = Figure(figsize=(7, 4))
        axis = figure.add_subplot(111)
        values, label = _plot_values(item.points, item.unit)
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


def save_crosstalk(folder: Path, context: dict, run: CrosstalkRun, network) -> list[str]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    matrix = kappa_matrix(network, run.pairs)
    static = run.static_pairs
    record = {**context,
              "kappa_definition": "victim-equivalent heater drive per aggressor heater drive (phase slope ratio)",
              "kappa": matrix,
              "pairs": [{"aggressor": item.aggressor, **_probe_record(item.probe), "victim": item.victim,
                         "bias_voltage_v": item.bias_voltage, "x_unit": item.x_unit,
                         "slope_rad_per_x": item.slope_rad_per_x, "kappa": item.kappa,
                         "max_phase_shift_rad": max((abs(value) for value in item.phase_shift if value is not None),
                                                    default=None)}
                        for item in run.pairs.values()],
              "pretest": [_screen_record(item) for item in run.screens.values()],
              "static_pairs": [{"aggressor": aggressor, "victim": victim} for aggressor, victim in sorted(static)],
              "victims": [{**_probe_record(item.probe), "fit": _fit_record(item.fit),
                           "extinction_db": None if math.isinf(item.extinction_db) else item.extinction_db}
                          for item in run.victims.values()]}
    (folder / "crosstalk_matrix.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with (folder / "crosstalk_matrix.csv").open("w", encoding="utf-8", newline="") as file:
        file.writelines(_header(context, "Thermal crosstalk matrix"))
        file.write("# kappa: rows are aggressors, columns victims; blank where not measured, "
                   "n.s. where the pretest saw no movement\n")
        writer = csv.writer(file)
        writer.writerow(["aggressor"] + list(network.switches))
        for aggressor, row in matrix.items():
            writer.writerow([aggressor] + ["n.s." if (aggressor, victim) in static else
                                           "" if value is None else f"{value:.6g}" for victim, value in row.items()])
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

    figure = Figure(figsize=(7, 6))
    draw_kappa_heatmap(figure.add_subplot(111), network, matrix, static)
    figure.tight_layout()
    figure.savefig(folder / "crosstalk_matrix.png", dpi=150)
    files.append("crosstalk_matrix.png")
    victims = sorted({item.victim for item in run.pairs.values()})
    for victim in victims:
        figure = Figure(figsize=(7, 4))
        draw_phase_shifts(figure.add_subplot(111), victim,
                          [item for item in run.pairs.values() if item.victim == victim])
        figure.tight_layout()
        filename = f"{victim.replace('.', '_')}_crosstalk.png"
        figure.savefig(folder / filename, dpi=150)
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


def draw_kappa_heatmap(axis, network, matrix, static=()):
    """κ in percent; pairs the pretest found static are marked n.s."""
    names = list(network.switches)
    values = [[float("nan") if matrix[aggressor][victim] is None else matrix[aggressor][victim] * 100
               for victim in names] for aggressor in names]
    finite = [abs(value) for row in values for value in row if math.isfinite(value)]
    limit = max(finite, default=1.0) or 1.0
    image = axis.imshow(values, cmap="coolwarm", vmin=-limit, vmax=limit)
    axis.set_xticks(range(len(names)), names, rotation=90)
    axis.set_yticks(range(len(names)), names)
    axis.set(xlabel="Victim", ylabel="Aggressor", title="Thermal crosstalk κ [%]")
    for row, line in enumerate(values):
        for column, value in enumerate(line):
            if math.isfinite(value):
                axis.text(column, row, f"{value:.1f}", ha="center", va="center", fontsize=6)
            elif (names[row], names[column]) in static:
                axis.text(column, row, "n.s.", ha="center", va="center", fontsize=5, color="gray")
    axis.figure.colorbar(image, ax=axis, label="κ [%]")


def draw_phase_shifts(axis, victim, items):
    for item in items:
        axis.plot(item.drive, [float("nan") if value is None else value for value in item.phase_shift], ".-",
                  label=f"{item.aggressor}" + ("" if item.kappa is None else f" κ={item.kappa * 100:.2f}%"))
    unit = items[0].x_unit if items else "mW"
    axis.set(xlabel=f"Aggressor heater drive [{unit}]", ylabel="Victim phase shift [rad]",
             title=f"{victim}: thermal crosstalk at quadrature")
    axis.grid(True)
    if items:
        axis.legend(fontsize=7, ncol=2)
