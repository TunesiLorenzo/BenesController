import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.measurements.calibration_analysis import (find_calibration_files, leakage_db, load_calibration,
                                                     summary_rows, write_analysis_outputs)


V_PI_SQUARED = 5.0
R_EFF_OHM = 200.0
VOLTAGES = [round(0.1 * step, 1) for step in range(51)]


def power(voltage, phase0, peak=0.01, floor=1e-7):
    return floor + peak * (1 + math.cos(math.pi * voltage * voltage / V_PI_SQUARED + phase0)) / 2


def write_run(folder, mzis):
    """``mzis`` maps name -> (input, output, bright_state, phase0, cross_v, bar_v)."""
    folder.mkdir(parents=True)
    meta = {"started_at": "2026-11-02T11:45:31+01:00", "meter": "HP 8152A", "unit": "mW",
            "voltages_v": VOLTAGES, "calibration_before": {}, "mzi_calibrations": {}}
    lines = ["# MZI calibration saved", "sweep,mzi,input,output,voltage_v,power_mW,power_dbm,reference_mW"]
    for name, (input_port, output, bright, phase0, cross_v, bar_v) in mzis.items():
        meta["mzi_calibrations"][name] = {
            "mzi": name, "input": input_port, "output": output, "bright_state": bright,
            "cross_voltage_v": cross_v, "bar_voltage_v": bar_v,
            "fit": {"x_pi": V_PI_SQUARED / R_EFF_OHM * 1000, "x_unit": "mW"}}
        meta["calibration_before"][name] = {"straight": 2.0, "cross": 0.0}
        for voltage in VOLTAGES:
            value = power(voltage, phase0)
            lines.append(f"calibration,{name},{input_port},{output},{voltage},{value},"
                         f"{10 * math.log10(value)},")
    lines.append(f"calibration,{name},{input_port},{output},5.1,,,")  # a missed reading
    (folder / "mzi_calibration.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "mzi_sweeps.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder / "mzi_calibration.json"


class CalibrationAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # S1.1 is bright in cross and 0.2 rad past cross at 0 V. S2.1 is bright in
        # bar, π + 0.05 rad from its bright state at 0 V, with bar saved one fringe late.
        self.json_path = write_run(self.root / "Calibration1" / "run", {
            "S1.1": (1, 5, "cross", 0.2, 0.0, 2.2),
            "S2.1": (4, 2, "bar", math.pi + 0.05, 0.0, 3.9),
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_finds_nested_calibration(self):
        self.assertEqual(find_calibration_files(self.root), [self.json_path])

    def test_extremes_and_heater_figures(self):
        run = load_calibration(self.json_path)
        self.assertEqual([mzi.name for mzi in run.mzis], ["S1.1", "S2.1"])
        mzi = run.mzis[0]
        self.assertEqual(len(mzi.voltages), 51)
        best = max(VOLTAGES, key=lambda v: power(v, 0.2))
        self.assertAlmostEqual(mzi.max_v, best)
        self.assertAlmostEqual(mzi.max_mw, power(best, 0.2))
        self.assertGreater(mzi.extinction_db, 20)  # the 0.1 V grid misses the null
        self.assertAlmostEqual(mzi.v_pi, math.sqrt(V_PI_SQUARED), places=2)
        self.assertAlmostEqual(mzi.r_eff_ohm, R_EFF_OHM, delta=2)
        self.assertAlmostEqual(mzi.phase_error_deg(0.0, "cross"), math.degrees(0.2), delta=1)
        expected_bar = math.sqrt((math.pi - 0.2) / math.pi * V_PI_SQUARED)
        self.assertAlmostEqual(mzi.exact_voltage("bar"), expected_bar, places=2)
        expected_cross = math.sqrt((2 * math.pi - 0.2) / math.pi * V_PI_SQUARED)
        self.assertAlmostEqual(mzi.exact_voltage("cross"), expected_cross, places=2)

    def test_bright_bar_phase_and_later_fringe_flag(self):
        run = load_calibration(self.json_path)
        mzi = run.mzis[1]
        self.assertAlmostEqual(mzi.phase_error_deg(0.0, "cross"), math.degrees(0.05), delta=1)
        self.assertAlmostEqual(mzi.exact_voltage("bar"), math.sqrt((math.pi - 0.05) / math.pi * V_PI_SQUARED),
                               places=2)
        self.assertTrue(any("later fringe" in note for note in mzi.flags(run.step_v)))
        self.assertFalse(any("later fringe" in note for note in run.mzis[0].flags(run.step_v)))

    def test_leakage(self):
        self.assertAlmostEqual(leakage_db(math.pi), 0.0)
        self.assertAlmostEqual(leakage_db(math.radians(28.5)), -12.2, delta=0.05)
        self.assertEqual(leakage_db(0.0), -60.0)

    def test_writes_outputs(self):
        run = load_calibration(self.json_path)
        paths = write_analysis_outputs(run, self.root / "Analysis")
        for path in paths.values():
            self.assertTrue(path.exists(), path)
        rows = summary_rows(run)
        self.assertEqual(rows[0]["bar_before_v"], "2.00")
        self.assertIn("S2.1", paths["summary_markdown"].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
