"""Power Meter (New): iterative MZI calibration that does not assume 0 V is cross.

Every MZI is a single-heater MZI whose phase at 0 V is unknown. The power
meter reads one lit input at one JDSU-selected output, so the calibration
splits into two problems:

* Voltages. Each MZI is swept while every other MZI holds a background
  configuration chosen from the topology so that light leaking out of the
  probe route cannot come back to the read output after one or two leaks
  (``isolated_probe``). With the other MZIs near their extremes the sweep is
  a clean sinusoid whose extremes are the MZI's physical cross and bar
  voltages. The other MZIs are only near their extremes once they have been
  calibrated, so the sweeps are repeated in rounds; each round starts from
  the voltages of the one before and the change shrinks quickly.

* Labels. A sweep finds the two extreme voltages reliably, but if another
  MZI on the route is in the opposite state, light can still reach the read
  output through the other arm, and the bright extreme is then the opposite
  state. Which of the two voltages is cross is therefore settled separately,
  for all twelve MZIs at once, by permutation tests (``resolve_labels``):
  command a configuration, light each input in turn, find the output it
  reaches, and keep the label assignments that predict what was measured.
  A single swapped label always changes the permutation; a compensating
  pair only cancels in some configurations, so a few adaptively chosen tests
  leave one assignment. An input whose brightest output is not clearly above
  the rest points at an MZI left part-way (about 50/50) and is reported.

All ports are 0-based here.
"""

from __future__ import annotations

import csv
import json
import math
import random
import time
from dataclasses import dataclass, field, replace
from itertools import product
from pathlib import Path

from benes.measurements.measurement_control import check_stop, wait
from benes.measurements.mzi_characterization import _session, heater_drive, require_ready, save_calibration, sweep
from benes.measurements.mzi_fit import (DEFAULT_MIN_EXTINCTION_DB, MZISweep, SweepPoint, extract_calibration,
                                        fit_mzi_response, fit_trusted, mw_to_dbm)
from benes.measurements.mzi_probing import Probe

# Rounds of sweeps; the run stops as soon as a round changes nothing.
DEFAULT_MAX_ROUNDS = 5
# A round is converged when no voltage moved by more than this [V].
DEFAULT_TOLERANCE_V = 0.05
# A permutation test trusts an input whose brightest output beats the next by this much [dB].
DEFAULT_LABEL_MARGIN_DB = 10.0
# A sweep below this extinction suggests a neighbour still part-way [dB].
LOW_CONTRAST_DB = 10.0
# At most this many permutation tests after each round.
DEFAULT_MAX_LABEL_TESTS = 6
# The route check found the Newport lasers need this long after switching [s].
LASER_CHANGE_SETTLE_S = 5.0
# A flat sweep is retried where a coarse scan of this many heater settings sees
# at least this swing [dB] at some lit input and output.
DISCOVERY_POINTS = 8
DISCOVERY_MIN_DB = 6.0
# A fitted extreme this close below the sweep start [rad] is taken at the start.
EXTREME_CLAMP_RAD = 0.1
# Candidate tests scored per label-test choice, at most.
_TEST_SCORE_BUDGET = 2_000_000


# --------------------------------------------------------------------------- topology


def leak_paths(network, states, name, source, max_leaks=2):
    """Every path from ``source`` with at most ``max_leaks`` held MZIs leaking.

    ``name`` is free (both states, no leak cost); every other MZI follows
    ``states`` or, at the cost of one leak, the other state. Returns
    (output, leaks, state of ``name`` or None if not passed).
    """
    found = []
    stages = len(network.pairs)

    def visit(stage, row, leaks, swept):
        if stage == stages:
            found.append((row, leaks, swept))
            return
        switch = network.at.get((stage, row))
        if switch is None:
            visit(stage + 1, network.step(stage, row, False), leaks, swept)
        elif switch == name:
            for crossed in (True, False):
                visit(stage + 1, network.step(stage, row, crossed), leaks, crossed)
        else:
            visit(stage + 1, network.step(stage, row, states[switch]), leaks, swept)
            if leaks < max_leaks:
                visit(stage + 1, network.step(stage, row, not states[switch]), leaks + 1, swept)

    visit(0, source, 0, None)
    return found


@dataclass(frozen=True)
class IsolatedProbe:
    probe: Probe
    # Paths reaching the read output after exactly one and exactly two leaks.
    first_order: int
    second_order: int


