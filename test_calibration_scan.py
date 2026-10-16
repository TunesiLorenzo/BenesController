import unittest
from types import SimpleNamespace
from unittest.mock import patch

from calibration_scan import (scan_switch, scan_switches, scan_switches_with_reader,
                              scan_voltages, spectrum_scan_point)


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
            lambda voltage: spectrum_scan_point(voltage, ([1550.0, 1551.0], [-40.0, -20.0])),
        )
        self.assertEqual([point.power for point in results["S1.1"]], [-20.0, -20.0])
        self.assertEqual(results["S1.1"][0].spectrum_dbm, (-40.0, -20.0))
        self.assertEqual(controller.sent, [-1, 1, 0])

    def test_osa_reader_rejects_bad_spectrum(self):
        with self.assertRaisesRegex(ValueError, "mismatched"):
            spectrum_scan_point(0, ([1550.0], []))


if __name__ == "__main__":
    unittest.main()
