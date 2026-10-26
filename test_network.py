import unittest
from itertools import product

from network import BenesNetwork


class NetworkTests(unittest.TestCase):
    def test_unbiased_switches_default_to_cross(self):
        network = BenesNetwork()
        self.assertTrue(all(network.states.values()))

    def test_three_port_subnetworks_independently_route_all_six_permutations(self):
        network = BenesNetwork()
        for offset in (0, 3):
            mappings = set()
            for bits in product((False, True), repeat=3):
                outputs = []
                for source in range(offset, offset + 3):
                    row = source
                    for stage, crossed in zip((1, 2, 3), bits):
                        # Measure at the 3x3 output, before the outer-stage wiring.
                        row = (network.switch_output(stage, row, crossed) if stage == 3
                               else network.step(stage, row, crossed))
                    outputs.append(row - offset)
                self.assertEqual(sorted(outputs), [0, 1, 2])
                mappings.add(tuple(outputs))
            self.assertEqual(len(mappings), 6)

    def test_three_port_bypasses_alternate(self):
        network = BenesNetwork()
        for offset in (0, 3):
            for stage, bypass in ((1, offset), (2, offset + 2), (3, offset)):
                self.assertNotIn((stage, bypass), network.at)
                self.assertEqual(network.switch_output(stage, bypass, False), bypass)
                self.assertEqual(network.switch_output(stage, bypass, True), bypass)
            for stage in (1, 2):
                for row in range(offset, offset + 3):
                    self.assertEqual(network.links[stage][row], row)

    def test_all_720_permutations_are_realizable(self):
        network = BenesNetwork()
        mappings = set()
        for bits in product((False, True), repeat=len(network.switches)):
            mapping = network.mapping(dict(zip(network.switches, bits)))
            self.assertEqual(sorted(mapping), list(range(6)))
            mappings.add(mapping)
        self.assertEqual(len(mappings), 720)

    def test_every_advertised_path_reaches_destination(self):
        network = BenesNetwork()
        for source in range(6):
            for destination in range(6):
                routes = network.routes(source, destination)
                self.assertTrue(routes)
                self.assertEqual(len(routes), len(set(routes)))
                for route in routes:
                    states = network.states | dict(route.states)
                    self.assertEqual(network.trace(source, states), route)
                    self.assertEqual(route.rows[-1], destination)

    def test_solver_and_invalid_permutations(self):
        network = BenesNetwork()
        target = (5, 2, 4, 0, 3, 1)
        self.assertTrue(network.solve(target))
        self.assertEqual(network.mapping(), target)
        for invalid in ((0, 0, 1, 2, 3, 4), (0, 1), (1, 2, 3, 4, 5, 6)):
            with self.assertRaises(ValueError):
                network.solve(invalid)

    def test_constrained_solver_keeps_a_requested_path_and_permutation(self):
        network = BenesNetwork()
        target = (5, 2, 4, 0, 3, 1)
        self.assertTrue(network.solve(target))
        active_path = network.trace(0)
        states = network.states_for_permutation(target, dict(active_path.states), prefer_states=network.states)
        self.assertIsNotNone(states)
        self.assertEqual(network.mapping(states), target)
        self.assertTrue(all(states[name] == crossed for name, crossed in active_path.states))
        self.assertEqual(states, network.states)

    def test_equivalent_permutation_states_lists_every_full_routing(self):
        network = BenesNetwork()
        target = (5, 2, 4, 0, 3, 1)
        states = network.equivalent_permutation_states(target)
        self.assertGreater(len(states), 1)
        self.assertEqual(len({tuple(item[name] for name in network.switches) for item in states}), len(states))
        self.assertTrue(all(network.mapping(item) == target for item in states))


if __name__ == "__main__":
    unittest.main()