# Topology searches depend only on the wiring, so they are kept between runs.
_PROBE_CACHE: dict = {}
_TABLE_CACHE: dict = {}


def _isolated(network, name, fixed, inputs, outputs):
    fixed = dict(fixed)
    others = [other for other in network.switches if other != name]
    free = [other for other in others if other not in fixed]
    best = None
    for bits in product((True, False), repeat=len(free)):
        states = {**fixed, **dict(zip(free, bits))}
        bars = sum(not crossed for crossed in states.values())
        for source in inputs:
            paths = leak_paths(network, states, name, source)
            ends = {crossed: row for row, leaks, crossed in paths if leaks == 0 and crossed is not None}
            if len(ends) != 2 or ends[True] == ends[False]:
                continue
            for bright_crossed in (True, False):
                output = ends[bright_crossed]
                if output not in outputs:
                    continue
                first = sum(1 for row, leaks, _ in paths if row == output and leaks == 1)
                second = sum(1 for row, leaks, _ in paths if row == output and leaks == 2)
                key = (first, second, bars, source, output, not bright_crossed)
                if best is None or key < best[0]:
                    best = (key, states, source, output, bright_crossed)
    if best is None:
        return None
    key, states, source, output, bright_crossed = best
    background = tuple((other, states[other]) for other in others)
    dark = network.mapping({**states, name: not bright_crossed})
    probe = Probe(name, source, output, bright_crossed, background, dark.index(output))
    return IsolatedProbe(probe, key[0], key[1])


def isolated_probe(network, name, fixed=None, inputs=None, outputs=None) -> IsolatedProbe | None:
    """The probe of ``name`` whose read output is hardest for leaked light to reach.

    Every other MZI not in ``fixed`` may take either state. Probes are ranked
    by the number of paths that reach the read output after one leak, then
    after two, then by fewest MZIs held at bar.
    """
    if name not in network.switches:
        raise ValueError(f"Unknown MZI: {name}")
    fixed = tuple(sorted((other, bool(crossed)) for other, crossed in (fixed or {}).items() if other != name))
    inputs = tuple(range(network.size) if inputs is None else inputs)
    outputs = frozenset(range(network.size) if outputs is None else outputs)
    key = (network.pairs, network.links, name, fixed, inputs, outputs)
    if key not in _PROBE_CACHE:
        _PROBE_CACHE[key] = _isolated(network, name, fixed, inputs, outputs)
    return _PROBE_CACHE[key]


# --------------------------------------------------------------------------- sweep extraction


def _first_extreme(fit, target, start, stop):
    """Lowest drive in [start, stop] where the fitted phase is ``target`` mod 2π, else None."""
    if not fit.x_pi or fit.x_pi <= 0:
        return None
    lowest = start - EXTREME_CLAMP_RAD * fit.x_pi / math.pi
    turns = math.ceil((fit.phase(lowest) - target) / (2 * math.pi))
    x = (target + 2 * math.pi * turns - fit.phase0) * fit.x_pi / math.pi
    x = max(x, start)
    return x if x <= stop else None


def voltage_for_drive(controller, name, x, low, high) -> float:
    """Voltage in [low, high] whose heater drive is ``x``, to the board's 0.01 V."""
    drive = lambda voltage: heater_drive(controller, name, voltage)[0]
    if x <= drive(low):
        return round(low, 2)
    if x >= drive(high):
        return round(high, 2)
    for _ in range(60):
        middle = (low + high) / 2
        if drive(middle) < x:
            low = middle
        else:
            high = middle
    return round((low + high) / 2, 2)


