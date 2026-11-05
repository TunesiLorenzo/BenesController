import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.measurements.recalibrate import apply, recalibrate


class RecalibrateTests(unittest.TestCase):
    def test_saved_sweep_is_re_extracted_and_applied_with_a_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            voltages = [round(0.1 * step, 1) for step in range(51)]
            # Null at 0.78 V, between two points; the first peak at 2.33 V.
            x_pi = 2.33 ** 2 - 0.78 ** 2
            rows = [f"calibration,S1.1,1,5,{v},{1e-4 + 0.01 * math.cos(math.pi * (v * v - 0.78 ** 2) / x_pi / 2 + math.pi / 2) ** 2},,"
                    for v in voltages]
            (folder / "mzi_sweeps.csv").write_text(
                "# MZI calibration\nsweep,mzi,input,output,voltage_v,power_mW,power_dbm,reference_mW\n"
                + "\n".join(rows) + "\n", encoding="utf-8")
            (folder / "mzi_calibration.json").write_text(json.dumps({
                "unit": "mW", "voltages_v": voltages, "mzi_calibrations": {"S1.1": {
                    "input": 1, "output": 5, "bright_state": "bar",
                    "cross_voltage_v": 0.0, "bar_voltage_v": 4.1}}}), encoding="utf-8")
            [result] = recalibrate(folder / "mzi_calibration.json")
            self.assertEqual((result.cross, result.bar, result.method), (0.7, 2.3, "fit"))

            config = folder / "benes_voltage_config.json"
            config.write_text("{}", encoding="utf-8")
            backup = apply([result], config)
            self.assertEqual(backup.read_text(encoding="utf-8"), "{}")
            saved = json.loads(config.read_text(encoding="utf-8"))["calibration"]["S1.1"]
            self.assertEqual(saved, {"straight": 2.3, "cross": 0.7})


if __name__ == "__main__":
    unittest.main()
