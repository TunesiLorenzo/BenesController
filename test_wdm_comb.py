import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path

from osa_output_switch import OSAOutputSwitch
from wdm_comb import (WDMPlan, line_power, normalized_assignment, parse_comb_lines, routed_wavelengths,
                      save_wdm_scan, scan_outputs, simulated_comb_spectrum)

PLAN = {"1": 1550.12, "2": 1550.92, "3": 1551.72, "4": None, "5": 1552.52, "6": 1553.33}
# Input 1 → output 3, input 2 → output 1, ... (0-based like BenesNetwork.mapping()).
MAPPING = (2, 0, 1, 3, 5, 4)


class WDMPlanTests(unittest.TestCase):
    def test_plan_and_presets_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            plan = WDMPlan(path)
            plan.configure({"1": "1550.12", "2": ""}, 0.3, 5, "1550.92, 1550.12")
            plan.save_preset("C-band A")
            plan.configure({"1": 1549.32}, 0.3, 5)
            reloaded = WDMPlan(path)
            self.assertEqual(reloaded.assignment["1"], 1549.32)
            self.assertEqual(reloaded.settings["settle_s"], 5)
            self.assertEqual(reloaded.load_preset("C-band A")["1"], 1550.12)
            self.assertIsNone(reloaded.assignment["2"])
            reloaded.delete_preset("C-band A")
            self.assertEqual(WDMPlan(path).settings["presets"], {})

    def test_rejects_duplicate_and_invalid_wavelengths(self):
        with self.assertRaisesRegex(ValueError, "both assigned"):
            normalized_assignment({"1": 1550.12, "3": "1550.12"})
        with self.assertRaisesRegex(ValueError, "Input 2"):
            normalized_assignment({"2": "abc"})
        with self.assertRaisesRegex(ValueError, "outside"):
            parse_comb_lines("15501")

    def test_rejects_invalid_scan_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = WDMPlan(Path(directory) / "plan.json")
            with self.assertRaisesRegex(ValueError, "Settling"):
                plan.configure({}, 0.2, -1)
            with self.assertRaisesRegex(ValueError, "window"):
                plan.configure({}, 0, 1)

    def test_routed_wavelengths_follow_the_permutation(self):
        routed = routed_wavelengths(MAPPING, normalized_assignment(PLAN))
        self.assertEqual(routed[3], (1, 1550.12))
        self.assertEqual(routed[4], (4, None))


class LinePowerTests(unittest.TestCase):
    def test_peak_and_window_power(self):
        wave = [1549.9, 1550.0, 1550.1, 1550.2, 1550.6]
        power = [-40.0, -13.0, -10.0, -13.0, -50.0]
        line = line_power(wave, power, 1550.1, 0.15)
        self.assertEqual((line.peak_nm, line.peak_dbm), (1550.1, -10.0))
        self.assertAlmostEqual(line.mean_dbm, -11.756, places=3)
        self.assertIsNone(line_power(wave, power, 1560.0, 0.2))


class WDMScanTests(unittest.TestCase):
    def test_scan_routes_waits_and_measures_every_line(self):
        with tempfile.TemporaryDirectory() as directory:
            switch = OSAOutputSwitch(Path(directory) / "switch.json", sleep=lambda _: None)
            switch.connect()
            assignment = normalized_assignment(PLAN)
            routed = routed_wavelengths(MAPPING, assignment)
            reads, waits = [], []

            def read(output):
                reads.append((output, switch.routed_output))
                return simulated_comb_spectrum(assignment, routed[output][0])

            results = scan_outputs(switch, read, [3, 1], MAPPING, assignment, 0.1, 0.25,
                                   sleep=waits.append)
            self.assertEqual(reads, [(3, 3), (1, 1)])
            self.assertAlmostEqual(sum(waits), 0.5, delta=0.11)
            first = results[0]
            self.assertEqual((first.output, first.routed_input, first.wavelength_nm), (3, 1, 1550.12))
            self.assertAlmostEqual(first.signal.peak_dbm, -10, delta=0.5)
            self.assertGreater(first.isolation_db, 25)
            self.assertEqual(set(first.lines), {1, 2, 3, 5, 6})

            folder = Path(directory) / "scan"
            context = {"permutation": [3, 1, 2, 4, 6, 5], "assignment": assignment,
                       "half_window_nm": 0.1, "settle_s": 0.25}
            files = save_wdm_scan(folder, results, context, "comb test")
            self.assertTrue(all((folder / name).exists() for name in files))
            rows = [row for row in csv.reader((folder / "wdm_summary.csv").open(encoding="utf-8"))
                    if row and not row[0].startswith("#")]
            self.assertEqual(rows[0][:6], ["output", "output_fiber", "jdsu_channel", "routed_input",
                                           "input_fiber", "wavelength_nm"])
            self.assertEqual(rows[1][:6], ["3", "6", "3", "1", "15", "1550.1200"])
            record = json.loads((folder / "wdm_scan.json").read_text(encoding="utf-8"))
            self.assertEqual(record["outputs"][1]["routed_input"], 2)

    def test_stop_event_ends_scan_during_settling(self):
        with tempfile.TemporaryDirectory() as directory:
            switch = OSAOutputSwitch(Path(directory) / "switch.json", sleep=lambda _: None)
            switch.connect()
            stop = threading.Event()
            stop.set()
            results = scan_outputs(switch, lambda _: self.fail("read after stop"), [1, 2], MAPPING,
                                   normalized_assignment(PLAN), 0.1, 10, stop_event=stop)
            self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
