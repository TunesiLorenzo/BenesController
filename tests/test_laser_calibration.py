import json
import tempfile
import threading
import unittest
from itertools import product
from pathlib import Path

from benes.measurements.laser_calibration import (CalibrationStopped, CompleteCalibration, ProbePoint, SimulatedChip, SwitchProbe,
                               calibrate_all, calibrate_switch, extract_calibration,
                               calibration_plan, check_alignment, check_calibrated_states, reference_states,
                               create_calibration_folder, save_calibration_results, switch_probes,
                               wavelengths_from_assignment)
from benes.core.network import BenesNetwork

WAVELENGTHS = {row: 1540.0 + 2 * row for row in range(6)}


class FakeController:
    connected = True

    def __init__(self, network):
        self.settings = {
            "mapping": {name: index for index, name in enumerate(network.switches)},
            "calibration": {name: {"straight": None, "cross": None} for name in network.switches},
        }
        self.applied_voltages = dict.fromkeys(network.switches, 1.0)

    def drive_voltage(self, name, voltage):
        self.applied_voltages[name] = float(voltage)
        return float(voltage)

    def set_calibration(self, name, straight, cross):
        self.settings["calibration"][name] = {"straight": straight, "cross": cross}

    def apply_calibrated_states(self, states):
        for name, crossed in states.items():
            self.drive_voltage(name, self.settings["calibration"][name]["cross" if crossed else "straight"])


class FakeSelector:
    def __init__(self):
        self.routed_output = None
        self.moves = []

    def select_output(self, output):
        self.moves.append(output)
        self.routed_output = output


def rig(wavelengths=WAVELENGTHS):
    network = BenesNetwork()
    controller = FakeController(network)
    chip = SimulatedChip(network, lambda name: controller.applied_voltages[name], wavelengths)
    return network, controller, FakeSelector(), chip


class ProbeTests(unittest.TestCase):
    def test_every_mzi_steers_two_outputs_in_any_configuration(self):
        network = BenesNetwork()
        for bits in ((True,) * 12, (False,) * 12, tuple(i % 3 == 0 for i in range(12))):
            states = dict(zip(network.switches, bits))
            for name in network.switches:
                first, second = switch_probes(network, states, name)
                # The MZI's two lines trade places between its two outputs.
                self.assertEqual((first.cross_input, first.straight_input),
                                 (second.straight_input, second.cross_input))

    def test_plan_covers_each_mzi_once_with_both_lines_and_few_switch_moves(self):
        network = BenesNetwork()
        plan = calibration_plan(network, reference_states(network), WAVELENGTHS)
        self.assertEqual(sorted(probe.name for probe in plan), sorted(network.switches))
        outputs = [probe.output for probe in plan]
        # Grouped: once the plan leaves an output it never returns to it.
        groups = [output for index, output in enumerate(outputs) if index == 0 or outputs[index - 1] != output]
        self.assertEqual(len(groups), len(set(groups)))
        self.assertLessEqual(len(groups), 4)

    def test_plan_prefers_outputs_seeing_a_lit_line_and_rejects_dark_mzis(self):
        network = BenesNetwork()
        states = reference_states(network)
        plan = calibration_plan(network, states, {0: 1550.0, 3: 1552.0}, ["S1.1"])
        self.assertTrue({plan[0].cross_input, plan[0].straight_input} & {0, 3})
        with self.assertRaisesRegex(ValueError, "No laser line reaches S1.1"):
            calibration_plan(network, states, {5: 1550.0}, ["S1.1"])

    def test_wavelength_assignment_uses_zero_based_inputs(self):
        self.assertEqual(wavelengths_from_assignment({"1": 1550.0, "2": None, "6": 1551.0}),
                         {0: 1550.0, 5: 1551.0})


