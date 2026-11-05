import math
import tempfile
import unittest
from pathlib import Path

from benes.measurements.calibration_analysis import CalibrationRun, MZIResult
from benes.measurements.calibration_compare import compare_runs, table_rows, write_comparison
from benes.measurements.mzi_fit import fit_mzi_response

VOLTAGES = tuple(round(0.1 * step, 1) for step in range(51))
P_PI = 5.0  # V² for a π phase


def result(name, cross_v, bar_v, input_port=1, output_port=5):
    # Bright (cross) at 0 V², dark (bar) at P_PI, repeating every 2 P_PI.
    powers = tuple(0.01 + (1 + math.cos(math.pi * v * v / P_PI)) / 2 for v in VOLTAGES)
    return MZIResult(name, input_port, output_port, "cross", VOLTAGES, powers, cross_v, bar_v, None, None,
                     fit_mzi_response([v * v for v in VOLTAGES], powers, "V²"), None)


def run(*mzis):
    return CalibrationRun(Path("."), "", "", 0.1, mzis)


class CompareTests(unittest.TestCase):
    def test_the_same_state_a_fringe_away_agrees_and_another_state_differs(self):
        # cross at 0 V² and at 2 P_PI (3.16 V) is the same state; bar at 1.5 P_PI is not bar.
        rows = compare_runs(run(result("S1.1", 0.0, math.sqrt(P_PI))),
                            run(result("S1.1", math.sqrt(2 * P_PI), math.sqrt(1.5 * P_PI), 2, 6)))
        (row,) = table_rows(rows, 0.1)
        self.assertEqual(row["cross_verdict"], "agree, other fringe")
        self.assertEqual(row["bar_verdict"], "differ")
        self.assertAlmostEqual(abs(float(row["bar_phase_rad"])), math.pi / 2, delta=0.05)
        self.assertEqual((row["first_path"], row["second_path"]), ("In 1 → Out 5", "In 2 → Out 6"))
        self.assertAlmostEqual(float(row["p_pi_first_v2"]), P_PI, delta=0.05)

    def test_one_step_apart_agrees_and_a_missing_voltage_is_reported(self):
        (row,) = table_rows(compare_runs(run(result("S1.1", 0.5, 2.2)), run(result("S1.1", 0.6, None))), 0.1)
        self.assertEqual((row["cross_verdict"], row["bar_verdict"]), ("agree", "missing"))

    def test_writes_a_csv_with_both_folders(self):
        rows = table_rows(compare_runs(run(result("S1.1", 0.0, 2.2)), run(result("S1.1", 0.0, 2.2))), 0.1)
        with tempfile.TemporaryDirectory() as folder:
            path = write_comparison(Path(folder) / "comparison.csv", run(), run(), rows)
            text = path.read_text(encoding="utf-8")
        self.assertIn("# First calibration", text)
        self.assertIn("cross_verdict", text)


if __name__ == "__main__":
    unittest.main()
