import unittest

from benes.core.network import BenesNetwork
from benes.measurements.mzi_probing import plan_calibration, plan_crosstalk, probe_candidates


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.network = BenesNetwork()

    def test_a_probe_reaches_its_output_only_in_the_bright_state(self):
        for name in self.network.switches:
            for probe in probe_candidates(self.network, name):
                bright = self.network.mapping(probe.states(probe.bright_crossed))
                dark = self.network.mapping(probe.states(not probe.bright_crossed))
                self.assertEqual(bright[probe.input], probe.output)
                self.assertNotEqual(dark[probe.input], probe.output)
                self.assertEqual(dark.index(probe.output), probe.other_input)
                self.assertIn(name, probe.bright_path(self.network))

    def test_uncalibrated_mzis_stay_cross(self):
        for probe in probe_candidates(self.network, "S3.1"):
            self.assertEqual(probe.biased, ())

    def test_inputs_and_outputs_limit_the_probes(self):
        probes = probe_candidates(self.network, "S1.1", inputs=[0], outputs=[4])
        self.assertTrue(probes)
        self.assertTrue(all((probe.input, probe.output) == (0, 4) for probe in probes))


class CalibrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.network = BenesNetwork()

    def test_the_jdsu_sees_every_mzi_at_zero_volts(self):
        plan = plan_calibration(self.network)
        self.assertEqual(sorted(probe.name for probe in plan.steps), sorted(self.network.switches))
        self.assertEqual(plan.unreachable, {})
        self.assertTrue(all(not probe.biased for probe in plan.steps))
        # Grouped by laser and output, so neither moves back to an earlier one.
        pairs = [(probe.input, probe.output) for probe in plan.steps]
        changes = [pair for index, pair in enumerate(pairs) if index == 0 or pairs[index - 1] != pair]
        self.assertEqual(len(changes), len(set(changes)))

    def test_a_fixed_detector_bootstraps_through_calibrated_bars(self):
        plan = plan_calibration(self.network, outputs=[2])
        calibrated = set()
        for probe in plan.steps:
            self.assertTrue(set(probe.biased) <= calibrated, probe)
            self.assertEqual(probe.output, 2)
            calibrated.add(probe.name)
        self.assertTrue(any(probe.biased for probe in plan.steps))
        # The last stage only reaches its own two outputs.
        self.assertEqual(set(plan.unreachable), {"S5.1", "S5.3"})
        self.assertIn("output 1 or 2", plan.unreachable["S5.1"])

    def test_inputs_without_light_cannot_probe(self):
        plan = plan_calibration(self.network, inputs=[])
        self.assertEqual(plan.steps, ())
        self.assertEqual(set(plan.unreachable), set(self.network.switches))


class CrosstalkPlanTests(unittest.TestCase):
    def setUp(self):
        self.network = BenesNetwork()

    def test_aggressors_are_never_on_the_lit_path(self):
        plan = plan_crosstalk(self.network, list(self.network.switches), known=self.network.switches)
        self.assertEqual(plan.uncovered, {})
        self.assertEqual(plan.pair_count, 12 * 11)
        measured = set()
        for group in plan.groups:
            path = set(group.probe.bright_path(self.network))
            self.assertFalse(path & set(group.aggressors))
            measured |= {(aggressor, group.probe.name) for aggressor in group.aggressors}
        self.assertEqual(len(measured), 12 * 11)

    def test_without_calibration_some_pairs_share_every_path(self):
        plan = plan_crosstalk(self.network, ["S1.1"], ["S5.3", "S2.1"])
        self.assertIn(("S5.3", "S1.1"), plan.uncovered)
        self.assertEqual([group.aggressors for group in plan.groups], [("S2.1",)])


if __name__ == "__main__":
    unittest.main()