class CalibrationTests(unittest.TestCase):
    voltages = [round(0.25 * step, 2) for step in range(21)]

    def test_complete_calibration_finds_each_mzi_and_verifies_both_states(self):
        network, controller, selector, chip = rig()
        checks = []
        # A coarse grid can land closer to the 3π null than to the first one, so use 0.1 V steps.
        voltages = [round(0.1 * step, 2) for step in range(51)]
        result = calibrate_all(controller, selector, lambda output: chip.spectrum(output, 1601), network,
                               voltages, 0, WAVELENGTHS, 0.2, on_check=checks.append, sleep=lambda _: None)
        self.assertEqual(set(result.switches), set(network.switches))
        self.assertEqual(result.alignment.routing_errors(), [])
        for calibration in result.switches.values():
            contrasts = [point.contrast_db for point in calibration.points]
            self.assertGreaterEqual(calibration.cross.contrast_db, max(contrasts) - 0.5)
            self.assertLessEqual(calibration.straight.contrast_db, min(contrasts) + 0.5)
            self.assertGreater(calibration.straight_margin_db, 10)
        self.assertEqual([check.label for check in checks],
                         ["Alignment (all 0 V)", "All cross (calibrated)", "All bar (calibrated)"])
        for check in checks[1:]:
            self.assertEqual({item.verdict() for item in check.switches.values()}, {"ok"})
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))

    def test_a_second_null_past_two_pi_does_not_replace_the_first(self):
        points = [ProbePoint(0, -10, -30), ProbePoint(3, -30, -10), ProbePoint(4, -11, -29),
                  ProbePoint(5, -30.2, -10)]
        result = extract_calibration(SwitchProbe("S1.1", 0, 1, 0), points)
        self.assertEqual((result.cross.voltage, result.straight.voltage), (0, 3))

    def test_single_mzi_sweep_with_one_lit_line_saves_and_resets(self):
        network, controller, selector, chip = rig({0: 1550.0})
        result = calibrate_switch(controller, selector, chip.spectrum, network, "S1.1", self.voltages, 0,
                                  {0: 1550.0}, 0.2, sleep=lambda _: None)
        self.assertEqual(result.cross.voltage, 0.0)
        self.assertIsNone(result.straight_margin_db)
        self.assertEqual(controller.settings["calibration"]["S1.1"],
                         {"straight": result.straight.voltage, "cross": 0.0})
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))

    def test_single_mzi_rejects_an_output_it_does_not_steer(self):
        network, controller, selector, chip = rig()
        steered = {probe.output for probe in switch_probes(network, reference_states(network), "S3.1")}
        other = min(set(range(6)) - steered)
        with self.assertRaisesRegex(ValueError, f"output {other + 1}"):
            calibrate_switch(controller, selector, chip.spectrum, network, "S3.1", self.voltages, 0,
                             WAVELENGTHS, 0.2, output=other)

    def test_state_check_flags_an_mzi_left_in_the_wrong_state(self):
        network, controller, selector, chip = rig()
        calibrate_all(controller, selector, chip.spectrum, network, self.voltages, 0, WAVELENGTHS, 0.2,
                      verify=False, sleep=lambda _: None)
        # Swap S2.1's saved voltages: commanded cross now drives it straight.
        saved = controller.settings["calibration"]["S2.1"]
        controller.set_calibration("S2.1", saved["cross"], saved["straight"])
        check = check_calibrated_states(controller, selector, chip.spectrum, network,
                                        reference_states(network), WAVELENGTHS, 0.2, "swapped")
        self.assertEqual(check.switches["S2.1"].verdict(), "inverted")
        self.assertTrue(check.routing_errors())

    def test_alignment_reports_swapped_output_fibers(self):
        network, controller, selector, chip = rig()
        swapped = {1: 2, 2: 1}
        check = check_alignment(controller, selector, lambda output: chip.spectrum(swapped.get(output, output)),
                                network, WAVELENGTHS, 0.2)
        mapping = network.mapping(reference_states(network))
        self.assertEqual(sorted(source for source, _, _ in check.routing_errors()),
                         sorted([mapping.index(0), mapping.index(1)]))

    def test_stop_aborts_and_returns_every_mzi_to_zero(self):
        network, controller, selector, chip = rig()
        stop = threading.Event()

        def on_point(probe, index, total, point):
            if index == 3:
                stop.set()

        partial = CompleteCalibration()
        with self.assertRaises(CalibrationStopped):
            calibrate_all(controller, selector, chip.spectrum, network, self.voltages, 0, WAVELENGTHS, 0.2,
                          on_point=on_point, stop_event=stop, result=partial, sleep=lambda _: None)
        self.assertIsNotNone(partial.alignment)
        self.assertEqual(partial.switches, {})
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))

    def test_results_are_saved(self):
        network, controller, selector, chip = rig()
        result = calibrate_all(controller, selector, chip.spectrum, network, [0, 2, 4], 0, WAVELENGTHS, 0.2,
                               sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as directory:
            files = save_calibration_results(Path(directory), {"comment": "six lasers"}, result.switches,
                                             [result.alignment, *result.checks])
            self.assertEqual(files[:2], ["mzi_calibration.json", "mzi_sweeps.csv"])
            self.assertEqual(len([file for file in files if file.endswith("_sweep.png")]), 12)
            self.assertTrue(all((Path(directory) / file).is_file() for file in files))
            self.assertIn("output_readouts.csv", files)
            self.assertIn("readout_spectra.csv", files)
            record = json.loads((Path(directory) / "mzi_calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(len(record["mzi_calibrations"]), 12)
            self.assertEqual(len(record["state_checks"]), 3)

    def test_calibration_folder_is_inside_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Dataset01"
            folder = create_calibration_folder(Path(directory), "complete", root)
            self.assertEqual(folder.parent, root / "calibration")
            self.assertTrue(folder.is_dir())


class SimulatedChipTests(unittest.TestCase):
    def test_zero_volt_chip_routes_the_all_cross_permutation(self):
        network = BenesNetwork()
        chip = SimulatedChip(network, lambda name: 0.0, WAVELENGTHS)
        powers = chip.output_mw()
        mapping = network.mapping(reference_states(network))
        for source, output in enumerate(mapping):
            self.assertEqual(max(powers, key=lambda candidate: powers[candidate][source]), output)

    def test_power_is_conserved_for_any_voltages(self):
        network = BenesNetwork()
        for voltages in product((0.0, 2.0, 3.5), repeat=2):
            chip = SimulatedChip(network, lambda name: voltages[int(name[1]) % 2], WAVELENGTHS)
            powers = chip.output_mw()
            for source in range(6):
                self.assertAlmostEqual(sum(powers[output][source] for output in range(6)),
                                       10 ** (chip.input_dbm[source] / 10))


if __name__ == "__main__":
    unittest.main()
