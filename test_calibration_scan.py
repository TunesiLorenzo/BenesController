import unittest
from types import SimpleNamespace
from unittest.mock import patch

from calibration_scan import (calibrate_default_cross_path, minimum_default_cross_pairs,
                              next_default_cross_pair,
                              scan_switch, scan_switches, scan_switches_with_reader,
                              scan_voltages, spectrum_scan_point)
from network import BenesNetwork


class FakeController:
    connected = True

    def __init__(self):
        self.settings = {
            "dry_run": False,
            "mapping": {"S1.1": 3},
            "voltage": {"S1.1": 1.25},
        }
        self.applied_voltages = {"S1.1": 2.75}
        self.sent = []

    def drive_voltage(self, name, voltage):
        self.sent.append(voltage)
        self.applied_voltages[name] = voltage

    def set_calibration(self, name, straight, cross):
        self.settings.setdefault("calibration", {})[name] = {"straight": straight, "cross": cross}


class FakeMeter:
    connected = True

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def read_current_power(self):
        self.calls += 1
        if self.fail and self.calls == 2:
            raise OSError("meter disconnected")
        return SimpleNamespace(value=float(self.calls))


class CalibrationScanTests(unittest.TestCase):
    def test_range_includes_endpoint_and_rejects_excessive_scan(self):
        self.assertEqual(scan_voltages(-1, 1, 0.6), [-1.0, -0.4, 0.2, 0.8, 1.0])
        with self.assertRaisesRegex(ValueError, "larger step"):
            scan_voltages(-10, 10, 0.01)

    @patch("calibration_scan.time.sleep")
    def test_scan_reads_each_point_and_restores_voltage(self, _sleep):
        controller = FakeController()
        points = scan_switch(controller, FakeMeter(), "S1.1", [0, 1, 2], 0.01)
        self.assertEqual([(point.voltage, point.power) for point in points], [(0, 1), (1, 2), (2, 3)])
        self.assertEqual(controller.sent, [0, 1, 2, 2.75])
        self.assertEqual(controller.settings["voltage"]["S1.1"], 1.25)

    @patch("calibration_scan.time.sleep")
    def test_scan_restores_voltage_after_meter_failure(self, _sleep):
        controller = FakeController()
        with self.assertRaisesRegex(OSError, "meter disconnected"):
            scan_switch(controller, FakeMeter(fail=True), "S1.1", [0, 1], 0)
        self.assertEqual(controller.sent, [0, 1, 2.75])

    @patch("calibration_scan.time.sleep")
    def test_sequential_scans_reset_each_switch_to_zero(self, _sleep):
        controller = FakeController()
        controller.settings["mapping"]["S1.2"] = 4
        points = scan_switches(controller, FakeMeter(), ["S1.1", "S1.2"], [-1, 1], 0)
        self.assertEqual(controller.sent, [-1, 1, 0, -1, 1, 0])
        self.assertEqual([p.power for p in points["S1.2"]], [3, 4])

    @patch("calibration_scan.time.sleep")
    def test_sequential_scan_resets_active_switch_on_failure(self, _sleep):
        controller = FakeController()
        controller.settings["mapping"]["S1.2"] = 4
        with self.assertRaisesRegex(OSError, "meter disconnected"):
            scan_switches(controller, FakeMeter(fail=True), ["S1.1", "S1.2"], [-1, 1], 0)
        self.assertEqual(controller.sent, [-1, 1, 0, 0])

    @patch("calibration_scan.time.sleep")
    def test_osa_reader_keeps_full_spectrum_and_resets_switch(self, _sleep):
        controller = FakeController()
        results = scan_switches_with_reader(
            controller, ["S1.1"], [-1, 1], 0,
            lambda voltage: spectrum_scan_point(voltage, ([1550.0, 1551.0, 1552.0, 1553.0],
                                                         [-40.0, -20.0, 30.0, 40.0])),
        )
        self.assertEqual([point.power for point in results["S1.1"]], [-20.0, -20.0])
        self.assertEqual(results["S1.1"][0].spectrum_dbm, (-40.0, -20.0))
        self.assertEqual(results["S1.1"][0].wavelength_nm, (1550.0, 1551.0))
        self.assertEqual(controller.sent, [-1, 1, 0])

    def test_osa_mean_is_computed_in_linear_power_after_trimming(self):
        point = spectrum_scan_point(0, ([1550, 1551, 1552, 1553], [-30, -20, 50, 60]))
        self.assertEqual(point.power, -20)
        self.assertAlmostEqual(point.mean_power_dbm, -22.596373, places=5)

    def test_osa_reader_rejects_bad_spectrum(self):
        with self.assertRaisesRegex(ValueError, "mismatched"):
            spectrum_scan_point(0, ([1550.0], []))

    def test_minimum_default_cross_plan_covers_every_switch_with_four_pairs(self):
        network = BenesNetwork()
        plan = minimum_default_cross_pairs(network)
        self.assertEqual(len(plan), 4)
        self.assertEqual([(source + 1, output + 1) for source, output, _ in plan],
                         [(1, 5), (2, 6), (3, 3), (6, 2)])
        self.assertEqual(set().union(*(set(names) for _, _, names in plan)), set(network.switches))

    def test_plan_does_not_finish_after_running_only_first_and_last_paths(self):
        plan = minimum_default_cross_pairs(BenesNetwork())
        covered = set(plan[0][2]) | set(plan[-1][2])
        self.assertEqual(next_default_cross_pair(plan, covered), plan[1][:2])
        covered.update(plan[1][2])
        self.assertEqual(next_default_cross_pair(plan, covered), plan[2][:2])
        covered.update(plan[2][2])
        self.assertIsNone(next_default_cross_pair(plan, covered))

    @patch("calibration_scan.time.sleep")
    def test_default_cross_path_calibration_averages_and_resets_every_mzi(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([10, 14, 1, 3, 5, 7] * 4)
        meter = SimpleNamespace(connected=True, read_current_power=lambda: SimpleNamespace(value=next(readings)))
        result = calibrate_default_cross_path(controller, meter, network, 0, 4, [0, 1, 2], 0, averages=2)
        self.assertEqual(list(result), ["S5.3", "S4.2", "S3.2", "S1.1"])
        self.assertEqual([point.power for point in result["S5.3"].points], [12, 2, 6])
        self.assertEqual(controller.settings["calibration"]["S5.3"], {"straight": 1, "cross": 0})
        self.assertTrue(all(controller.applied_voltages[name] == 0 for name in network.switches))

    @patch("calibration_scan.time.sleep")
    def test_floor_rescan_uses_sensitive_minimum_and_restores_range_before_next_switch(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([10, 2, 2, 100, 3, 5] + [10, 5, 1] * 3)
        meter = SimpleNamespace(connected=True, settings={"offset": 2.0},
                                read_current_power=lambda: SimpleNamespace(value=next(readings)))
        changes = []

        def change_range(name, phase, floor_count):
            changes.append((name, phase, floor_count, controller.applied_voltages[name]))
            return True

        results = calibrate_default_cross_path(
            controller, meter, network, 0, 4, [0, 1, 2], 0, on_range_change=change_range,
        )
        self.assertEqual(changes, [("S5.3", "sensitive", 2, 0), ("S5.3", "restore_sensitive", 2, 0)])
        self.assertEqual(results["S5.3"].cross.voltage, 0)
        self.assertEqual(results["S5.3"].straight.voltage, 1)
        self.assertEqual([point.power for point in results["S5.3"].sensitive_points], [100, 3, 5])
        self.assertEqual(controller.settings["calibration"]["S5.3"], {"straight": 1, "cross": 0})

    @patch("calibration_scan.time.sleep")
    def test_second_floor_hit_restores_range_flags_switch_and_continues(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([10, 2, 2, 10, 2, 2] + [10, 5, 1] * 3)
        meter = SimpleNamespace(connected=True, settings={"offset": 2.0},
                                read_current_power=lambda: SimpleNamespace(value=next(readings)))
        changes = []

        def change_range(name, phase, floor_count):
            changes.append((name, phase))
            return True

        results = calibrate_default_cross_path(
            controller, meter, network, 0, 4, [0, 1, 2], 0, on_range_change=change_range,
        )
        self.assertEqual(changes, [("S5.3", "sensitive"), ("S5.3", "restore_sensitive")])
        self.assertTrue(results["S5.3"].straight_at_floor)
        self.assertEqual(results["S5.3"].straight.voltage, 1)
        self.assertEqual(list(results), ["S5.3", "S4.2", "S3.2", "S1.1"])
        self.assertFalse(results["S4.2"].straight_at_floor)
        self.assertTrue(all(controller.applied_voltages[name] == 0 for name in network.switches))

    @patch("calibration_scan.time.sleep")
    def test_cancelled_range_change_stops_without_saving_floor_switch(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([10, 0, 0])
        meter = SimpleNamespace(connected=True, read_current_power=lambda: SimpleNamespace(value=next(readings)))
        with self.assertRaisesRegex(RuntimeError, "range change was cancelled"):
            calibrate_default_cross_path(
                controller, meter, network, 0, 4, [0, 1, 2], 0,
                on_range_change=lambda name, phase, count: False,
            )
        self.assertNotIn("S5.3", controller.settings.get("calibration", {}))
        self.assertTrue(all(controller.applied_voltages[name] == 0 for name in network.switches))

    @patch("calibration_scan.time.sleep")
    def test_rescan_failure_still_requests_original_range(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([10, 0, 0])
        meter = SimpleNamespace(connected=True, read_current_power=lambda: SimpleNamespace(value=next(readings)))
        phases = []

        def change_range(name, phase, floor_count):
            phases.append((phase, controller.applied_voltages[name]))
            return True

        with self.assertRaises(StopIteration):
            calibrate_default_cross_path(
                controller, meter, network, 0, 4, [0, 1, 2], 0, on_range_change=change_range,
            )
        self.assertEqual(phases, [("sensitive", 0), ("restore_sensitive", 0)])
        self.assertTrue(all(controller.applied_voltages[name] == 0 for name in network.switches))

    @patch("calibration_scan.time.sleep")
    def test_ceiling_rescan_uses_wider_range_maximum(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([10, 255, 255, 10, 20, 30] + [10, 5, 1] * 3)

        def read_power():
            value = next(readings)
            return SimpleNamespace(value=value, raw_mean=value)

        meter = SimpleNamespace(connected=True, read_current_power=read_power)
        changes = []

        def change_range(name, phase, count):
            changes.append((name, phase, count, controller.applied_voltages[name]))
            return True

        results = calibrate_default_cross_path(
            controller, meter, network, 0, 4, [0, 1, 2], 0, on_range_change=change_range,
        )
        self.assertEqual(changes, [("S5.3", "wide", 2, 0), ("S5.3", "restore_wide", 2, 0)])
        self.assertEqual(results["S5.3"].cross.voltage, 2)
        self.assertEqual(results["S5.3"].straight.voltage, 0)
        self.assertEqual([point.power for point in results["S5.3"].wide_points], [10, 20, 30])

    @patch("calibration_scan.time.sleep")
    def test_both_limits_rescan_minimum_and_maximum_separately(self, _sleep):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        readings = iter([0, 255, 0, 255, 100, 255, 1, 255, 0, 20, 0, 40]
                        + [10, 5, 3, 1] * 3)

        def read_power():
            value = next(readings)
            return SimpleNamespace(value=value, raw_mean=value)

        meter = SimpleNamespace(connected=True, read_current_power=read_power)
        phases = []

        def change_range(name, phase, count):
            phases.append(phase)
            return True

        results = calibrate_default_cross_path(
            controller, meter, network, 0, 4, [0, 1, 2, 3], 0, on_range_change=change_range,
        )
        self.assertEqual(phases, ["sensitive", "restore_sensitive", "wide", "restore_wide"])
        self.assertEqual(results["S5.3"].straight.voltage, 2)
        self.assertEqual(results["S5.3"].cross.voltage, 3)
        self.assertEqual(controller.settings["calibration"]["S5.3"], {"straight": 2, "cross": 3})

    def test_default_cross_path_calibration_requires_the_selected_zero_voltage_output(self):
        network = BenesNetwork()
        controller = FakeController()
        controller.settings["mapping"] = {name: index for index, name in enumerate(network.switches)}
        with self.assertRaisesRegex(ValueError, "reaches output 5"):
            calibrate_default_cross_path(controller, FakeMeter(), network, 0, 0, [0, 1], 0)

    def test_saturation_uses_the_meter_ceiling_below_full_scale(self):
        from calibration_scan import _read_averaged_power
        readings = iter([SimpleNamespace(value=81.0, raw_mean=81), SimpleNamespace(value=80.0, raw_mean=80)])
        meter = SimpleNamespace(ceiling=80, full_scale=127, read_current_power=lambda: next(readings))
        self.assertEqual(_read_averaged_power(meter, 2), (80.5, False, True))
        meter.read_current_power = lambda: SimpleNamespace(value=79.0, raw_mean=79)
        self.assertFalse(_read_averaged_power(meter, 1)[2])
        legacy = SimpleNamespace(full_scale=127, read_current_power=lambda: SimpleNamespace(value=127.0, raw_mean=127))
        self.assertTrue(_read_averaged_power(legacy, 1)[2])


if __name__ == "__main__":
    unittest.main()
