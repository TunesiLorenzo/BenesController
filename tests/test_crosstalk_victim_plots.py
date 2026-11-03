import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.measurements.crosstalk_victim_plots import load_run, main, operating_points, save_victim_figures

HEATER = 120.0
TOTAL = 150.0
X_PI = 25.0
VOLTAGES = [0.5 * step for step in range(11)]
FIT = {"offset": 1e-5, "amplitude": 2e-3, "x_pi": X_PI, "phase0_rad": 0.1, "rms": 1e-5, "x_unit": "mW"}


def drive(voltage):
    return voltage * voltage * HEATER / TOTAL ** 2 * 1000


def victim_power(voltage):
    return FIT["offset"] + FIT["amplitude"] * (1 + math.cos(math.pi * drive(voltage) / X_PI + FIT["phase0_rad"])) / 2


def write_run(root, with_json=True):
    folder = Path(root) / "run_matrix"
    folder.mkdir()
    names = ["S1.1", "S1.2", "S2.1"]
    run = {
        "unit": "mW",
        "status": "stopped",
        "heater_resistance_ohm": {name: HEATER for name in names},
        "total_resistance_ohm": {name: TOTAL for name in names},
        "victims": [{"mzi": "S1.1", "input": 1, "output": 5, "bright_state": "cross", "fit": FIT},
                    # The run stopped before this probe's sweep was saved.
                    {"mzi": "S2.1", "input": 4, "output": 2, "bright_state": "bar", "fit": None}],
        "pairs": [{"aggressor": "S1.2", "victim": "S1.1", "input": 1, "output": 5, "bias_voltage_v": 2.0,
                   "x_unit": "mW", "kappa": -0.03, "contamination": None}],
        "failed_pairs": [{"aggressor": "S1.1", "victim": "S1.2", "reason": "only 2.0 dB change"}],
    }
    if with_json:
        (folder / "crosstalk_matrix.json").write_text(json.dumps(run), encoding="utf-8")
    lines = ["# Thermal crosstalk sweeps", "# Comment: ",
             "swept,victim,input,output,voltage_v,power_mW,power_dbm,reference_mW,aggressor_drive,drive_unit,"
             "victim_phase_shift_rad"]
    for voltage in VOLTAGES:
        power = "" if voltage == 1.0 else victim_power(voltage)  # one missed reading
        lines.append(f"S1.1,S1.1,1,5,{voltage},{power},,,,,")
    for voltage in VOLTAGES:
        lines.append(f"S1.2 (victim bright),S1.1,1,5,{voltage},0.002,,,{drive(voltage)},mW,")
        shift = "" if voltage == 5.0 else -0.001 * drive(voltage)  # one folded-back point
        lines.append(f"S1.2,S1.1,1,5,{voltage},0.001,,,{drive(voltage)},mW,{shift}")
    # A pair the JSON does not list, cut short by the stop.
    lines += [f"S2.1,S1.1,1,5,{voltage},0.001,,,{drive(voltage)},mW,0.0" for voltage in VOLTAGES[:3]]
    (folder / "crosstalk_sweeps.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


class CrosstalkVictimPlotsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_sweeps_fit_and_pairs(self):
        run = load_run(write_run(self.root))
        probe = run.probes["S1.1", 1, 5]
        self.assertEqual(len(probe.points), 11)
        self.assertIsNone(next(point for point in probe.points if point.voltage == 1.0).power)
        self.assertAlmostEqual(probe.fit.x_pi, X_PI)
        self.assertEqual(probe.bias_voltage, 2.0)
        pair = run.pairs["S1.2", "S1.1", 1, 5]
        self.assertEqual(len(pair.drive), 11)  # the bright reference rows are not mixed in
        self.assertIsNone(pair.phase_shift[-1])
        self.assertAlmostEqual(pair.kappa, -0.03)
        unlisted = run.pairs["S2.1", "S1.1", 1, 5]
        self.assertEqual(len(unlisted.drive), 3)
        self.assertIsNone(unlisted.kappa)
        self.assertEqual(run.victims(), ["S1.1", "S1.2", "S2.1"])
        self.assertEqual(run.failed["S1.2"], {"only 2.0 dB change": ["S1.1"]})
        self.assertTrue(any("S2.1" in warning and "no victim sweep" in warning for warning in run.warnings))

    def test_operating_points_follow_the_fit(self):
        run = load_run(write_run(self.root))
        probe = run.probes["S1.1", 1, 5]
        bias, bright = operating_points(run, probe)
        self.assertEqual(bias.voltage, 2.0)
        self.assertEqual(bright.voltage, 0.0)
        probe.bias_voltage = None
        bias, _ = operating_points(run, probe)
        phases = {point.voltage: abs(math.cos(math.pi * drive(point.voltage) / X_PI + FIT["phase0_rad"]))
                  for point in probe.points if point.power is not None}
        self.assertEqual(bias.voltage, min(phases, key=phases.get))

    def test_saves_one_figure_per_victim(self):
        folder = write_run(self.root)
        paths = save_victim_figures(load_run(folder))
        self.assertEqual([path.name for path in paths], ["S1_1_victim.png", "S1_2_victim.png", "S2_1_victim.png"])
        self.assertTrue(all(path.parent == folder and path.stat().st_size > 0 for path in paths))

    def test_without_json_draws_measurements_only(self):
        folder = write_run(self.root, with_json=False)
        run = load_run(folder)
        self.assertIsNone(run.probes["S1.1", 1, 5].fit)
        self.assertEqual(operating_points(run, run.probes["S1.1", 1, 5])[0], None)
        self.assertEqual(main([str(folder), "--x", "power"]), 0)
        self.assertTrue((folder / "S1_1_victim.png").exists())

    def test_missing_sweeps_file_fails_cleanly(self):
        with self.assertRaises(SystemExit):
            main([str(self.root)])


if __name__ == "__main__":
    unittest.main()
