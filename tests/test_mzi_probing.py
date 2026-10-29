import unittest

from benes.core.network import BenesNetwork
from benes.measurements.mzi_probing import (CrosstalkProbe, Probe, leak_order, plan_calibration, plan_crosstalk,
                                            probe_candidates, stage_distance)


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
        # Keep one laser on for its input group while using each MZI's own output.
        inputs = [probe.input for probe in plan.steps]
        self.assertEqual(inputs, sorted(inputs))

    def test_complete_uses_the_same_s4_2_probe_as_single_calibration(self):
        for known in ((), tuple(self.network.switches)):
            with self.subTest(known=bool(known)):
                complete = plan_calibration(self.network, known=known)
                single = plan_calibration(self.network, ["S4.2"], known=known)
                probe = complete.probe_for("S4.2")
                self.assertEqual((probe.input, probe.output, probe.bright_crossed),
                                 (single.steps[0].input, single.steps[0].output,
                                  single.steps[0].bright_crossed))
                self.assertEqual((probe.input, probe.output), (0, 2))
        self.assertEqual([(probe.name, probe.output) for probe in plan_calibration(self.network).steps[:2]],
                         [("S5.3", 4), ("S4.2", 2)])

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
        plan = plan_crosstalk(self.network, list(self.network.switches), known=self.network.switches,
                              min_leak_order=1)
        self.assertEqual(plan.uncovered, {})
        self.assertEqual(plan.pair_count, 12 * 11)
        measured = set()
        for group in plan.groups:
            path = set(group.probe.bright_path(self.network))
            self.assertFalse(path & set(group.aggressors))
            measured |= {(aggressor, group.probe.name) for aggressor in group.aggressors}
        self.assertEqual(len(measured), 12 * 11)

    def test_leak_order_counts_the_held_mzis_light_must_leak_through(self):
        background = {name: True for name in self.network.switches if name != "S5.3"}
        probe = Probe("S5.3", 5, 5, True, tuple({**background, "S3.1": False}.items()), 0)
        # The stage-5 neighbours never carry light that reaches this output.
        self.assertIsNone(leak_order(self.network, probe, "S5.1"))
        self.assertEqual(leak_order(self.network, probe, "S3.2"), 1)
        self.assertEqual(leak_order(self.network, probe, "S4.2"), 2)
        for name in probe.bright_path(self.network):
            if name != "S5.3":
                self.assertEqual(leak_order(self.network, probe, name), 0)

    def test_each_aggressor_gets_the_best_isolated_probe(self):
        names = list(self.network.switches)
        plan = plan_crosstalk(self.network, ["S5.3"], ["S3.2", "S4.2", "S5.1", "S5.2"], known=names)
        self.assertEqual(plan.uncovered, {})
        orders = {aggressor: group.leak_order(aggressor) for group in plan.groups for aggressor in group.aggressors}
        # S3.2 reaches three leaks only with S4.1 and S4.2 at bar; S4.2 at most two.
        self.assertEqual(orders, {"S3.2": 3, "S4.2": 2, "S5.1": None, "S5.2": None})
        for group in plan.groups:
            for aggressor in group.aggressors:
                self.assertEqual(group.leak_order(aggressor), leak_order(self.network, group.probe, aggressor))

    def test_pairs_one_leak_from_every_probe_are_not_measurable(self):
        names = list(self.network.switches)
        plan = plan_crosstalk(self.network, names, known=names)
        self.assertEqual(set(plan.uncovered),
                         {("S3.1", "S2.1"), ("S3.1", "S4.1"), ("S3.2", "S2.2"), ("S3.2", "S4.2")})
        self.assertIn("after 1 leak(s); 2 needed", plan.uncovered["S3.2", "S4.2"])
        self.assertEqual(plan.pair_count, 12 * 11 - 4)
        for group in plan.groups:
            self.assertTrue(all(order is None or order >= 2 for order in group.leak_orders))
        self.assertIsNone(CrosstalkProbe(plan.groups[0].probe, ("S1.1",)).leak_order("S1.1"))

    def test_adjacent_stages_only_leaves_distant_pairs_out(self):
        names = list(self.network.switches)
        plan = plan_crosstalk(self.network, names, known=names, max_stage_distance=1, min_leak_order=1)
        self.assertEqual(plan.uncovered, {})
        # Same stage: 3·2 + 2·1 + 2·1 + 2·1 + 3·2; next stage: 2·(3·2 + 2·2 + 2·2 + 2·3).
        self.assertEqual(plan.pair_count, 18 + 40)
        self.assertEqual(len(plan.distant), 12 * 11 - 58)
        self.assertIn(("S4.2", "S1.2"), plan.distant)
        for group in plan.groups:
            for aggressor in group.aggressors:
                self.assertLessEqual(stage_distance(self.network, aggressor, group.probe.name), 1)

    def test_without_calibration_some_pairs_share_every_path(self):
        plan = plan_crosstalk(self.network, ["S1.1"], ["S5.3", "S2.1"])
        self.assertIn(("S5.3", "S1.1"), plan.uncovered)
        self.assertEqual([group.aggressors for group in plan.groups], [("S2.1",)])


if __name__ == "__main__":
    unittest.main()
