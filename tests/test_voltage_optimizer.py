import math
import threading
import unittest

from benes.core.network import BenesNetwork
from benes.measurements.laser_calibration import CalibrationStopped
from benes.measurements.voltage_optimizer import (OBJECTIVE_LEAK, OBJECTIVE_THROUGH, leak_output,
                                                  optimization_plan, optimize_paths, optimize_switch)


class FakeController:
    def __init__(self, voltages):
        self.applied_voltages = dict(voltages)
        self.sent = []

    def drive_voltage(self, name, voltage):
        self.applied_voltages[name] = voltage
        self.sent.append((name, voltage))
        return voltage


def phase_error(voltage, peak_v):
    """Phase from the ideal state; the heater phase grows with power, so with V²."""
    return math.pi / 2 * (voltage ** 2 - peak_v ** 2) / 6.0


def through(voltage, peak_v):
    return math.cos(phase_error(voltage, peak_v)) ** 2


def leak(voltage, peak_v, extinction=1e-3):
    return math.sin(phase_error(voltage, peak_v)) ** 2 + extinction


class OptimizeSwitchTests(unittest.TestCase):
    def search(self, start_v, peak_v, response=through, **options):
        state = {"v": start_v}

        def drive(name, voltage):
            state["v"] = voltage

        result = optimize_switch(drive, lambda: response(state["v"], peak_v), "S1.1", start_v, **options)
        return result, state["v"]

    def test_finds_the_nearby_peak_of_the_routed_power(self):
        (best_v, start_mw, best_mw, reads), left_at = self.search(2.30, 2.47)
        self.assertAlmostEqual(best_v, 2.47, delta=0.03)
        self.assertEqual(left_at, best_v)
        self.assertGreater(best_mw, start_mw)
        self.assertLessEqual(reads, 16)

    def test_finds_the_leak_minimum_to_the_board_resolution(self):
        (best_v, start_mw, best_mw, reads), _ = self.search(2.30, 2.47, leak, minimize=True)
        self.assertAlmostEqual(best_v, 2.47, delta=0.015)
        self.assertLess(best_mw, start_mw / 10)

    def test_stays_within_the_offset_from_the_start(self):
        (best_v, *_), _ = self.search(2.0, 3.5, max_offset_v=0.3)
        self.assertLessEqual(best_v, 2.3 + 1e-9)

    def test_never_leaves_the_board_range(self):
        (best_v, *_), _ = self.search(0.05, 0.0)
        self.assertGreaterEqual(best_v, 0.0)

    def test_refused_voltages_are_skipped(self):
        (best_v, *_), _ = self.search(2.30, 2.47, allowed=lambda name, voltage: voltage <= 2.35)
        self.assertLessEqual(best_v, 2.35)

    def test_noise_below_the_tolerance_does_not_move_the_heater(self):
        readings = iter([1.0, 1.004, 0.997, 1.003] * 10)
        best_v, *_ = optimize_switch(lambda name, voltage: None, lambda: next(readings), "S1.1", 2.0)
        self.assertEqual(best_v, 2.0)


class PlanTests(unittest.TestCase):
    def test_one_input_tunes_only_its_own_path(self):
        network = BenesNetwork()
        ((source, output, switches),) = optimization_plan(network, [2])
        route = network.trace(2)
        self.assertEqual((source, output), (2, route.rows[-1]))
        self.assertEqual(tuple(name for name, _ in switches), tuple(name for name, _ in route.states))

    def test_all_inputs_tune_every_mzi_once(self):
        network = BenesNetwork()
        plan = optimization_plan(network, range(6))
        names = [name for _, _, switches in plan for name, _ in switches]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(network.switches))

    def test_the_leak_output_is_where_the_flipped_mzi_sends_the_light(self):
        network = BenesNetwork()
        for source in range(6):
            routed = network.trace(source).rows[-1]
            for name, crossed in network.trace(source).states:
                flipped = network.trace(source, {**network.states, name: not crossed}).rows[-1]
                self.assertEqual(leak_output(network, source, name), flipped)
                self.assertNotEqual(flipped, routed)


class OptimizePathsTests(unittest.TestCase):
    def setUp(self):
        self.peaks = {"S1.1": 2.4, "S2.1": 1.1}
        self.controller = FakeController({"S1.1": 2.3, "S2.1": 1.25})
        self.lit = []
        self.selected = []
        self.plan = ((0, 3, (("S1.1", 1), ("S2.1", 4))),)

    def select(self, output):
        self.selected.append(output)

    def read(self):
        volts = self.controller.applied_voltages
        output = self.selected[-1] if self.selected else 3
        if output == 3:
            return through(volts["S1.1"], 2.4) * through(volts["S2.1"], 1.1)
        if output == 1:
            return leak(volts["S1.1"], 2.4)
        return through(volts["S1.1"], 2.4) * leak(volts["S2.1"], 1.1)

    def test_leak_objective_reads_each_mzi_at_its_leak_output(self):
        paths = optimize_paths(self.controller, self.plan, self.read, light=self.lit.append,
                               select_output=self.select, sleep=lambda seconds: None)
        self.assertEqual(self.lit, [0])
        self.assertEqual(self.selected, [3, 1, 4, 3])
        (path,) = paths
        self.assertGreater(path.gain_db, 0)
        self.assertTrue(all(item.objective == OBJECTIVE_LEAK and item.gain_db > 3 for item in path.switches))
        self.assertAlmostEqual(self.controller.applied_voltages["S1.1"], 2.4, delta=0.015)
        self.assertAlmostEqual(self.controller.applied_voltages["S2.1"], 1.1, delta=0.015)

    def test_through_objective_works_with_the_meter_on_one_output(self):
        (path,) = optimize_paths(self.controller, self.plan, self.read, objective=OBJECTIVE_THROUGH,
                                 sleep=lambda seconds: None)
        self.assertEqual(self.selected, [])
        self.assertGreater(path.gain_db, 0)

    def test_leak_objective_needs_the_jdsu(self):
        with self.assertRaises(ValueError):
            optimize_paths(self.controller, self.plan, self.read, sleep=lambda seconds: None)

    def test_a_stop_restores_the_mzi_being_searched_and_keeps_finished_ones(self):
        stop = threading.Event()
        done = []

        def read():
            if self.controller.sent and self.controller.sent[-1][0] == "S2.1":
                stop.set()
            return self.read()

        with self.assertRaises(CalibrationStopped):
            optimize_paths(self.controller, self.plan, read, select_output=self.select, stop_event=stop,
                           sleep=lambda seconds: None, results=done)
        self.assertEqual([item.name for item in done], ["S1.1"])
        self.assertEqual(self.controller.applied_voltages["S2.1"], 1.25)


if __name__ == "__main__":
    unittest.main()
