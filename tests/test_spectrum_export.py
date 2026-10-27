import tempfile
import unittest
from pathlib import Path

from benes.measurements.calibration_scan import ScanPoint, SpectrumScanPoint
from benes.measurements.spectrum_export import write_crosstalk_csv, write_spectra


class SpectrumExportTests(unittest.TestCase):
    def test_export_keeps_all_traces_and_description_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "traces.csv"
            write_spectra(path, [("Before", [1550.0, 1551.0], [-20.0, -21.0]),
                                 ("After", [1550.0, 1551.0], [-18.0, -19.0])],
                          "MZI S1.1 calibration")
            content = path.read_text(encoding="utf-8")
        self.assertIn("# Description: MZI S1.1 calibration", content)
        self.assertIn("wavelength_nm,Before [dBm],After [dBm]", content)
        self.assertIn("1550.0,-20.0,-18.0", content)

    def test_crosstalk_export_keeps_comment_voltage_and_full_osa_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crosstalk.csv"
            data = {"S1.1": [SpectrumScanPoint(1.0, -20.0, (1550.0, 1551.0), (-30.0, -20.0))],
                    "S1.2": [ScanPoint(0.0, 12.5)]}
            write_crosstalk_csv(path, data, "OSA", "Input 1, output 2", "dBm")
            content = path.read_text(encoding="utf-8")
        self.assertIn("# Description: Input 1, output 2", content)
        self.assertIn("# Response unit: dBm", content)
        self.assertIn("switch,voltage_v,peak_power_dbm,mean_power_dbm,wavelength_nm,spectrum_power_dbm", content)
        self.assertIn("# Final OSA bins discarded per trace: 2", content)
        self.assertIn("S1.1,1.0,-20.0,", content)
        self.assertIn(",1551.0,-20.0", content)
        self.assertIn("S1.2,0.0,12.5,,,", content)


if __name__ == "__main__":
    unittest.main()
