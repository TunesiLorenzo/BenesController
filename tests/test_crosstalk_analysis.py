import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.measurements.crosstalk_analysis import analyze_folder, analyze_pairs, load_sweeps, power_law_exponent

HEATER = 120.0
TOTAL = 150.0
X_PI = 25.0
BIAS = 2.0
VOLTAGES = [0.5 * step for step in range(11)]


def shared_resistance_phase(voltage, resistance):
    """Victim phase change for a shared series resistance, to first order."""
    lost_mw = 2 * BIAS * HEATER / TOTAL ** 2 * resistance * voltage / TOTAL * 1e3
    return -math.pi / X_PI * lost_mw


def write_run(root, resistance=5.0):
    folder = Path(root) / "run_matrix"
    folder.mkdir()
    names = ["S1.1", "S1.2", "S2.1"]
    run = {
        "voltages_v": VOLTAGES,
        "heater_resistance_ohm": {name: HEATER for name in names},
        "total_resistance_ohm": {name: TOTAL for name in names},
        "calibration": {name: {"straight": 2.0, "cross": 0.0} for name in names},
        "victims": [{"mzi": "S1.1", "input": 1, "output": 5,
                     "fit": {"x_pi": X_PI, "x_unit": "mW"}}],
        "pairs": [{"aggressor": "S1.2", "victim": "S1.1", "input": 1, "output": 5,
                   "bias_voltage_v": BIAS, "kappa": -0.03}],
    }
    (folder / "crosstalk_matrix.json").write_text(json.dumps(run), encoding="utf-8")
    lines = ["# sweeps", "swept,victim,input,output,voltage_v,victim_phase_shift_rad"]
    lines += [f"S1.2,S1.1,1,5,{voltage},{shared_resistance_phase(voltage, resistance)}" for voltage in VOLTAGES]
    lines += [f"S1.1,S1.1,1,5,{voltage}," for voltage in VOLTAGES]
    (folder / "crosstalk_sweeps.csv").write_text("\n".join(lines), encoding="utf-8")
    return folder, run


class CrosstalkAnalysisTests(unittest.TestCase):
    def test_power_law_exponent_recovers_shape(self):
        self.assertAlmostEqual(power_law_exponent(VOLTAGES[1:], [2 * v for v in VOLTAGES[1:]]), 1.0)
        self.assertAlmostEqual(power_law_exponent(VOLTAGES[1:], [v ** 2 for v in VOLTAGES[1:]]), 2.0)

    def test_shared_resistance_is_recovered_from_voltage_slope(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, run = write_run(temporary, resistance=5.0)
            pairs = analyze_pairs(run, load_sweeps(folder / "crosstalk_sweeps.csv"))

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].relation, "same stage")
        self.assertAlmostEqual(pairs[0].exponent, 1.0)
        self.assertAlmostEqual(pairs[0].shared_resistance_ohm, 5.0)
        self.assertGreater(pairs[0].r2_voltage, pairs[0].r2_power)

    def test_analyze_folder_writes_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, _run = write_run(temporary)
            paths = analyze_folder(folder)
            self.assertTrue(all(path.is_file() for path in paths.values()))


if __name__ == "__main__":
    unittest.main()
