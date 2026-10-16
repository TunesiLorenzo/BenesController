import tempfile
import unittest
from pathlib import Path

from spectrum_export import write_spectra


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


if __name__ == "__main__":
    unittest.main()
