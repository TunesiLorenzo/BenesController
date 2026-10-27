import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from benes.core.network import BenesNetwork
from benes.hardware.laser_bank import LaserBank, SimulatedLaserController
from benes.hardware.newport_laser import ModuleState
from benes.measurements.laser_calibration import CalibrationStopped, SimulatedChip
from benes.measurements.mzi_characterization import (MZIFit, OSALineDetector, PowerMeterDetector, SweepPoint,
                                                     dbm_to_mw, evaluate_screen, extract_calibration,
                                                     fit_mzi_response, phase_shift_from_power,
                                                     run_calibration, run_crosstalk, save_calibration,
                                                     save_crosstalk)
from benes.measurements.mzi_probing import Probe, plan_calibration, plan_crosstalk
from tests.test_laser_calibration import WAVELENGTHS, FakeController, FakeSelector

VOLTAGES = [round(0.1 * step, 2) for step in range(51)]


def bench(network, via_jdsu=True, fixed_output=None):
    controller = FakeController(network)
    selector = FakeSelector()
    chip = SimulatedChip(network, lambda name: controller.applied_voltages[name], WAVELENGTHS)
    directory = tempfile.TemporaryDirectory()
    lasers = LaserBank(SimulatedLaserController(chip, {port: 7 - port for port in range(1, 7)}),
                       Path(directory.name) / "lasers.json", sleep=lambda _: None)

    def read():
        return dbm_to_mw(chip.meter_dbm(selector.routed_output if via_jdsu else fixed_output + 1))

    detector = PowerMeterDetector(read, lasers, switch=selector if via_jdsu else None,
                                  fixed_output=None if via_jdsu else fixed_output)
    return controller, selector, chip, detector, directory


class FitTests(unittest.TestCase):
    def test_fit_recovers_a_cos_squared_response(self):
        xs = [0.05 * step for step in range(200)]
        powers = [0.1 + 2 * (1 + math.cos(math.pi * x / 7.3 + 0.4)) / 2 for x in xs]
        fit = fit_mzi_response(xs, powers, "mW")
        self.assertAlmostEqual(fit.x_pi, 7.3, delta=0.05)
        self.assertAlmostEqual(fit.amplitude, 2, delta=0.02)
        self.assertLess(fit.rms, 0.01)

    def test_phase_shift_inverts_the_fit_on_the_bias_branch(self):
        fit = fit_mzi_response([0.05 * step for step in range(200)],
                               [1 + math.cos(math.pi * 0.05 * step / 5) for step in range(200)], "V²")
        bias_x = 2.5  # quadrature, π/2
        powers = [fit.model(bias_x + dx) for dx in (0, 0.1, 0.2)]
        shifts = phase_shift_from_power(fit, bias_x, powers)
        self.assertAlmostEqual(shifts[0], 0)
        self.assertAlmostEqual(shifts[2], 0.2 * fit.phase_per_x, places=3)

    def test_extremes_prefer_the_lowest_voltage_and_follow_the_bright_state(self):
        probe = Probe("S1.1", 0, 4, False, (), 1)
        points = [SweepPoint(0, 1.0), SweepPoint(3, 0.01), SweepPoint(4, 0.9995), SweepPoint(5, 0.0101)]
        result = extract_calibration(probe, points, [0, 9, 16, 25], min_extinction_db=3)
        # Bright when bar: the brightest point is bar, the darkest cross.
        self.assertEqual((result.bar.voltage, result.cross.voltage), (0, 3))
        self.assertAlmostEqual(result.extinction_db, 20, places=1)

    def test_a_flat_response_is_rejected(self):
        probe = Probe("S1.1", 0, 4, True, (), 1)
        with self.assertRaisesRegex(ValueError, "only 0.5 dB"):
            extract_calibration(probe, [SweepPoint(0, 1.0), SweepPoint(1, 0.9)], [0, 1])


