import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from calibration_scan import ScanPoint, SpectrumScanPoint
from crosstalk_results import create_result_folder, save_scan_artifacts, scan_context
from network import BenesNetwork


class CrosstalkResultsTests(unittest.TestCase):
    def test_exports_folder_figures_csv_and_base_path_snapshot(self):
        network = BenesNetwork()
        controller = SimpleNamespace(
            settings={"mapping": {name: index for index, name in enumerate(network.switches)}},
            applied_voltages={"S1.1": 2.5},
        )
        context = scan_context(network, 0, 3, 1, 4, controller, ["S1.1"], [-1, 1], 50,
                               "OSA", "Input one test")
        with tempfile.TemporaryDirectory() as directory:
            folder = create_result_folder(Path(directory), context)
            data = {"S1.1": [SpectrumScanPoint(-1, -20, (1550.0, 1551.0), (-30.0, -20.0)),
                             SpectrumScanPoint(1, -18, (1550.0, 1551.0), (-25.0, -18.0))]}
            save_scan_artifacts(folder, context, data, "OSA", "dBm", network)
            summary = json.loads((folder / "base_setting.json").read_text(encoding="utf-8"))
            csv_text = (folder / "crosstalk_dataset.csv").read_text(encoding="utf-8")
            self.assertTrue((folder / "base_path.png").stat().st_size > 0)
            self.assertTrue((folder / "S1_1.png").stat().st_size > 0)
        self.assertTrue(folder.name.startswith("Port1-Port4_"))
        self.assertEqual(summary["input_port"], 1)
        self.assertEqual(summary["selected_output_port"], 4)
        self.assertEqual(summary["power_meter_output_port"], 2)
        self.assertEqual(summary["osa_output_port"], 5)
        self.assertEqual(summary["applied_voltages_before_scan"]["S1.1"], 2.5)
        self.assertIn("# Description: Input one test", csv_text)
        self.assertIn("S1.1,-1,-20,1551.0,-20.0", csv_text)

    def test_arduino_response_creates_one_figure(self):
        network = BenesNetwork()
        controller = SimpleNamespace(settings={"mapping": {}}, applied_voltages={})
        context = scan_context(network, 2, 1, None, None, controller, ["S2.1"], [0, 1], 0,
                               "Arduino", "")
        with tempfile.TemporaryDirectory() as directory:
            folder = create_result_folder(Path(directory), context)
            save_scan_artifacts(folder, context, {"S2.1": [ScanPoint(0, 10), ScanPoint(1, 12)]},
                                "Arduino", "uW", network)
            self.assertTrue((folder / "S2_1.png").exists())
            self.assertIn("# Response unit: uW", (folder / "crosstalk_dataset.csv").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
