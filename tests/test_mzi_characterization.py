import csv
import json
import math
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from benes.core.network import BenesNetwork
from benes.hardware.laser_bank import LaserBank, SimulatedLaserController
from benes.hardware.newport_laser import ModuleState
from benes.measurements.laser_calibration import CalibrationStopped, SimulatedChip
from benes.measurements.mzi_characterization import (LIGHT_POLL_S, MARK_DISTANT, MARK_FAILED, MARK_PATH, MZIFit,
                                                     OSALineDetector,
                                                     PowerMeterDetector, Reading, SweepPoint, apply_background,
                                                     crosstalk_marks, dbm_to_mw, evaluate_screen,
                                                     extract_calibration, fit_mzi_response, path_contamination,
                                                     phase_shift_from_power,
                                                     run_calibration, run_crosstalk, save_calibration,
                                                     save_crosstalk, scan_with_detector)
from benes.measurements.mzi_probing import CrosstalkProbe, Probe, plan_calibration, plan_crosstalk
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

    def test_calibration_background_cross_uses_zero_even_when_saved_cross_is_nonzero(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        probe = plan_calibration(self.network, ["S4.2"]).steps[0]
        controller.settings["calibration"]["S5.3"]["cross"] = 2.7
        controller.drive_voltage("S5.3", 2.7)
        background = apply_background(controller, probe, zero_cross=True)
        self.assertEqual(background["S5.3"], 0.0)
        self.assertTrue(all(controller.applied_voltages[name] == 0.0
                            for name in background))

    def test_power_meter_keeps_calibration_capture_around_each_probe(self):
        events = []
        lasers = SimpleNamespace(begin=lambda: events.append("lasers begin"),
                                 light_only=lambda source: events.append(("light", source)),
                                 finish=lambda: events.append("lasers finish"))
        detector = PowerMeterDetector(lambda: 1.0, lasers, fixed_output=0,
                                      begin_curve=lambda: events.append("capture begin"),
                                      end_curve=lambda: events.append("capture end"))
        probe = plan_calibration(self.network, outputs=(0,)).steps[0]
        detector.begin()
        detector.prepare(probe)
        detector.finish()
        self.assertEqual(events, ["lasers begin", ("light", probe.input), "capture begin",
                                  "capture end", "lasers finish"])

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

    def test_same_route_keeps_background_off_before_second_probe(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        selector.settings = {"settle_s": 3.0}
        plan = plan_calibration(self.network, inputs=detector.inputs, outputs=(4,))
        self.assertEqual([(probe.name, probe.input, probe.output) for probe in plan.steps[:2]],
                         [("S5.3", 0, 4), ("S4.2", 0, 4)])
        events = []
        background_at_first_point = []

        def on_point(name, index, *_):
            events.append(("point", name, index))
            if name == "S4.2" and index == 1:
                background_at_first_point.append(controller.applied_voltages["S5.3"])

        run_calibration(controller, detector, self.network, plan.steps[:2], VOLTAGES, 0.05,
                        on_step=lambda probe, *_: events.append(("step", probe.name)),
                        on_point=on_point,
                        sleep=lambda seconds: events.append(("sleep", seconds)))
        start = events.index(("step", "S4.2"))
        first_point = events.index(("point", "S4.2", 1))
        # Settle, one light check, settle again: no 3 s background wait.
        self.assertAlmostEqual(sum(event[1] for event in events[start + 1:first_point]
                                   if event[0] == "sleep"), 0.05 + LIGHT_POLL_S + 0.05)
        self.assertEqual(background_at_first_point, [0.0])
        self.assertEqual(selector.moves, [5, 5])

    def test_a_laser_that_lights_late_does_not_darken_the_first_point(self):
        # Bench run 20261028_235534: the Newport laser came on ~0.3 s after its 3 s
        # settle, so S5.1's 0 V point read 0 counts and became the "bar" voltage.
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(self.network, ["S2.1"], detector.inputs, detector.outputs)
        lit_power = detector.read_power
        dark_reads = [3]

        def late_laser():
            if dark_reads[0]:
                dark_reads[0] -= 1
                return 0.0
            return lit_power()

        begun = []
        late = PowerMeterDetector(late_laser, detector.lasers, detector.unit, detector.switch,
                                  begin_curve=lambda: begun.append(True))
        results = run_calibration(controller, late, self.network, plan.steps, VOLTAGES, 0,
                                  sleep=lambda _: None)
        self.assertEqual(dark_reads, [0])
        self.assertGreater(results["S2.1"].points[0].power, 0)
        # prepare() starts the curve, then the warm-up restarts it so its reads are not points.
        self.assertEqual(len(begun), 2)

    def test_complete_s5_3_to_s4_2_changes_jdsu_output(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(self.network, inputs=detector.inputs, outputs=detector.outputs)
        self.assertEqual([(probe.name, probe.output) for probe in plan.steps[:2]],
                         [("S5.3", 4), ("S4.2", 2)])
        run_calibration(controller, detector, self.network, plan.steps[:2], VOLTAGES, 0,
                        sleep=lambda _: None)
        self.assertEqual(selector.moves, [5, 3])

    def test_range_rescans_run_after_the_complete_initial_sweep(self):
        controller, selector, chip, detector, directory = bench(self.network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(self.network, ["S2.1"], detector.inputs, detector.outputs)
        seen = []

        def rescan(_controller, probe, voltages, _settle, points, *_args):
            seen.append((probe.name, len(points), controller.applied_voltages[probe.name]))
            return points

        detector.rescan_points = rescan
        run_calibration(controller, detector, self.network, plan.steps, VOLTAGES, 0,
                        sleep=lambda _: None)
        self.assertEqual(seen, [("S2.1", len(VOLTAGES), VOLTAGES[-1])])

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

        range_points = {name: [{"initial_raw_adc": 4.5, "final_raw_adc": 45.0,
                                "range_dbm": -30, "rescans": 1}
                               for _ in item.points]
                        for name, item in results.items()}
        context = {"arduino_settings": {"scale": 2.0, "offset": 1.0},
                   "hp_bnc": {"reference_range_dbm": -20, "point_ranges": range_points}}
        files = save_calibration(folder, context, results)
        self.assertIn("mzi_hp_bnc_points.csv", files)
        with (folder / "mzi_hp_bnc_points.csv").open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(line for line in file if not line.startswith("#")))
        self.assertEqual(float(rows[0]["initial_mean_adc"]), 4.5)
        self.assertEqual(float(rows[0]["initial_power_mW"]), 10.0)
        self.assertEqual(float(rows[0]["final_mean_adc"]), 45.0)
        self.assertEqual(float(rows[0]["adjusted_power_mW"]),
                         results[rows[0]["mzi"]].points[0].power)
        context["hp_bnc"]["bnc_reference"] = {"adc": 40.0, "dbm": -45.0, "range_dbm": -40}
        save_calibration(folder, context, results)
        with (folder / "mzi_hp_bnc_points.csv").open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(line for line in file if not line.startswith("#")))
        self.assertAlmostEqual(float(rows[0]["initial_power_mW"]),
                               10 ** (-45 / 10) * 4.5 / 40 * 100)
        context["hp_bnc"]["recovery_method"] = "adaptive_fixed_range"
        for records in range_points.values():
            for point in records:
                point.update(initial_range_dbm=-30, range_steps=1)
        save_calibration(folder, context, results)
        with (folder / "mzi_hp_bnc_points.csv").open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(line for line in file if not line.startswith("#")))
        self.assertEqual(rows[0]["initial_range_dbm"], "-30")
        self.assertEqual(rows[0]["range_steps"], "1")
        self.assertNotIn("gpib_dbm", rows[0])
        self.assertAlmostEqual(float(rows[0]["initial_power_mW"]),
                               10 ** (-45 / 10) * 4.5 / 40 * 10)


class ManualScanTests(unittest.TestCase):
    def test_manual_scan_lights_the_input_routes_the_jdsu_and_zeroes_the_switches(self):
        network = BenesNetwork()
        controller, selector, chip, detector, directory = bench(network)
        self.addCleanup(directory.cleanup)
        lit, closed, seen = [], [], []
        set_output = detector.lasers.controller.set_output
        detector.lasers.controller.set_output = lambda slot, kind, on: (lit.append(set(chip.lit)),
                                                                         set_output(slot, kind, on))[1]
        results = scan_with_detector(controller, lambda: (detector, lambda: closed.append(True)),
                                     ["S1.2", "S3.1"], 2, 4, VOLTAGES[:5], 0,
                                     on_point=lambda name, index, total, point: seen.append(name),
                                     sleep=lambda _: None)
        self.assertEqual(list(results), ["S1.2", "S3.1"])
        self.assertTrue(all(len(points) == 5 and points[0].power > 0 for points in results.values()))
        self.assertEqual(seen, ["S1.2"] * 5 + ["S3.1"] * 5)
        self.assertEqual(set(selector.moves), {5})
        # Only input 3 was lit while measuring; the lasers end as they started.
        self.assertIn({2}, lit)
        self.assertEqual(chip.lit, set(range(6)))
        self.assertEqual((controller.applied_voltages["S1.2"], controller.applied_voltages["S3.1"]), (0.0, 0.0))
        self.assertEqual(closed, [True])

    def test_manual_scan_reports_rescanned_points(self):
        network = BenesNetwork()
        controller, selector, chip, detector, directory = bench(network)
        self.addCleanup(directory.cleanup)
        detector.rescan_points = lambda controller, probe, voltages, settle, points, **kwargs: [
            SweepPoint(point.voltage, 1.0) for point in points]
        rescanned = []
        scan_with_detector(controller, lambda: (detector, lambda: None), ["S2.1"], 0, 0, VOLTAGES[:3], 0,
                           on_rescan=lambda name, points: rescanned.append((name, [p.power for p in points])),
                           sleep=lambda _: None)
        self.assertEqual(rescanned, [("S2.1", [1.0, 1.0, 1.0])])


class PathContaminationTests(unittest.TestCase):
    # Read off a measured run: S5.3 as victim, aggressors two stages, one stage and zero stages away.
    LEAKED = [(0, 0), (1, 0.05), (5, 0.42), (11, 0.95), (20, 1.29), (32, 0.8), (46, -0.27), (62, 1.01),
              (81, 1.09), (103, -0.08), (127, 1.29)]
    NOISY = [(0, 0), (5, -0.06), (20, -0.25), (32, -0.15), (46, 0.02), (62, -0.08), (81, -0.13), (101, 0.05),
             (125, -0.1)]
    THERMAL = [(0, 0), (2, -0.06), (5, -0.14), (17, -0.32), (26, -0.4), (40, -0.53), (55, -0.6), (73, -0.73),
               (91, -0.76), (113, -0.85)]

    @staticmethod
    def check(curve, x_pi=None):
        return path_contamination("S3.2", [x for x, _ in curve], [shift for _, shift in curve], x_pi)

    def test_a_curve_that_follows_the_aggressor_switching_is_flagged(self):
        self.assertIn("follows S3.2's own switching", self.check(self.LEAKED, 25))
        self.assertIn("turns back by 1.56 rad", self.check(self.LEAKED))

    def test_thermal_and_noisy_curves_keep_their_kappa(self):
        for curve in (self.NOISY, self.THERMAL):
            for x_pi in (None, 23, 28):
                self.assertIsNone(self.check(curve, x_pi))

    def test_a_small_leak_on_a_thermal_slope_is_found_by_its_period(self):
        xs = [5 * step for step in range(25)]
        shifts = [-0.004 * x + 0.3 * (math.cos(math.pi * x / 24) - 1) for x in xs]
        self.assertIn("±0.30 rad", path_contamination("S4.2", xs, shifts, 24))
        self.assertIsNone(path_contamination("S4.2", xs, [-0.004 * x for x in xs], 24))

    def test_a_folded_readout_is_flagged(self):
        self.assertIn("1.40 rad", path_contamination("S1.1", [0, 1, 2], [0, 0.8, 1.4]))
        self.assertIn("2 of 4 points", path_contamination("S1.1", [0, 1, 2, 3], [0, None, 0.2, None]))


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
        self.assertTrue(all(pair.contamination is None for pair in run.pairs.values()))
        folder = Path(directory.name) / "crosstalk"
        files = save_crosstalk(folder, {"comment": "", "unit": "mW"}, run, network, crosstalk)
        self.assertIn("crosstalk_matrix.csv", files)
        self.assertTrue(all((folder / file).is_file() for file in files))
        record = json.loads((folder / "crosstalk_matrix.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(record["kappa"]["S1.1"]["S1.2"])
        self.assertIsNone(record["kappa"]["S1.2"]["S1.1"])
        leaks = {item["aggressor"]: item["leak_order"] for item in record["pairs"]}
        self.assertEqual(set(leaks), {"S1.1", "S1.3", "S4.2"})
        self.assertTrue(all(value == "never" or value >= 2 for value in leaks.values()), leaks)
        # A curve flagged as path-contaminated keeps no κ and is marked in the matrix.
        run.pairs["S1.1", "S1.2"] = replace(run.pairs["S1.1", "S1.2"], kappa=None, contamination="leak")
        self.assertEqual(crosstalk_marks(run, crosstalk), {("S1.1", "S1.2"): MARK_PATH})

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

    def calibrated_bench(self):
        network = BenesNetwork()
        controller, selector, chip, detector, directory = bench(network)
        self.addCleanup(directory.cleanup)
        plan = plan_calibration(network, inputs=detector.inputs, outputs=detector.outputs)
        run_calibration(controller, detector, network, plan.steps, VOLTAGES, 0, sleep=lambda _: None)
        return network, controller, chip, detector

    def test_path_reference_allows_for_a_bright_bias_that_is_not_stationary(self):
        network, controller, chip, detector = self.calibrated_bench()
        plan = plan_crosstalk(network, ["S1.2"], list(network.switches), detector.inputs, detector.outputs,
                              known=network.switches)
        group = next(group for group in plan.groups if "S1.1" in group.aggressors)
        # S1.2's phase offset puts its brightest reachable point at 0 V, past the true maximum,
        # so the bright sweep also sees part of the victim's own phase shift.
        self.assertGreater(chip.phase["S1.2"], 0)
        run = run_crosstalk(controller, detector, network, [CrosstalkProbe(group.probe, ("S1.1",))], VOLTAGES, 0,
                            sleep=lambda _: None)
        pair = run.pairs["S1.1", "S1.2"]
        self.assertAlmostEqual(pair.kappa, chip.coupling["S1.1", "S1.2"], delta=0.002)
        self.assertAlmostEqual(pair.kappa_uncorrected, chip.coupling["S1.1", "S1.2"], delta=0.004)

    def test_a_victim_that_does_not_respond_is_skipped_and_the_run_continues(self):
        network, controller, chip, detector = self.calibrated_bench()
        plan = plan_crosstalk(network, ["S1.2", "S2.1"], ["S1.1"], detector.inputs, detector.outputs,
                              known=network.switches)
        read = detector.read
        detector.read = lambda probe: Reading(1.0) if probe.name == "S1.2" else read(probe)
        failed = []
        run = run_crosstalk(controller, detector, network, plan.groups, VOLTAGES, 0, sleep=lambda _: None,
                            on_failed=lambda group, reason: failed.append(group.probe.name))
        self.assertEqual(failed, ["S1.2"])
        self.assertIn("only 0.0 dB change", run.failed["S1.1", "S1.2"])
        self.assertEqual(set(run.pairs), {("S1.1", "S2.1")})
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))
        marks = crosstalk_marks(run, plan_crosstalk(network, ["S1.2", "S2.1", "S5.1"], ["S1.1"],
                                                    max_stage_distance=1))
        self.assertEqual(marks, {("S1.1", "S1.2"): MARK_FAILED, ("S1.1", "S5.1"): MARK_DISTANT})

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
    def test_rechecks_and_waits_for_laser_on_consecutive_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            chip = SimulatedChip(BenesNetwork(), lambda name: 0.0, {})
            controller = SimulatedLaserController(chip, {port: 7 - port for port in range(1, 7)})
            waits = []
            bank = LaserBank(controller, Path(directory) / "lasers.json", sleep=waits.append)
            bank.begin()
            commands = []
            original = controller.set_output

            def record(slot, kind, enabled):
                commands.append((slot, kind, enabled))
                return original(slot, kind, enabled)

            controller.set_output = record
            bank.light_only(0)
            bank.light_only(0)
            self.assertEqual(commands, [(6, "laser", True), (6, "laser", True)])
            self.assertEqual(waits, [1.0, 1.0])
            bank.light_only(1, minimum_settle_s=5.0)
            self.assertEqual(waits[-1], 5.0)
            bank.finish()

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