class CalibrationRunTests(unittest.TestCase):
    def setUp(self):
        self.network = BenesNetwork()

    def check_calibrations(self, controller, chip, names):
        for name in names:
            saved = controller.settings["calibration"][name]
            # The simulated phase is π·V²/V_pi² + offset: bar at its first null, cross at its
            # maximum, which is 0 V unless the offset is negative.
            null = chip.v_pi[name] * math.sqrt(1 - chip.phase[name] / math.pi)
            peak = chip.v_pi[name] * math.sqrt(max(0.0, -chip.phase[name]) / math.pi)
            self.assertAlmostEqual(saved["straight"], null, delta=0.15, msg=name)
            self.assertAlmostEqual(saved["cross"], peak, delta=0.25, msg=name)

    def test_power_meter_through_the_jdsu_calibrates_every_mzi_one_laser_at_a_time(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        lit = []
        detector.lasers.controller.set_output = (lambda original: lambda slot, kind, on: (
            lit.append(set(chip.lit)), original(slot, kind, on))[1])(detector.lasers.controller.set_output)
        plan = plan_calibration(self.network, inputs=detector.inputs, outputs=detector.outputs)
        results = run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        self.assertEqual(set(results), set(self.network.switches))
        self.check_calibrations(controller, chip, self.network.switches)
        # Never two lasers at once while measuring, and the lasers end as they started.
        self.assertTrue(all(len(state) <= 1 for state in lit[6:-6]))
        self.assertEqual(chip.lit, set(range(6)))
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))

    def test_a_fixed_detector_calibrates_what_it_can_see(self):
        controller, selector, chip, detector, directory = bench(self.network, via_jdsu=False, fixed_output=2)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(self.network, inputs=detector.inputs, outputs=detector.outputs)
        run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        self.check_calibrations(controller, chip, [probe.name for probe in plan.steps])
        self.assertEqual(selector.moves, [])

    def test_osa_lines_calibrate_with_the_other_line_as_reference(self):
        controller, selector, chip, _, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        detector = OSALineDetector(selector, lambda output: chip.spectrum(output, 1601), WAVELENGTHS, 0.2)
        plan = plan_calibration(self.network, ["S2.1", "S4.2"], detector.inputs, detector.outputs)
        results = run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        self.assertTrue(all(point.reference is not None for point in results["S2.1"].points))
        self.check_calibrations(controller, chip, ["S2.1", "S4.2"])

    def test_stop_keeps_finished_mzis_and_resets_everything(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        stop = threading.Event()
        plan = plan_calibration(self.network, inputs=detector.inputs, outputs=detector.outputs)
        results = {}

        def on_point(name, index, total, point):
            if name == plan.steps[1].name and index == 3:
                stop.set()

        with self.assertRaises(CalibrationStopped):
            run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0, results=results,
                            on_point=on_point, stop_event=stop, sleep=lambda _: None)
        self.assertEqual(list(results), [plan.steps[0].name])
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))
        self.assertEqual(chip.lit, set(range(6)))

    def test_lasers_need_their_tec_on(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        modules = detector.lasers.controller.modules
        modules[5] = ModuleState("SIM LDD/TEC", False, False)
        plan = plan_calibration(self.network, inputs=detector.inputs, outputs=detector.outputs)
        with self.assertRaisesRegex(RuntimeError, "TEC of Newport slot 6 \\(input 1\\)"):
            run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)

    def test_results_are_saved(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(self.network, ["S1.1", "S3.2"], detector.inputs, detector.outputs)
        results = run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        folder = Path(directory.name) / "run"
        files = save_calibration(folder, {"comment": "one laser"}, results)
        self.assertEqual(files[:2], ["mzi_calibration.json", "mzi_sweeps.csv"])
        self.assertTrue(all((folder / file).is_file() for file in files))
        record = json.loads((folder / "mzi_calibration.json").read_text(encoding="utf-8"))
        self.assertEqual(record["mzi_calibrations"]["S1.1"]["input"], plan.probe_for("S1.1").input + 1)


class CrosstalkRunTests(unittest.TestCase):
    def test_kappa_matches_the_simulated_coupling(self):
        network = BenesNetwork()
        controller, selector, chip, detector, directory = bench(network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(network, inputs=detector.inputs, outputs=detector.outputs)
        run_calibration(controller, detector, network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        crosstalk = plan_crosstalk(network, ["S1.2"], ["S1.1", "S1.3", "S4.2"], detector.inputs, detector.outputs,
                                   known=network.switches)
        self.assertEqual(crosstalk.uncovered, {})
        run = run_crosstalk(controller, detector, network, crosstalk.groups, VOLTAGES, 0, sleep=lambda _: None)
        for aggressor in ("S1.1", "S1.3", "S4.2"):
            truth = chip.coupling.get((aggressor, "S1.2"), 0.0)
            self.assertAlmostEqual(run.pairs[aggressor, "S1.2"].kappa, truth, delta=0.004, msg=aggressor)
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))
        folder = Path(directory.name) / "crosstalk"
        files = save_crosstalk(folder, {"comment": "", "unit": "mW"}, run, network)
        self.assertIn("crosstalk_matrix.csv", files)
        self.assertTrue(all((folder / file).is_file() for file in files))
        record = json.loads((folder / "crosstalk_matrix.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(record["kappa"]["S1.1"]["S1.2"])
        self.assertIsNone(record["kappa"]["S1.2"]["S1.1"])

    def test_pretest_skips_aggressors_that_do_not_move_the_victim(self):
        network = BenesNetwork()
        controller, selector, chip, detector, directory = bench(network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(network, inputs=detector.inputs, outputs=detector.outputs)
        run_calibration(controller, detector, network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        # S4.2 is three stages from S1.2 and does not heat it.
        crosstalk = plan_crosstalk(network, ["S1.2"], ["S1.1", "S4.2"], detector.inputs, detector.outputs,
                                   known=network.switches)
        screened = []
        run = run_crosstalk(controller, detector, network, crosstalk.groups, VOLTAGES, 0, sleep=lambda _: None,
                            screen_min_phase_rad=0.02, on_screen=screened.append)
        self.assertEqual(len(screened), 2)
        self.assertTrue(run.screens["S1.1", "S1.2"].moved)
        self.assertEqual(run.static_pairs, {("S4.2", "S1.2")})
        self.assertEqual(set(run.pairs), {("S1.1", "S1.2")})
        self.assertAlmostEqual(run.pairs["S1.1", "S1.2"].kappa, chip.coupling["S1.1", "S1.2"], delta=0.004)
        # The path reference was taken with the victim at its bright extreme.
        self.assertEqual(len(run.screens["S4.2", "S1.2"].reference_low), 4)
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))
        folder = Path(directory.name) / "screened"
        files = save_crosstalk(folder, {"comment": "", "unit": "mW"}, run, network)
        self.assertIn("crosstalk_pretest.csv", files)
        record = json.loads((folder / "crosstalk_matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(record["static_pairs"], [{"aggressor": "S4.2", "victim": "S1.2"}])
        self.assertIn("n.s.", (folder / "crosstalk_matrix.csv").read_text(encoding="utf-8"))

    def test_pretest_weighs_the_step_against_noise_and_the_phase_floor(self):
        probe = Probe("S1.2", 0, 1, True, (), 2)
        fit = MZIFit(0.0, 2.0, 5.0, 0.0, 0.0, "V²")
        # Amplitude 2 gives 1 unit of power per radian at quadrature.
        quiet = evaluate_screen("S1.1", probe, fit, 0, 5, [1.0, 1.0, 1.0, 1.0], [1.05, 1.05, 1.05, 1.05])
        self.assertTrue(quiet.moved)
        self.assertAlmostEqual(quiet.phase_shift_rad, 0.05)
        tiny = evaluate_screen("S1.1", probe, fit, 0, 5, [1.0] * 4, [1.01] * 4)
        self.assertFalse(tiny.moved)
        noisy = evaluate_screen("S1.1", probe, fit, 0, 5, [0.9, 1.1, 0.9, 1.1], [0.95, 1.15, 0.95, 1.15])
        self.assertFalse(noisy.moved)
        self.assertGreater(noisy.resolution_rad, 0.05)
        # The same step on the path alone is not the victim's.
        path = evaluate_screen("S1.1", probe, fit, 0, 5, [1.0] * 4, [1.05] * 4, [2.0] * 4, [2.1] * 4)
        self.assertAlmostEqual(path.phase_shift_rad, 0.0)
        self.assertFalse(path.moved)
        self.assertTrue(evaluate_screen("S1.1", probe, fit, 0, 5, [1.0], [None]).moved)


class LaserBankTests(unittest.TestCase):
    def test_bays_are_reversed_by_default_and_the_mapping_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lasers.json"
            chip = SimulatedChip(BenesNetwork(), lambda name: 0.0, {})
            bank = LaserBank(SimulatedLaserController(chip, {}), path)
            self.assertEqual(bank.settings["slots"], {1: 6, 2: 5, 3: 4, 4: 3, 5: 2, 6: 1})
            bank.configure({1: 7, 2: 5, 3: 4, 4: 3, 5: 2, 6: 1}, 0.5)
            self.assertEqual(LaserBank(None, path).slot_for(0), 7)
            with self.assertRaisesRegex(ValueError, "six different"):
                bank.configure({port: 1 for port in range(1, 7)}, 0)


if __name__ == "__main__":
    unittest.main()
