"""Nudge the MZI voltages on lit paths toward their best nearby setting, read with one power meter.

Each MZI on a lit path is tuned on its own, either for the least light leaking out of
its unused port (read at the output that leak reaches) or, when the meter cannot
reach that output, for the most light at the routed output. The leak is the sharper
measure: it falls as sin² of the phase error while the routed power only loses cos².
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from benes.measurements.laser_calibration import _check_stop, _wait

OBJECTIVE_LEAK, OBJECTIVE_THROUGH = "leak", "through"
# Half-width of the three-point bracket, and the narrowest one before an MZI counts as done.
# The board resolves 0.01 V.
START_STEP_V = 0.15
MIN_STEP_V = 0.03
# How far from its starting voltage one MZI may move: the search stays near the configuration.
MAX_OFFSET_V = 0.5
# A move counts only when it improves the power by more than this share, so noise cannot walk it.
TOLERANCE = {OBJECTIVE_LEAK: 0.05, OBJECTIVE_THROUGH: 0.005}
MAX_READS_PER_SWITCH = 16
# Heaters settle within milliseconds; the Arduino read itself averages much longer.
SETTLE_S = 0.03
# The search stays inside the board's default range so it never stops for a confirmation.
LOW_V, HIGH_V = 0.0, 5.0


@dataclass(frozen=True)
class SwitchOptimum:
    name: str
    source: int
    # Output the meter read while this MZI was tuned: its leak output or the routed one.
    output: int
    objective: str
    start_v: float
    best_v: float
    start_mw: float
    best_mw: float
    reads: int

    @property
    def moved(self):
        return abs(self.best_v - self.start_v) >= 0.005

    @property
    def gain_db(self):
        """Improvement in dB: less leak or more routed power are both positive."""
        if self.start_mw <= 0 or self.best_mw <= 0:
            return None
        ratio = 10 * math.log10(self.best_mw / self.start_mw)
        return -ratio if self.objective == OBJECTIVE_LEAK else ratio


@dataclass(frozen=True)
class PathOptimum:
    """Routed power of one lit input before and after its MZIs were tuned."""
    source: int
    output: int
    before_mw: float | None
    after_mw: float | None
    switches: tuple[SwitchOptimum, ...]

    @property
    def gain_db(self):
        if not self.before_mw or not self.after_mw or self.before_mw <= 0 or self.after_mw <= 0:
            return None
        return 10 * math.log10(self.after_mw / self.before_mw)


def leak_output(network, source, name, states=None):
    """0-based output reached by light of ``source`` leaving ``name`` through its unused port."""
    states = network.states if states is None else states
    row = source
    for stage in range(len(network.pairs)):
        here = network.at.get((stage, row))
        crossed = states[here] if here else False
        if here == name:
            crossed = not crossed
        row = network.step(stage, row, crossed)
        if here == name:
            for later in range(stage + 1, len(network.pairs)):
                other = network.at.get((later, row))
                row = network.step(later, row, states[other] if other else False)
            return row
    raise ValueError(f"{name} is not on the path of input {source + 1}")


def optimization_plan(network, sources, states=None):
    """(input, routed output, ((MZI, its leak output), ...)) per source; each MZI is tuned once.

    Each MZI carries two inputs in a permutation; both want it fully cross or bar,
    so the first lit path through it tunes it for the other as well.
    """
    mapping = network.mapping(states)
    taken = set()
    plan = []
    for source in sources:
        names = tuple(name for name, _ in network.trace(source, states).states if name not in taken)
        taken.update(names)
        plan.append((source, mapping[source],
                     tuple((name, leak_output(network, source, name, states)) for name in names)))
    return tuple(plan)


def optimize_switch(drive, read, name, start_v, *, minimize=False, low_v=LOW_V, high_v=HIGH_V,
                    step_v=START_STEP_V, min_step_v=MIN_STEP_V, max_offset_v=MAX_OFFSET_V, tolerance=None,
                    max_reads=MAX_READS_PER_SWITCH, allowed=None, on_read=None):
    """Bracket the voltage by ±step, jump to the parabola's vertex, and narrow when nothing improves.

    ``drive(name, volts)`` sets and settles the heater, ``read()`` returns mW (None below the floor).
    Returns (best volts, start mW, best mW, reads); the heater is left at the best voltage.
    """
    if tolerance is None:
        tolerance = TOLERANCE[OBJECTIVE_LEAK if minimize else OBJECTIVE_THROUGH]
    low = max(low_v, start_v - max_offset_v)
    high = min(high_v, start_v + max_offset_v)
    sign = -1.0 if minimize else 1.0
    reads = 0
    seen = {}

    def power_at(voltage):
        nonlocal reads
        voltage = round(voltage, 2)
        drive(name, voltage)
        value = read()
        reads += 1
        value = 0.0 if value is None or not math.isfinite(value) else value
        seen[voltage] = value
        if on_read is not None:
            on_read(name, voltage, value)
        return value

    def usable(voltage):
        voltage = round(voltage, 2)
        return (low - 1e-9 <= voltage <= high + 1e-9
                and (allowed is None or allowed(name, voltage)))

    def improves(power, than):
        if minimize:
            return power < than * (1 - tolerance)
        return power > than * (1 + tolerance) or (than <= 0 < power)

    best_v = round(start_v, 2)
    start_mw = best_mw = power_at(best_v)
    while step_v >= min_step_v - 1e-9 and reads + 3 <= max_reads:
        sides = {}
        for offset in (-step_v, step_v):
            if usable(best_v + offset):
                sides[offset] = power_at(best_v + offset)
        candidates = {round(best_v + offset, 2): power for offset, power in sides.items()}
        if len(sides) == 2:
            # Vertex of the parabola through the three points, in units of the score sign·power.
            minus, plus, middle = sign * sides[-step_v], sign * sides[step_v], sign * best_mw
            curvature = minus - 2 * middle + plus
            if curvature < 0:
                shift = step_v * (minus - plus) / (2 * curvature)
                shift = max(-2 * step_v, min(2 * step_v, shift))
                vertex = round(best_v + shift, 2)
                if vertex not in candidates and vertex != best_v and usable(vertex):
                    candidates[vertex] = power_at(vertex)
        chosen = (max if not minimize else min)(candidates, key=candidates.get, default=None)
        if chosen is not None and improves(candidates[chosen], best_mw):
            best_v, best_mw = chosen, candidates[chosen]
        else:
            step_v /= 2
    drive(name, best_v)
    return best_v, start_mw, best_mw, reads


def optimize_paths(controller, plan, read_power, *, objective=OBJECTIVE_LEAK, light=None, select_output=None,
                   settle_s=SETTLE_S, allowed=None, on_switch=None, on_read=None, stop_event=None,
                   sleep=time.sleep, results=None, **search):
    """Tune each planned path in turn; each MZI is left at its best voltage. Returns ``PathOptimum``s.

    ``light(source)`` lights only that input and ``select_output(output)`` brings an output to
    the meter, both 0-based; the leak objective needs ``select_output``. Finished MZIs are
    appended to ``results`` as ``SwitchOptimum`` while the run goes, so a stop keeps them.
    """
    if objective == OBJECTIVE_LEAK and select_output is None:
        raise ValueError("The leak objective needs the meter to reach every output")
    finished = [] if results is None else results
    paths = []

    def drive(name, voltage):
        _check_stop(stop_event)
        controller.drive_voltage(name, voltage)
        _wait(settle_s, stop_event, sleep)

    def read():
        _check_stop(stop_event)
        return read_power()

    def routed(output):
        if select_output is None:
            return None
        select_output(output)
        _wait(settle_s, stop_event, sleep)
        return read()

    for source, output, switches in plan:
        if not switches:
            continue
        if light is not None:
            light(source)
        before = routed(output)
        tuned = []
        for name, leak in switches:
            start_v = controller.applied_voltages.get(name)
            if start_v is None:
                continue
            target = leak if objective == OBJECTIVE_LEAK else output
            if objective == OBJECTIVE_LEAK:
                select_output(leak)
            try:
                best_v, start_mw, best_mw, reads = optimize_switch(
                    drive, read, name, start_v, minimize=objective == OBJECTIVE_LEAK, allowed=allowed,
                    on_read=None if on_read is None else
                    (lambda n, v, p, s=source, o=target: on_read(s, o, n, v, p)), **search)
            except BaseException:
                # Stopped or failed mid-search: the heater goes back to where it started.
                try:
                    controller.drive_voltage(name, start_v)
                except Exception:
                    pass
                raise
            item = SwitchOptimum(name, source, target, objective, start_v, best_v, start_mw, best_mw, reads)
            tuned.append(item)
            finished.append(item)
            if on_switch is not None:
                on_switch(item)
        after = routed(output) if objective == OBJECTIVE_LEAK else tuned[-1].best_mw if tuned else before
        if before is None and tuned:
            before = tuned[0].start_mw
        paths.append(PathOptimum(source, output, before, after, tuple(tuned)))
    return paths