def extract_extremes(controller, probe, points, voltages, unit="mW") -> MZISweep:
    """Cross and bar from the fitted fringe, at the fitted extremes rather than the nearest step.

    Falls back to the measured extremes when the fit is not trusted or misses
    an extreme. Raises ValueError for a sweep that barely changes.
    """
    points = tuple(points)
    drives = [heater_drive(controller, probe.name, voltage) for voltage in voltages]
    xs, x_unit = [x for x, _ in drives], drives[0][1]
    powers = [point.power for point in points]
    measured = [power for power in powers if power is not None and math.isfinite(power)]
    if len(measured) < 4:
        raise ValueError(f"{probe.name}: fewer than four readings")
    high, low = max(measured), min(measured)
    extinction = math.inf if low <= 0 < high else 10 * math.log10(high / low) if low > 0 else 0.0
    if extinction < DEFAULT_MIN_EXTINCTION_DB:
        raise ValueError(f"{probe.name}: only {extinction:.1f} dB change at output {probe.output + 1}")
    fit = fit_mzi_response(xs, powers, x_unit)
    if fit_trusted(fit):
        start, stop = min(xs), max(xs)
        bright_x, dark_x = (_first_extreme(fit, target, start, stop) for target in (0.0, math.pi))
        if bright_x is not None and dark_x is not None:
            low_v, high_v = min(voltages), max(voltages)
            bright = SweepPoint(voltage_for_drive(controller, probe.name, bright_x, low_v, high_v),
                                fit.model(bright_x))
            dark = SweepPoint(voltage_for_drive(controller, probe.name, dark_x, low_v, high_v), fit.model(dark_x))
            cross, bar = (bright, dark) if probe.bright_crossed else (dark, bright)
            return MZISweep(probe, points, cross, bar, extinction, fit, unit)
    return extract_calibration(probe, points, xs, unit, x_unit)


# --------------------------------------------------------------------------- label tests


@dataclass(frozen=True)
class LabelTest:
    """One commanded configuration, every input lit in turn, every output read."""

    states: dict
    # powers[input][output] in mW, None when unreadable.
    powers: tuple
    # Output each input reaches when the labels are right.
    expected: tuple
    # Brightest output per input, None when no output reads.
    brightest: tuple
    # Brightest over the next output, per input [dB]; None without a second reading.
    margins_db: tuple
    margin_db: float

    def clean(self, source) -> bool:
        margin = self.margins_db[source]
        return self.brightest[source] is not None and margin is not None and margin >= self.margin_db

    @property
    def observed(self) -> tuple:
        """Output per input, None where the input is not clearly routed."""
        return tuple(self.brightest[source] if self.clean(source) else None for source in range(len(self.powers)))


@dataclass(frozen=True)
class LabelResult:
    tests: tuple[LabelTest, ...]
    # MZIs whose cross and bar voltages were the wrong way round.
    swapped: tuple[str, ...]
    # Label assignments still consistent with the tests (1 when fully settled).
    remaining: int
    # Mismatched input readings of the best assignment; 0 when consistent.
    mismatches: int
    # (test index, input) whose light was not clearly at one output.
    ambiguous: tuple[tuple[int, int], ...]
    # MZIs on the routes of the ambiguous inputs: likely left part-way.
    suspects: tuple[str, ...]

    @property
    def consistent(self) -> bool:
        return self.mismatches == 0


def route_table(network):
    """(names, mapping of every configuration); bit k of a configuration set means names[k] is cross."""
    key = (network.pairs, network.links)
    if key not in _TABLE_CACHE:
        names = tuple(network.switches)
        table = [network.mapping({name: bool(bits >> index & 1) for index, name in enumerate(names)})
                 for bits in range(1 << len(names))]
        _TABLE_CACHE[key] = names, table
    return _TABLE_CACHE[key]


def evaluate_label_test(states, powers, expected, margin_db) -> LabelTest:
    brightest, margins = [], []
    for row in powers:
        dbm = sorted(((mw_to_dbm(power), output) for output, power in enumerate(row)
                      if power is not None and math.isfinite(power) and power > 0), reverse=True)
        brightest.append(dbm[0][1] if dbm else None)
        margins.append(dbm[0][0] - dbm[1][0] if len(dbm) >= 2 else (math.inf if dbm else None))
    return LabelTest(dict(states), tuple(tuple(row) for row in powers), tuple(expected), tuple(brightest),
                     tuple(margins), margin_db)


