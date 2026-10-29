"""Which MZI can be seen from which laser input at which detector output.

A probe lights one input and reads one output. The probed MZI decides
whether that input's light reaches the output: in one state it arrives
(bright), in the other it leaves elsewhere (dark). Every other MZI holds a
known background state: cross at 0 V (the assumed unbiased state), or its
calibrated bar voltage when a fixed detector needs that route.

Calibrated MZIs set to bar let a fixed detector see further, so calibration
bootstraps: it calibrates what is visible, then uses those MZIs to reach the
rest. With the detector behind the JDSU every MZI is visible at 0 V.

All ports are 0-based here.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from itertools import combinations

# Held MZIs that must leak before light through an aggressor can reach the detector.
# Each leak costs the MZI's extinction (about 20 dB in power, 10 % in field) on the
# field that interferes with the probed light; one leak still moves a quadrature
# reading by tenths of a radian.
DEFAULT_MIN_LEAK_ORDER = 2
# Past three leaks the interfering field is below 0.1 %; more isolation gains nothing.
ENOUGH_LEAK_ORDER = 3


@dataclass(frozen=True)
class Probe:
    name: str
    input: int
    output: int
    # The probed MZI's state that sends ``input`` to ``output``.
    bright_crossed: bool
    # Every other MZI's state, in network order.
    background: tuple[tuple[str, bool], ...]
    # The input whose light arrives at ``output`` in the dark state.
    other_input: int

    def states(self, crossed: bool) -> dict[str, bool]:
        return {**dict(self.background), self.name: crossed}

    @property
    def biased(self) -> tuple[str, ...]:
        """Background MZIs set to bar; they need a calibrated bar voltage."""
        return tuple(name for name, crossed in self.background if not crossed)

    def bright_path(self, network) -> tuple[str, ...]:
        """MZIs the probed light passes through when it reaches the output."""
        return tuple(name for name, _ in network.trace(self.input, self.states(self.bright_crossed)).states)
    def describe(self) -> str:
        text = (f"In {self.input + 1} → Out {self.output + 1}, bright when "
                f"{'cross' if self.bright_crossed else 'bar'}")
        if self.biased:
            text += f"; {', '.join(self.biased)} at bar"
        return text


@dataclass(frozen=True)
class CalibrationPlan:
    # In measurement order; a later step may hold earlier MZIs at bar.
    steps: tuple[Probe, ...]
    # MZI name -> why no probe reaches it.
    unreachable: dict[str, str]

    def probe_for(self, name: str) -> Probe | None:
        return next((probe for probe in self.steps if probe.name == name), None)


@dataclass(frozen=True)
class CrosstalkProbe:
    """A victim probe and the aggressors measured with it, none on its lit path."""

    probe: Probe
    aggressors: tuple[str, ...]
    # ``leak_order`` of each aggressor, in the same order; empty when not computed.
    leak_orders: tuple[int | None, ...] = ()

    def leak_order(self, aggressor: str) -> int | None:
        return dict(zip(self.aggressors, self.leak_orders)).get(aggressor)


@dataclass(frozen=True)
class CrosstalkPlan:
    groups: tuple[CrosstalkProbe, ...]
    # (aggressor, victim) -> why it cannot be measured.
    uncovered: dict[tuple[str, str], str]
    # (aggressor, victim) pairs left out as too many stages apart to couple.
    distant: tuple[tuple[str, str], ...] = ()

    @property
    def pair_count(self) -> int:
        return sum(len(group.aggressors) for group in self.groups)


def stage_distance(network, first, second) -> int:
    """Stages between two MZIs; 0 in the same stage, 1 for neighbouring stages."""
    return abs(network.switches[first][0] - network.switches[second][0])


def leak_order(network, probe: Probe, aggressor: str) -> int | None:
    """Fewest held MZIs that must leak for lit light through ``aggressor`` to reach the probe's output.

    The victim at quadrature splits its light and the swept aggressor takes
    every state, so both route freely; any other MZI leaks only against its
    held state. 0 means the aggressor steers the detected light itself; None
    means no light that reaches the output ever passes through it.
    """
    states = probe.states(probe.bright_crossed)
    stages = len(network.pairs)
    start = (0, probe.input, False)
    best = {start: 0}
    queue = [(0, start)]
    while queue:
        leaks, (stage, row, through) = heapq.heappop(queue)
        if leaks > best[stage, row, through]:
            continue
        if stage == stages:
            if through and row == probe.output:
                return leaks
            continue
        name = network.at.get((stage, row))
        if name is None:
            choices = ((False, 0),)
        elif name in (probe.name, aggressor):
            choices = ((False, 0), (True, 0))
        else:
            choices = ((states[name], 0), (not states[name], 1))
        for crossed, cost in choices:
            following = (stage + 1, network.step(stage, row, crossed), through or name == aggressor)
            if leaks + cost < best.get(following, leaks + cost + 1):
                best[following] = leaks + cost
                heapq.heappush(queue, (leaks + cost, following))
    return None


def _isolation(order: int | None) -> int:
    """Leak order for ranking probes: unreachable and anything past ``ENOUGH_LEAK_ORDER`` rank alike."""
    return ENOUGH_LEAK_ORDER if order is None else min(order, ENOUGH_LEAK_ORDER)


def _probes_with(network, name, background, inputs, outputs) -> list[Probe]:
    crossed = network.mapping({**background, name: True})
    straight = network.mapping({**background, name: False})
    ordered = tuple((other, background[other]) for other in network.switches if other != name)
    probes = []
    for source in inputs:
        for bright_crossed, mapping, dark in ((True, crossed, straight), (False, straight, crossed)):
            output = mapping[source]
            if output in outputs and dark[source] != output:
                probes.append(Probe(name, source, output, bright_crossed, ordered, dark.index(output)))
    return probes


def probe_candidates(network, name, inputs=None, outputs=None, known=(), max_biased=None,
                     first_level_only=False) -> list[Probe]:
    """Every probe for ``name``, fewest background bars first.

    Only MZIs in ``known`` (calibrated) may be held at bar; the rest stay
    cross. ``first_level_only`` stops at the smallest number of bars that
    gives any probe.
    """
    if name not in network.switches:
        raise ValueError(f"Unknown MZI: {name}")
    inputs = tuple(range(network.size) if inputs is None else inputs)
    outputs = frozenset(range(network.size) if outputs is None else outputs)
    known = [other for other in network.switches if other in set(known) and other != name]
    limit = len(known) if max_biased is None else min(max_biased, len(known))
    found = []
    for count in range(limit + 1):
        for biased in combinations(known, count):
            background = {other: other not in biased for other in network.switches if other != name}
            found.extend(_probes_with(network, name, background, inputs, outputs))
        if found and first_level_only:
            break
    return found


def _visibility_hint(network, name, inputs) -> str:
    outputs = sorted({probe.output + 1 for probe in probe_candidates(network, name, inputs)})
    if not outputs:
        return "no lit input can steer light through it"
    return f"put the detector on output {' or '.join(map(str, outputs))}"


def plan_calibration(network, names=None, inputs=None, outputs=None, known=()) -> CalibrationPlan:
    """Order MZI calibrations so each one is visible when its turn comes.

    Rounds calibrate every MZI visible with the MZIs known so far; each round
    makes more MZIs available as bar backgrounds for the next. Within a round
    each MZI uses the same lowest-numbered input/output probe that a single
    calibration would choose. Within each input, output-side MZIs run first;
    this keeps earlier calibration biases comparable while allowing the JDSU
    to move to the output each MZI actually needs.
    """
    names = list(network.switches if names is None else names)
    inputs = tuple(range(network.size) if inputs is None else inputs)
    known = set(known)
    steps = []
    remaining = names
    while remaining:
        found = {}
        for name in remaining:
            candidates = probe_candidates(network, name, inputs, outputs, known, first_level_only=True)
            if candidates:
                found[name] = candidates
        if not found:
            break
        # Reuse each switch's single-calibration choice. Sharing a convenient
        # route with another MZI can pick a weak physical output branch.
        chosen = {name: min(candidates, key=lambda probe: (probe.input, probe.output))
                  for name, candidates in found.items()}
        steps.extend(sorted(chosen.values(), key=lambda probe: (
            probe.input, -network.switches[probe.name][0], probe.output, probe.name)))
        known |= set(chosen)
        remaining = [name for name in remaining if name not in chosen]
    unreachable = {name: _visibility_hint(network, name, inputs) for name in remaining}
    return CalibrationPlan(tuple(steps), unreachable)


def plan_crosstalk(network, victims, aggressors=None, inputs=None, outputs=None, known=(),
                   max_biased=2, max_stage_distance=None,
                   min_leak_order=DEFAULT_MIN_LEAK_ORDER) -> CrosstalkPlan:
    """Choose victim probes that keep each aggressor's light away from the detector.

    An aggressor on the lit path would switch the light itself; one a single
    leak away still adds light that interferes with the probe as it switches.
    Each aggressor is measured with a probe of the highest ``leak_order`` any
    probe of its victim reaches, and the fewest such probes are used. A pair
    whose best probe stays below ``min_leak_order`` is not measurable. At most
    ``max_biased`` background MZIs are heated to bar: more seldom helps and
    adds background heat. With ``max_stage_distance`` only aggressors that
    many stages or fewer from the victim are measured; heat from further
    away is taken as negligible.
    """
    aggressors = list(network.switches if aggressors is None else aggressors)
    groups, uncovered, distant = [], {}, []
    for victim in victims:
        wanted = [name for name in aggressors if name != victim]
        if max_stage_distance is not None:
            distant.extend((name, victim) for name in wanted
                           if stage_distance(network, name, victim) > max_stage_distance)
            wanted = [name for name in wanted if stage_distance(network, name, victim) <= max_stage_distance]
        if not wanted:
            continue
        candidates = probe_candidates(network, victim, inputs, outputs, known, max_biased)
        if not candidates:
            for aggressor in wanted:
                uncovered[aggressor, victim] = f"{victim} is not visible: {_visibility_hint(network, victim, inputs)}"
            continue
        orders = [(probe, {name: leak_order(network, probe, name) for name in wanted}) for probe in candidates]
        best = {name: max(_isolation(found[name]) for _, found in orders) for name in wanted}
        for aggressor in wanted:
            if best[aggressor] == 0:
                uncovered[aggressor, victim] = f"every probe of {victim} passes through {aggressor}"
            elif best[aggressor] < min_leak_order:
                uncovered[aggressor, victim] = (
                    f"light through {aggressor} reaches every {victim} probe after {best[aggressor]} leak(s); "
                    f"{min_leak_order} needed")
        wanted = [name for name in wanted if best[name] >= min_leak_order]
        while wanted:
            probe, found = max(orders, key=lambda item: (
                sum(_isolation(item[1][name]) >= best[name] for name in wanted), -len(item[0].biased)))
            covered = tuple(name for name in wanted if _isolation(found[name]) >= best[name])
            groups.append(CrosstalkProbe(probe, covered, tuple(found[name] for name in covered)))
            wanted = [name for name in wanted if name not in covered]
    return CrosstalkPlan(tuple(groups), uncovered, tuple(distant))
