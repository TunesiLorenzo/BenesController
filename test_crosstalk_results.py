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
        self.assertIn("S1.1,-1,-20,", csv_text)
        self.assertIn(",1551.0,-20.0", csv_text)

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

    def test_calibrations_and_crosstalk_scans_use_separate_subfolders(self):
        network = BenesNetwork()
        controller = SimpleNamespace(settings={"mapping": {}}, applied_voltages={})
        context = scan_context(network, 2, 1, None, None, controller, ["S2.1"], [0, 1], 0, "Arduino", "")
        with tempfile.TemporaryDirectory() as directory:
            scan = create_result_folder(Path(directory), context)
            context["calibration_mode"] = "default_cross_path"
            calibration = create_result_folder(Path(directory), context)
            self.assertEqual(scan.parent.name, "Crosstalk_scan")
            self.assertEqual(calibration.parent.name, "Path_calibration")

    def test_saved_arduino_and_osa_folders_load_back_with_rescans(self):
        from crosstalk_results import load_scan_folder

        network = BenesNetwork()
        controller = SimpleNamespace(settings={"mapping": {}}, applied_voltages={},
                                     heater_resistance=lambda name: 800.0)
        with tempfile.TemporaryDirectory() as directory:
            context = scan_context(network, 2, 1, None, None, controller, ["S2.1", "S3.1"], [0, 1], 0,
                                   "Arduino", "two\nlines")
            context["sensitive_range_rescans"] = {"S2.1": [{"voltage_v": 0, "power": 40}]}
            folder = create_result_folder(Path(directory), context)
            data = {"S2.1": [ScanPoint(0, 10), ScanPoint(1, 12)], "S3.1": [ScanPoint(0, 5)]}
            save_scan_artifacts(folder, context, data, "Arduino", "uW", network)
            loaded = load_scan_folder(folder)
            self.assertEqual(loaded["data"], data)
            self.assertEqual((loaded["source"], loaded["unit"], loaded["comment"]), ("Arduino", "uW", "two\nlines"))
            self.assertEqual(loaded["context"]["sensitive_range_rescans"]["S2.1"][0]["power"], 40)
            self.assertEqual(loaded["context"]["heater_resistance_ohm"]["S3.1"], 800.0)

            osa_context = scan_context(network, 0, 3, 1, 4, controller, ["S1.1"], [-1, 1], 50, "OSA", "")
            osa_folder = create_result_folder(Path(directory), osa_context)
            spectra = {"S1.1": [SpectrumScanPoint(-1, -20, (1550.0, 1551.0), (-30.0, -20.0)),
                                SpectrumScanPoint(1, -18, (1550.0, 1551.0), (-25.0, -18.0))]}
            save_scan_artifacts(osa_folder, osa_context, spectra, "OSA", "dBm", network)
            self.assertEqual(load_scan_folder(osa_folder)["data"], spectra)

            # Older OSA datasets used a single response_power column.
            (osa_folder / "crosstalk_dataset.csv").write_text("\n".join((
                "# Crosstalk OSA scan exported 2026-10-16",
                "# Response unit: dBm",
                "switch,voltage_v,response_power,wavelength_nm,spectrum_power_dbm",
                "S1.1,0.0,-22.5,1515.0,-48.1",
                "S1.1,0.0,-22.5,1515.1,-22.5",
                "S1.1,0.5,-21.0,1515.0,-21.0",
            )) + "\n", encoding="utf-8")
            old = load_scan_folder(osa_folder)["data"]["S1.1"]
            self.assertEqual([(p.voltage, p.power, len(p.spectrum_dbm)) for p in old],
                             [(0.0, -22.5, 2), (0.5, -21.0, 1)])
        with self.assertRaisesRegex(ValueError, "base_setting.json"):
            load_scan_folder(Path(directory))

    def test_power_axis_uses_v_squared_over_r_and_falls_back_to_volts(self):
        from crosstalk_results import PLOT_X_POWER, PLOT_X_VOLTAGE, plot_x_values
        xs, label = plot_x_values([0, 1, 2], 1000, PLOT_X_POWER)
        self.assertEqual(xs, [0, 1, 4])
        self.assertIn("Heater power [mW]", label)
        self.assertEqual(plot_x_values([0, 2], None, PLOT_X_POWER),
                         ([0, 2], "Voltage [V] (no heater resistance set)"))
        self.assertEqual(plot_x_values([0, 2], 1000, PLOT_X_VOLTAGE), ([0, 2], "Voltage [V]"))
        # With routing resistance, I = V / R_total and the heater dissipates I^2 R_heater.
        xs, label = plot_x_values([2], 150, PLOT_X_POWER, 200)
        self.assertAlmostEqual(xs[0], 15.0)
        self.assertIn("R total = 200", label)

    def test_saved_figure_follows_power_axis_mode(self):
        from unittest.mock import patch
        from matplotlib.figure import Figure

        network = BenesNetwork()
        controller = SimpleNamespace(settings={"mapping": {}}, applied_voltages={},
                                     heater_resistance=lambda name: 500.0)
        context = scan_context(network, 2, 1, None, None, controller, ["S2.1"], [0, 1], 0,
                               "Arduino", "")
        self.assertEqual(context["heater_resistance_ohm"], {"S2.1": 500.0})
        context["plot_x_axis"] = "power"
        figures = []

        def recording_figure(*args, **kwargs):
            figures.append(Figure(*args, **kwargs))
            return figures[-1]

        with tempfile.TemporaryDirectory() as directory, \
                patch("matplotlib.figure.Figure", side_effect=recording_figure):
            folder = create_result_folder(Path(directory), context)
            save_scan_artifacts(folder, context, {"S2.1": [ScanPoint(0, 10), ScanPoint(1, 12)]},
                                "Arduino", "uW", network)
        response = figures[-1].axes[0]
        self.assertEqual(list(response.get_lines()[0].get_xdata()), [0, 2])
        self.assertIn("Heater power", response.get_xlabel())

    def test_saved_figure_draws_range_rescans_on_their_own_axis(self):
        from unittest.mock import patch
        from matplotlib.figure import Figure

        network = BenesNetwork()
        controller = SimpleNamespace(settings={"mapping": {}}, applied_voltages={})
        context = scan_context(network, 2, 1, None, None, controller, ["S2.1"], [0, 1], 0,
                               "Arduino", "")
        context["sensitive_range_rescans"] = {"S2.1": [{"voltage_v": 0, "power": 40},
                                                       {"voltage_v": 1, "power": 3}]}
        context["wide_range_rescans"] = {"S2.1": [{"voltage_v": 0, "power": 20}]}
        figures = []

        def recording_figure(*args, **kwargs):
            figures.append(Figure(*args, **kwargs))
            return figures[-1]

        with tempfile.TemporaryDirectory() as directory, \
                patch("matplotlib.figure.Figure", side_effect=recording_figure):
            folder = create_result_folder(Path(directory), context)
            save_scan_artifacts(folder, context, {"S2.1": [ScanPoint(0, 10), ScanPoint(1, 12)]},
                                "Arduino", "uW", network)
        response, rescan = figures[-1].axes
        self.assertEqual([line.get_label() for line in rescan.get_lines()],
                         ["Sensitive-range rescan", "Wider-range rescan"])
        self.assertEqual(len(response.get_legend().get_texts()), 3)


if __name__ == "__main__":
    unittest.main()
