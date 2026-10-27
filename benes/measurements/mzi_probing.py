"""Which MZI can be seen from which laser input at which detector output.

A probe lights one input and reads one output. The probed MZI decides
whether that input's light reaches the output: in one state it arrives
(bright), in the other it leaves elsewhere (dark). Every other MZI holds a
known background state: cross at 0 V (the unbiased state) while it is
uncalibrated, or its calibrated cross or bar voltage once known.

Calibrated MZIs set to bar let a fixed detector see further, so calibration
bootstraps: it calibrates what is visible, then uses those MZIs to reach the
rest. With the detector behind the JDSU every MZI is visible at 0 V.

All ports are 0-based here.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations


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
    probes are grouped by input and output, so lasers and the JDSU move as
    little as possible, with output-side MZIs first.
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
        # Prefer the (input, output) pair that serves the most MZIs this round.
        chosen = {}
        while len(chosen) < len(found):
            counts = {}
            for name, candidates in found.items():
                if name not in chosen:
                    for probe in candidates:
                        key = (probe.input, probe.output)
                        counts[key] = counts.get(key, 0) + 1
            key = min(counts, key=lambda pair: (-counts[pair], pair))
            for name, candidates in found.items():
                if name not in chosen:
                    match = next((probe for probe in candidates if (probe.input, probe.output) == key), None)
                    if match is not None:
                        chosen[name] = match
        steps.extend(sorted(chosen.values(), key=lambda probe: (
            probe.input, probe.output, -network.switches[probe.name][0], probe.name)))
        known |= set(chosen)
        remaining = [name for name in remaining if name not in chosen]
    unreachable = {name: _visibility_hint(network, name, inputs) for name in remaining}
    return CalibrationPlan(tuple(steps), unreachable)


def plan_crosstalk(network, victims, aggressors=None, inputs=None, outputs=None, known=(),
                   max_biased=2, max_stage_distance=None) -> CrosstalkPlan:
    """Choose victim probes whose lit path avoids each aggressor.

    An aggressor on the lit path would switch the light itself, so it must be
    measured with another probe. Each victim takes the probe that leaves the
    most aggressors measurable, then more probes for the rest. At most
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
        paths = [(probe, set(probe.bright_path(network))) for probe in candidates]
        while wanted:
            probe, path = min(paths, key=lambda item: (sum(name in item[1] for name in wanted),
                                                       len(item[0].biased)))
            covered = tuple(name for name in wanted if name not in path)
            if not covered:
                for aggressor in wanted:
                    uncovered[aggressor, victim] = (f"every probe of {victim} passes through {aggressor}")
                break
            groups.append(CrosstalkProbe(probe, covered))
            wanted = [name for name in wanted if name not in covered]
    return CrosstalkPlan(tuple(groups), uncovered, tuple(distant))