def choose_label_test(table, hypotheses, rng=None):
    """Configuration bits that best split ``hypotheses``, or None when none splits them.

    Scored by the largest group of hypotheses predicting the same
    permutation, then by the number of groups.
    """
    rng = rng or random.Random(0)
    candidates = range(len(table))
    budget = max(1, _TEST_SCORE_BUDGET // max(1, len(hypotheses)))
    if len(table) > budget:
        candidates = sorted(rng.sample(range(len(table)), budget))
    best = None
    for config in candidates:
        groups = {}
        for hypothesis in hypotheses:
            key = table[config ^ hypothesis]
            groups[key] = groups.get(key, 0) + 1
        score = (max(groups.values()), -len(groups), config)
        if best is None or score < best[0]:
            best = (score, config)
    if best is None or best[0][0] == len(hypotheses):
        return None
    return best[1]


def resolve_labels(network, measure, margin_db=DEFAULT_LABEL_MARGIN_DB, max_tests=DEFAULT_MAX_LABEL_TESTS,
                   on_test=None, stop_event=None) -> LabelResult:
    """Find which MZIs have cross and bar swapped.

    ``measure(states)`` applies the commanded ``states`` with the current
    labels and returns powers[input][output] in mW. The first test is
    all-cross; each further test is the configuration that best splits the
    assignments still consistent with every clean reading.
    """
    names, table = route_table(network)
    count = len(table)
    mismatches = [0] * count
    tests, ambiguous = [], []
    config = count - 1  # every MZI cross
    rng = random.Random(0)
    while True:
        check_stop(stop_event)
        states = {name: bool(config >> index & 1) for index, name in enumerate(names)}
        powers = measure(states)
        test = evaluate_label_test(states, powers, table[config], margin_db)
        tests.append(test)
        for source, output in enumerate(test.observed):
            if output is None:
                ambiguous.append((len(tests) - 1, source))
                continue
            for hypothesis in range(count):
                if table[config ^ hypothesis][source] != output:
                    mismatches[hypothesis] += 1
        if on_test is not None:
            on_test(test, len(tests))
        fewest = min(mismatches)
        hypotheses = [hypothesis for hypothesis in range(count) if mismatches[hypothesis] == fewest]
        if len(hypotheses) == 1 or len(tests) >= max_tests:
            break
        following = choose_label_test(table, hypotheses, rng)
        if following is None:
            break
        config = following
    # Indistinguishable assignments route every configuration alike; keep the fewest swaps.
    best = min(hypotheses, key=lambda hypothesis: (bin(hypothesis).count("1"), hypothesis))
    swapped = tuple(name for index, name in enumerate(names) if best >> index & 1)
    suspects = _suspects(network, names, tests, ambiguous, best)
    return LabelResult(tuple(tests), swapped, len(hypotheses), fewest, tuple(ambiguous), suspects)


def _suspects(network, names, tests, ambiguous, hypothesis):
    """MZIs on every ambiguous input's physical route, or on any of them when they share none."""
    routes = []
    for index, source in ambiguous:
        # The physical state is the commanded one, flipped where the label is swapped.
        states = {name: tests[index].states[name] != bool(hypothesis >> position & 1)
                  for position, name in enumerate(names)}
        routes.append({name for name, _ in network.trace(source, states).states})
    if not routes:
        return ()
    shared = set.intersection(*routes) or set.union(*routes)
    return tuple(name for name in names if name in shared)


def measure_configuration(controller, detector, network, estimates, states, settle_s, stop_event=None,
                          sleep=time.sleep, reverse=False, on_output=None):
    """Apply ``states`` with the estimated voltages; light each input and read all six outputs."""
    for name in network.switches:
        estimate = estimates.get(name)
        voltage = 0.0 if estimate is None else estimate["cross" if states[name] else "bar"]
        if controller.applied_voltages.get(name) != voltage:
            controller.drive_voltage(name, voltage)
    wait(settle_s, stop_event, sleep)
    powers = [None] * network.size
    sources = range(network.size - 1, -1, -1) if reverse else range(network.size)
    for source in sources:
        check_stop(stop_event)
        detector.lasers.light_only(source, minimum_settle_s=LASER_CHANGE_SETTLE_S)
        row = []
        for output in range(network.size):
            check_stop(stop_event)
            detector.switch.select_output(output + 1, extra_settle_s=0)
            power = detector.read_power()
            if power is not None and (not math.isfinite(power) or power < 0):
                raise ValueError(f"Invalid optical power at output {output + 1}")
            row.append(power)
            if on_output is not None:
                on_output(source, output, power)
        powers[source] = row
    return powers


# --------------------------------------------------------------------------- the run


@dataclass
class PMCalibrationRun:
    """Filled as the run goes, so a stopped run keeps what was measured."""

    # One {name: MZISweep} per round, as measured.
    rounds: list = field(default_factory=list)
    # One LabelResult per round that ran label tests, and that round's number.
    labels: list = field(default_factory=list)
    label_rounds: list = field(default_factory=list)
    # name -> latest MZISweep, with cross and bar swapped where the labels said so.
    results: dict = field(default_factory=dict)
    # name -> {"cross": V, "bar": V}, the current best voltages.
    estimates: dict = field(default_factory=dict)
    # Largest voltage change of each completed round [V]; inf when an MZI had no earlier value.
    changes: list = field(default_factory=list)
    # (round, name, why) for sweeps that failed or need attention.
    warnings: list = field(default_factory=list)
    converged: bool = False

    def swap(self, name) -> None:
        """Exchange ``name``'s cross and bar: its bright extreme was the other state."""
        estimate = self.estimates[name]
        estimate["cross"], estimate["bar"] = estimate["bar"], estimate["cross"]
        item = self.results.get(name)
        if item is not None:
            probe = replace(item.probe, bright_crossed=not item.probe.bright_crossed)
            self.results[name] = MZISweep(probe, item.points, item.bar, item.cross, item.extinction_db,
                                          item.fit, item.unit)


def _change(previous, current, names):
    largest = 0.0
    for name in names:
        before, after = previous.get(name), current.get(name)
        if after is None:
            continue
        if before is None:
            return math.inf
        largest = max(largest, abs(before["cross"] - after["cross"]), abs(before["bar"] - after["bar"]))
    return largest


def run_pm_calibration(controller, detector, network, voltages, settle_s, start=None, names=None,
                       max_rounds=DEFAULT_MAX_ROUNDS, tolerance_v=DEFAULT_TOLERANCE_V,
                       label_margin_db=DEFAULT_LABEL_MARGIN_DB, max_label_tests=DEFAULT_MAX_LABEL_TESTS,
                       label_tests=True, save=True, run=None, on_round=None, on_step=None, on_point=None,
                       on_result=None, on_label_test=None, on_labels=None, on_label_output=None,
                       stop_event=None, sleep=time.sleep) -> PMCalibrationRun:
    """Sweep in rounds with isolated backgrounds, settle the labels after each round, repeat until stable.

    ``start`` gives {name: {"cross": V, "bar": V}} to begin from; an MZI
    without both is held at 0 V and taken as cross until it is swept.
    ``detector`` is a ``PowerMeterDetector`` read through the JDSU. With
    ``save`` the voltages are stored after every round's label tests.
    """
    require_ready(controller, network, voltages, settle_s)
    if getattr(detector, "switch", None) is None:
        raise ValueError("Power Meter (New) reads every output: put the meter on the JDSU common port")
    if not 1 <= max_rounds <= 10:
        raise ValueError("Use 1 to 10 rounds")
    if not math.isfinite(tolerance_v) or tolerance_v <= 0:
        raise ValueError("The convergence tolerance must be a positive voltage")
    names = list(network.switches if names is None else names)
    run = PMCalibrationRun() if run is None else run
    run.estimates.update({name: {"cross": value["cross"], "bar": value["bar"]}
                          for name, value in (start or {}).items()
                          if value.get("cross") is not None and value.get("bar") is not None})
    stage = {name: network.switches[name][0] for name in network.switches}
    inputs, outputs = detector.inputs, detector.outputs

    def background_voltages(probe):
        voltages_for = {}
        for other, crossed in probe.background:
            estimate = run.estimates.get(other)
            if estimate is None:
                if not crossed:
                    raise ValueError(f"{other} has no voltages and cannot be held at bar")
                voltages_for[other] = 0.0
            else:
                voltages_for[other] = estimate["cross" if crossed else "bar"]
        return voltages_for

    def probe_for(name):
        unknown = {other: True for other in network.switches if other != name and other not in run.estimates}
        found = isolated_probe(network, name, unknown, inputs, outputs)
        if found is None:
            raise ValueError(f"No probe reaches {name} with this detection")
        return found.probe

    def discover(probe):
        """A lit input and read output where ``probe.name`` visibly switches the light, else None.

        Used when the planned probe shows no fringe: an uncalibrated
        neighbour is not where the plan assumed and sends the light
        elsewhere. A few heater settings, evenly spread in heater power, are
        read at every output for one input after another.
        """
        low, high = min(voltages), max(voltages)
        coarse = sorted({min(voltages, key=lambda voltage: abs(voltage - math.sqrt(
            low * low + step / (DISCOVERY_POINTS - 1) * (high * high - low * low))))
            for step in range(DISCOVERY_POINTS)})
        for source in [probe.input] + [other for other in inputs if other != probe.input]:
            check_stop(stop_event)
            detector.lasers.light_only(source, minimum_settle_s=LASER_CHANGE_SETTLE_S)
            readings = {output: [] for output in outputs}
            for voltage in coarse:
                controller.drive_voltage(probe.name, voltage)
                wait(settle_s, stop_event, sleep)
                for output in outputs:
                    check_stop(stop_event)
                    detector.switch.select_output(output + 1, extra_settle_s=0)
                    power = detector.read_power()
                    if power is not None and math.isfinite(power) and power > 0:
                        readings[output].append(power)
            swings = {output: 10 * math.log10(max(values) / min(values))
                      for output, values in readings.items() if len(values) >= 3}
            if swings:
                output = max(swings, key=lambda item: (swings[item], -item))
                if swings[output] >= DISCOVERY_MIN_DB:
                    # Which extreme is cross is unknown here; the permutation tests decide it.
                    return replace(probe, input=source, output=output, bright_crossed=True,
                                   other_input=probe.other_input)
        return None

    def measure_probe(probe):
        before = dict(controller.applied_voltages)
        background = background_voltages(probe)
        for other, voltage in background.items():
            if controller.applied_voltages.get(other) != voltage:
                controller.drive_voltage(other, voltage)
        detector.prepare(probe)
        if any(before.get(other) != voltage for other, voltage in background.items()):
            wait(max(settle_s, getattr(detector, "background_settle_s", 0.0)), stop_event, sleep)
        points = sweep(controller, detector, probe, probe.name, voltages, settle_s, on_point, stop_event, sleep)
        return extract_extremes(controller, probe, points, voltages, detector.unit)

    def sweep_one(probe, round_number, index, total):
        check_stop(stop_event)
        if on_step is not None:
            on_step(probe, index, total)
        try:
            result = measure_probe(probe)
        except ValueError as error:
            found = discover(probe)
            if found is None:
                run.warnings.append((round_number, probe.name, f"{error}; no input and output shows its fringe"))
                return None
            run.warnings.append((round_number, probe.name, f"{error}; swept through In {found.input + 1} → "
                                 f"Out {found.output + 1} instead"))
            if on_step is not None:
                on_step(found, index, total)
            try:
                result = measure_probe(found)
            except ValueError as retry_error:
                run.warnings.append((round_number, probe.name, str(retry_error)))
                return None
        if result.extinction_db < LOW_CONTRAST_DB:
            run.warnings.append((round_number, probe.name,
                                 f"only {result.extinction_db:.1f} dB extinction: a neighbour may be part-way"))
        run.estimates[probe.name] = {"cross": result.cross.voltage, "bar": result.bar.voltage}
        run.results[probe.name] = result
        if on_result is not None:
            on_result(result)
        return result

    order = {"reverse": False}

    def measure(states):
        # Alternate the input order so consecutive tests share their boundary laser.
        powers = measure_configuration(controller, detector, network, run.estimates, states, settle_s,
                                       stop_event, sleep, order["reverse"], on_label_output)
        order["reverse"] = not order["reverse"]
        return powers

    def body():
        for round_number in range(1, max_rounds + 1):
            check_stop(stop_event)
            previous = {name: dict(value) for name, value in run.estimates.items()}
            if on_round is not None:
                on_round(round_number, max_rounds)
            sweeps = {}
            run.rounds.append(sweeps)
            if all(name in run.estimates for name in network.switches):
                # Every probe is free to choose its background: order by laser to save switching.
                probes = sorted((probe_for(name) for name in names),
                                key=lambda probe: (probe.input, stage[probe.name], probe.name))
                for index, probe in enumerate(probes, start=1):
                    result = sweep_one(probe, round_number, index, len(probes))
                    if result is not None:
                        sweeps[probe.name] = result
            else:
                # Bootstrap: unknown MZIs stay at 0 V, so each probe is chosen when its turn comes.
                for index, name in enumerate(sorted(names, key=lambda item: (stage[item], item)), start=1):
                    result = sweep_one(probe_for(name), round_number, index, len(names))
                    if result is not None:
                        sweeps[name] = result
            swapped = ()
            labels = None
            if label_tests and all(name in run.estimates for name in network.switches):
                labels = resolve_labels(network, measure, label_margin_db, max_label_tests,
                                        on_label_test, stop_event)
                run.labels.append(labels)
                run.label_rounds.append(round_number)
                swapped = labels.swapped
                for name in swapped:
                    run.swap(name)
                if on_labels is not None:
                    on_labels(labels, round_number)
            elif label_tests:
                missing = [name for name in network.switches if name not in run.estimates]
                run.warnings.append((round_number, "labels", "no permutation tests: no voltages yet for "
                                     + ", ".join(missing)))
            if save:
                for name in names:
                    if name in run.estimates:
                        controller.set_calibration(name, run.estimates[name]["bar"], run.estimates[name]["cross"])
            change = _change(previous, run.estimates, names)
            run.changes.append(change)
            settled = labels is None or (labels.consistent and not labels.ambiguous)
            if change <= tolerance_v and not swapped and settled and all(name in sweeps for name in names):
                run.converged = True
                break
        return run

    return _session(controller, network, detector, body)


# --------------------------------------------------------------------------- saving


def _label_record(result: LabelResult, round_number):
    return {
        "round": round_number,
        "swapped": list(result.swapped),
        "remaining_assignments": result.remaining,
        "mismatched_readings": result.mismatches,
        "ambiguous": [{"test": index + 1, "input": source + 1} for index, source in result.ambiguous],
        "suspects_part_way": list(result.suspects),
        "tests": [{
            "test": number,
            "states": {name: "cross" if crossed else "bar" for name, crossed in test.states.items()},
            "expected_output_per_input": [output + 1 for output in test.expected],
            "brightest_output_per_input": [None if output is None else output + 1 for output in test.brightest],
            "margin_db_per_input": [None if margin is None or math.isinf(margin) else margin
                                    for margin in test.margins_db],
            "outputs_dbm_per_input": [[mw_to_dbm(power) for power in row] for row in test.powers],
        } for number, test in enumerate(result.tests, start=1)],
    }


def save_pm_calibration(folder, context, run: PMCalibrationRun) -> list[str]:
    """The usual calibration files for the final voltages, plus every round and permutation test."""
    folder = Path(folder)
    files = save_calibration(folder, context, run.results)
    record = {
        **context,
        "method": "Power Meter (New): isolated-background sweeps in rounds, labels from permutation tests",
        "converged": run.converged,
        "round_changes_v": [None if math.isinf(change) else change for change in run.changes],
        "final_voltages": run.estimates,
        "warnings": [{"round": number, "mzi": name, "message": message} for number, name, message in run.warnings],
        "rounds": [{name: {"input": item.probe.input + 1, "output": item.probe.output + 1,
                           "bright_state": "cross" if item.probe.bright_crossed else "bar",
                           "background_bar": list(item.probe.biased),
                           "cross_voltage_v": item.cross.voltage, "bar_voltage_v": item.bar.voltage,
                           "extinction_db": None if math.isinf(item.extinction_db) else item.extinction_db}
                    for name, item in sweeps.items()} for sweeps in run.rounds],
        "label_tests": [_label_record(result, number) for result, number in zip(run.labels, run.label_rounds)],
    }
    (folder / "pm_calibration.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    files.append("pm_calibration.json")
    with (folder / "pm_label_tests.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["label_round", "test", "input", "expected_output", "brightest_output", "margin_db",
                         "clean"] + [f"output_{port}_dbm" for port in range(1, 7)])
        for round_number, result in zip(run.label_rounds, run.labels):
            for number, test in enumerate(result.tests, start=1):
                for source, row in enumerate(test.powers):
                    margin = test.margins_db[source]
                    writer.writerow([round_number, number, source + 1, test.expected[source] + 1,
                                     "" if test.brightest[source] is None else test.brightest[source] + 1,
                                     "" if margin is None else "inf" if math.isinf(margin) else f"{margin:.2f}",
                                     int(test.clean(source)),
                                     *("" if mw_to_dbm(power) is None else f"{mw_to_dbm(power):.2f}"
                                       for power in row)])
    files.append("pm_label_tests.csv")
    return files
