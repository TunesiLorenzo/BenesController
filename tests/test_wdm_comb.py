import csv
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from benes.hardware.osa_output_switch import OSAOutputSwitch
from benes.measurements.wdm_comb import (LinePower, WDMOutputResult, WDMPlan,
                                         acquire_averaged_spectrum, average_spectra, evaluate_configuration,
                                         line_power, normalized_assignment, parse_comb_lines,
                                         power_ratio_db, routed_wavelengths, save_configuration_evaluation,
                                         save_wdm_scan, scan_outputs,
                                         simulated_comb_spectrum)

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
            self.assertEqual(reloaded.settings["evaluation_sweeps"], 10)
            plan.configure({"1": 1550.12}, 0.3, 5, evaluation_sweeps="5")
            self.assertEqual(WDMPlan(path).settings["evaluation_sweeps"], 5)
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
            with self.assertRaisesRegex(ValueError, "Evaluation sweeps"):
                plan.configure({}, 0.2, 1, evaluation_sweeps="2.5")

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


class SpectrumAveragingTests(unittest.TestCase):
    def test_averages_linear_power_and_waits_for_new_sweeps(self):
        fresh_counts = []
        progress = []
        powers = iter([-10.0, -20.0, -10.0])

        def fetch(fresh):
            fresh_counts.append(fresh)
            return [1550.0], [next(powers)]

        wave, power = acquire_averaged_spectrum(fetch, 3, on_sweep=lambda index, total: progress.append((index, total)))
        self.assertEqual(fresh_counts, [2, 1, 1])
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(wave, [1550.0])
        self.assertAlmostEqual(power[0], 10 * math.log10((0.1 + 0.01 + 0.1) / 3))

    def test_rejects_misaligned_sweep_grids(self):
        with self.assertRaisesRegex(ValueError, "wavelength grid"):
            average_spectra([([1550.0], [-10.0]), ([1550.1], [-10.0])])


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
            with (folder / "wdm_summary.csv").open(encoding="utf-8", newline="") as file:
                rows = [row for row in csv.reader(file) if row and not row[0].startswith("#")]
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

    def test_configuration_summary_has_transmission_total_and_per_port_crosstalk(self):
        with tempfile.TemporaryDirectory() as directory:
            switch = OSAOutputSwitch(Path(directory) / "switch.json", sleep=lambda _: None)
            switch.connect()
            assignment = normalized_assignment(PLAN)
            routed = routed_wavelengths(MAPPING, assignment)
            results = scan_outputs(
                switch, lambda output: simulated_comb_spectrum(assignment, routed[output][0]),
                range(1, 7), MAPPING, assignment, 0.1, 0, sleep=lambda _: None)
            summary = evaluate_configuration(results, MAPPING, assignment)
            line = next(item for item in summary if item.input_port == 1)
            self.assertEqual(line.expected_output, 3)
            self.assertAlmostEqual(line.transmission_dbm, -10, delta=0.5)
            self.assertAlmostEqual(line.total_power_dbm, -9.9, delta=0.5)
            self.assertGreater(line.normalized_transmission, 0.99)
            self.assertIsNone(line.crosstalk_dbc[3])
            self.assertLess(line.crosstalk_dbc[1], -25)
            self.assertLess(line.normalized_crosstalk[1], 0.01)

    def test_configuration_total_adds_linear_output_power_before_db_conversion(self):
        results = [WDMOutputResult(output, output, 1, 1550.0, [], [],
                                   {1: LinePower(dbm, 1550.0, dbm)})
                   for output, dbm in ((1, -10.0), (2, -10.0), (3, -20.0))]
        line = evaluate_configuration(results, (0, 1, 2, 3, 4, 5), {"1": 1550.0})[0]
        self.assertAlmostEqual(line.total_power_dbm, 10 * math.log10(0.21))
        self.assertAlmostEqual(line.normalized_transmission, 10 / 21)
        self.assertAlmostEqual(line.normalized_transmission +
                               sum(value for value in line.normalized_crosstalk.values() if value is not None), 1.0)
        self.assertAlmostEqual(power_ratio_db(line.normalized_transmission),
                               line.transmission_dbm - line.total_power_dbm)
        self.assertAlmostEqual(power_ratio_db(line.normalized_crosstalk[3]),
                               -20.0 - line.total_power_dbm)
        self.assertIsNone(power_ratio_db(None))

    def test_configuration_export_uses_scan_start_controls_and_evaluation_values(self):
        results = [WDMOutputResult(output, output, 1, 1550.0, [], [],
                                   {1: LinePower(dbm, 1550.0, dbm)})
                   for output, dbm in ((1, -10.0), (2, -20.0))]
        summary = evaluate_configuration(results, (0, 1, 2, 3, 4, 5), {"1": 1550.0})
        context = {"switch_states": {"S1.1": "cross", "S1.2": "bar"},
                   "switch_voltages_v": {"S1.1": 2.5, "S1.2": 0.0}}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files = save_configuration_evaluation(folder, summary, context)
            self.assertEqual(files, ["configuration_evaluation.csv", "configuration_controls.csv"])
            with (folder / files[0]).open(encoding="utf-8", newline="") as file:
                evaluation = list(csv.DictReader(file))
            self.assertEqual(evaluation[0]["input"], "1")
            self.assertEqual(evaluation[0]["expected_output"], "1")
            self.assertEqual(evaluation[0]["out1_crosstalk_to_total_db"], "")
            self.assertAlmostEqual(float(evaluation[0]["out2_crosstalk_to_total_db"]),
                                   power_ratio_db(summary[0].normalized_crosstalk[2]), places=3)
            with (folder / files[1]).open(encoding="utf-8", newline="") as file:
                controls = list(csv.DictReader(file))
            self.assertEqual(controls, [
                {"switch": "S1.1", "state": "cross", "voltage_v": "2.5"},
                {"switch": "S1.2", "state": "bar", "voltage_v": "0.0"},
            ])


if __name__ == "__main__":
    unittest.main()
