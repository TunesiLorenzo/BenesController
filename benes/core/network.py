"""A six-port generalized Beneš network, including the odd-sized bypasses."""

from dataclasses import dataclass
from itertools import product


@dataclass(frozen=True)
class Route:
    # Row occupied immediately before each stage, followed by the output row.
    rows: tuple[int, ...]
    states: tuple[tuple[str, bool], ...]


class BenesNetwork:
    size = 6
    # Each 3-port subnetwork is three adjacent-pair switches: (1,2),
    # (0,1), (1,2). The first and last stages have their bypass at the top.
    pairs = (
        ((0, 1), (2, 3), (4, 5)),
        ((1, 2), (4, 5)),
        ((0, 1), (3, 4)),
        ((1, 2), (4, 5)),
        ((0, 1), (2, 3), (4, 5)),
    )
    links = (
        (0, 3, 1, 4, 2, 5),
        (0, 1, 2, 3, 4, 5),
        (0, 1, 2, 3, 4, 5),
        (0, 2, 4, 1, 3, 5),
        (0, 1, 2, 3, 4, 5),
    )

    def __init__(self):
        self.switches = {}
        self.at = {}
        for stage, pairs in enumerate(self.pairs):
            for index, pair in enumerate(pairs):
                name = f"S{stage + 1}.{index + 1}"
                self.switches[name] = (stage, pair)
                for row in pair:
                    self.at[stage, row] = name
        # The fabricated MZIs are cross at their unbiased 0 V state.
        self.states = dict.fromkeys(self.switches, True)

    def switch_output(self, stage, row, crossed):
        """Row leaving a switch, before the wiring to the next stage."""
        name = self.at.get((stage, row))
        if name and crossed:
            a, b = self.switches[name][1]
            row = b if row == a else a
        return row

    def step(self, stage, row, crossed):
        return self.links[stage][self.switch_output(stage, row, crossed)]

    def trace(self, source, states=None):
        states = self.states if states is None else states
        rows = [source]
        used = []
        for stage in range(len(self.pairs)):
            name = self.at.get((stage, rows[-1]))
            crossed = states[name] if name else False
            if name:
                used.append((name, crossed))
            rows.append(self.step(stage, rows[-1], crossed))
        return Route(tuple(rows), tuple(used))

    def mapping(self, states=None):
        return tuple(self.trace(i, states).rows[-1] for i in range(self.size))

    def routes(self, source, destination):
        """Enumerate every physical path; unrelated switches remain unconstrained."""
        found = []

        def visit(stage, rows, states):
            if stage == len(self.pairs):
                if rows[-1] == destination:
                    found.append(Route(tuple(rows), tuple(states)))
                return
            name = self.at.get((stage, rows[-1]))
            for crossed in (False, True) if name else (False,):
                visit(stage + 1,
                      rows + [self.step(stage, rows[-1], crossed)],
                      states + [(name, crossed)] if name else states)

        visit(0, [source], [])
        return found

    def solve(self, permutation):
        states = self.states_for_permutation(permutation)
        if states is None:
            return False
        self.states.update(states)
        return True

    def states_for_permutation(self, permutation, required_states=None, prefer_states=None):
        """Return states for ``permutation`` that honour requirements and minimise changes."""
        if sorted(permutation) != list(range(self.size)):
            raise ValueError("Use each output from 1 to 6 exactly once.")
        required_states = required_states or {}
        unknown = set(required_states) - set(self.switches)
        if unknown:
            raise ValueError(f"Unknown switch: {sorted(unknown)[0]}")
        names = tuple(self.switches)
        best = None
        fewest_changes = len(names) + 1
        for bits in product((False, True), repeat=len(names)):
            states = dict(zip(names, bits))
            if (all(states[name] == crossed for name, crossed in required_states.items()) and
                    self.mapping(states) == tuple(permutation)):
                if prefer_states is None:
                    return states
                changes = sum(states[name] != prefer_states[name] for name in names)
                if changes < fewest_changes:
                    best, fewest_changes = states, changes
        return best

    def equivalent_permutation_states(self, permutation):
        """Return every full switch-state configuration that realizes ``permutation``."""
        if sorted(permutation) != list(range(self.size)):
            raise ValueError("Use each output from 1 to 6 exactly once.")
        names = tuple(self.switches)
        matches = []
        for bits in product((False, True), repeat=len(names)):
            states = dict(zip(names, bits))
            if self.mapping(states) == tuple(permutation):
                matches.append(states)
        return matches
