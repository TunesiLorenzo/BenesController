"""The batch viewer must mark the routed wavelength on the correct output."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from benes.core.network import BenesNetwork
from benes.measurements.dataset_view import (format_transmission_share, load_output_spectra,
                                             load_target_summary,
                                             saved_dataset_scans, saved_scan_entry)


class DatasetViewTests(unittest.TestCase):
    def test_loads_each_output_with_its_own_routed_target(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            outputs = [{"output": output, "routed_input": 7 - output,
                        "wavelength_nm": 1550 + output,
                        "lines": {str(input_port): {"peak_dbm": 0 if input_port == 7 - output else -10}
                                  for input_port in range(1, 7)}} for output in range(1, 7)]
            (folder / "wdm_scan.json").write_text(
                json.dumps({"permutation": [6, 5, 4, 3, 2, 1], "outputs": outputs}), encoding="utf-8")
            with (folder / "wdm_spectra.csv").open("w", encoding="utf-8", newline="") as file:
                file.write("# saved trace\n")
                writer = csv.writer(file)
                writer.writerow(["wavelength_nm"] +
                                [f"Out {output} (In {7 - output}) [dBm]" for output in range(6, 0, -1)])
                writer.writerow([1550.0] + [-output for output in range(6, 0, -1)])
                writer.writerow([1551.0] + [-2 * output for output in range(6, 0, -1)])
            spectra = load_output_spectra(folder)
            self.assertEqual([item[:3] for item in spectra],
                             [(output, 7 - output, 1550 + output) for output in range(1, 7)])
            self.assertEqual(spectra[0][3:], ([1550.0, 1551.0], [-1.0, -2.0]))
            self.assertEqual(spectra[5][3:], ([1550.0, 1551.0], [-6.0, -12.0]))

            with (folder / "wdm_summary.csv").open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["output", "routed_input", "peak_power_dbm", "window_mean_dbm"])
                for output in range(1, 7):
                    writer.writerow([output, 7 - output, -10 - output, -12 - output])
            permutation, summary = load_target_summary(folder)
            self.assertEqual(permutation, (6, 5, 4, 3, 2, 1))
            self.assertEqual(summary[0][:5], (1, 6, 1551.0, -11.0, -13.0))
            self.assertEqual(summary[5][:5], (6, 1, 1556.0, -16.0, -18.0))
            self.assertAlmostEqual(summary[0][5], 2 / 3)
            self.assertEqual(format_transmission_share(summary[0][5]), "-1.76 dB (66.7%)")

    def test_indexes_previous_runs_by_permutation_and_path_alternative(self):
        network = BenesNetwork()
        permutation = (0, 1, 2, 3, 4, 5)
        paths = network.equivalent_permutation_states(permutation)
        self.assertGreater(len(paths), 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folders = [root / "Measurements" / f"20261102_12000{index}_000001_osa wdm"
                       for index in (1, 2, 3)]
            for folder, states in zip(folders, (paths[0], paths[1], paths[0])):
                folder.mkdir(parents=True)
                context = {
                    "permutation": [1, 2, 3, 4, 5, 6],
                    "switch_states": {name: "cross" if crossed else "bar"
                                      for name, crossed in states.items()},
                    "outputs": [{"output": output} for output in range(1, 7)],
                }
                (folder / "wdm_scan.json").write_text(json.dumps(context), encoding="utf-8")
                (folder / "wdm_summary.csv").touch()
                (folder / "wdm_spectra.csv").touch()
            entries = saved_dataset_scans(root)
            self.assertEqual([entry["number"] for entry in entries], [1, 1, 1])
            self.assertEqual([entry["alternative"] for entry in entries],
                             [(1, len(paths)), (1, len(paths)), (2, len(paths))])
            self.assertEqual(len({entry["label"] for entry in entries}), 3)
            self.assertIn("Path 2/", saved_scan_entry(folders[1])["label"])


if __name__ == "__main__":
    unittest.main()
