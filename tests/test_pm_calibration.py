import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.core.network import BenesNetwork
from benes.measurements.mzi_fit import SweepPoint
from benes.measurements.mzi_probing import Probe
from benes.measurements.pm_calibration import (PMCalibrationRun, extract_extremes, isolated_probe, leak_paths,
                                               resolve_labels, save_pm_calibration)


class Controller:
    """No heater resistances: drive is V²."""


def ideal_measure(network, swapped, part_way=()):
    """Binary chip whose labels are wrong for ``swapped``; ``part_way`` MZIs split light evenly."""

    def measure(states):
        physical = {name: crossed != (name in swapped) for name, crossed in states.items()}
        powers = []
        for source in range(network.size):
            row = [1e-6] * network.size
            spread = [(physical, 1.0)]
            for name in part_way:
                spread = [({**states_, name: crossed}, weight / 2) for states_, weight in spread
                          for crossed in (True, False)]
            for states_, weight in spread:
                row[network.mapping(states_)[source]] += weight
            powers.append(row)
        return powers

    return measure


class IsolatedProbeTests(unittest.TestCase):
    def test_every_mzi_has_a_probe_no_single_leak_reaches(self):
        network = BenesNetwork()
        for name in network.switches:
            found = isolated_probe(network, name)
            probe = found.probe
            self.assertEqual(found.first_order, 0, name)
            bright = network.mapping(probe.states(probe.bright_crossed))
            dark = network.mapping(probe.states(not probe.bright_crossed))
            self.assertEqual(bright[probe.input], probe.output)
            self.assertNotEqual(dark[probe.input], probe.output)
            paths = leak_paths(network, dict(probe.background), name, probe.input, max_leaks=1)
            self.assertFalse([path for path in paths if path[0] == probe.output and path[1] == 1])

    def test_unknown_mzis_stay_cross(self):
        network = BenesNetwork()
        fixed = {other: True for other in network.switches if other != "S1.1"}
        probe = isolated_probe(network, "S1.1", fixed).probe
        self.assertEqual(probe.biased, ())


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.network = BenesNetwork()

    def test_correct_labels_are_kept(self):
        result = resolve_labels(self.network, ideal_measure(self.network, set()))
        self.assertEqual(result.swapped, ())
        self.assertTrue(result.consistent)

    def test_first_and_last_stage_swapped_together_are_found(self):
        for swapped in ({"S1.1", "S5.1"}, {"S2.2"}, {"S1.2", "S3.1", "S4.2", "S5.3"}):
            with self.subTest(swapped=swapped):
                result = resolve_labels(self.network, ideal_measure(self.network, swapped))
                # Whatever was named routes exactly like the truth in every configuration tried.
                for test in result.tests:
                    truth = {n: c != (n in swapped) for n, c in test.states.items()}
                    found = {n: c != (n in result.swapped) for n, c in test.states.items()}
                    self.assertEqual(self.network.mapping(truth), self.network.mapping(found))
                self.assertEqual(set(result.swapped), swapped)
                self.assertTrue(result.consistent)

    def test_a_part_way_mzi_is_reported(self):
        result = resolve_labels(self.network, ideal_measure(self.network, set(), part_way=("S3.1",)))
        self.assertTrue(result.ambiguous)
        self.assertIn("S3.1", result.suspects)


class ExtremeTests(unittest.TestCase):
    def test_fitted_extremes_fall_between_steps(self):
        voltages = [round(0.1 * step, 2) for step in range(51)]
        v2_pi, phase0 = 7.3, 1.1
        points = [SweepPoint(v, 1e-4 + (1 + math.cos(math.pi * v * v / v2_pi + phase0)) / 2) for v in voltages]
        probe = Probe("S2.1", 0, 1, True, (), 2)
        result = extract_extremes(Controller(), probe, points, voltages)
        bright = math.sqrt((2 * math.pi - phase0) * v2_pi / math.pi)
        dark = math.sqrt((math.pi - phase0) * v2_pi / math.pi)
        self.assertAlmostEqual(result.cross.voltage, bright, delta=0.02)
        self.assertAlmostEqual(result.bar.voltage, dark, delta=0.02)


class SaveTests(unittest.TestCase):
    def test_saves_rounds_and_label_tests(self):
        network = BenesNetwork()
        run = PMCalibrationRun()
        run.labels.append(resolve_labels(network, ideal_measure(network, {"S1.1"})))
        run.label_rounds.append(1)
        run.rounds.append({})
        run.changes.append(math.inf)
        with tempfile.TemporaryDirectory() as folder:
            files = save_pm_calibration(folder, {"kind": "test"}, run)
            self.assertIn("pm_calibration.json", files)
            record = json.loads((Path(folder) / "pm_calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(record["label_tests"][0]["swapped"], ["S1.1"])
            self.assertEqual(record["round_changes_v"], [None])
            self.assertTrue((Path(folder) / "pm_label_tests.csv").is_file())


if __name__ == "__main__":
    unittest.main()
