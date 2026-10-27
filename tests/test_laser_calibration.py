import json
import tempfile
import unittest
from itertools import product
from pathlib import Path

from benes.measurements.laser_calibration import (SimulatedChip, check_alignment, check_calibrated_states,
                                                  create_calibration_folder, reference_states, save_state_checks,
                                                  switch_probes, wavelengths_from_assignment)
from benes.measurements.mzi_characterization import OSALineDetector, run_calibration
from benes.measurements.mzi_probing import plan_calibration
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

    def test_wavelength_assignment_uses_zero_based_inputs(self):
        self.assertEqual(wavelengths_from_assignment({"1": 1550.0, "2": None, "6": 1551.0}),
                         {0: 1550.0, 5: 1551.0})


def calibrate(network, controller, selector, chip, voltages=None):
    """Save every MZI's voltages with the OSA method, as the GUI does."""
    voltages = voltages or [round(0.1 * step, 2) for step in range(51)]
    detector = OSALineDetector(selector, chip.spectrum, WAVELENGTHS, 0.2)
    plan = plan_calibration(network, inputs=detector.inputs, outputs=detector.outputs)
    run_calibration(controller, detector, network, plan.steps, voltages, 0, sleep=lambda _: None)


class StateCheckTests(unittest.TestCase):
    def test_calibrated_all_cross_and_all_bar_pass(self):
        network, controller, selector, chip = rig()
        # With every heater on, thermal crosstalk alone costs a few dB of margin.
        chip.coupling = {}
        calibrate(network, controller, selector, chip)
        for crossed in (True, False):
            check = check_calibrated_states(controller, selector, chip.spectrum, network,
                                            dict.fromkeys(network.switches, crossed), WAVELENGTHS, 0.2, "all")
            self.assertEqual({item.verdict() for item in check.switches.values()}, {"ok"})
            self.assertEqual(check.routing_errors(), [])

    def test_state_check_flags_an_mzi_left_in_the_wrong_state(self):
        network, controller, selector, chip = rig()
        calibrate(network, controller, selector, chip)
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
        self.assertTrue(all(voltage == 0 for voltage in controller.applied_voltages.values()))

    def test_check_results_are_saved(self):
        network, controller, selector, chip = rig()
        check = check_alignment(controller, selector, chip.spectrum, network, WAVELENGTHS, 0.2)
        with tempfile.TemporaryDirectory() as directory:
            files = save_state_checks(Path(directory), {"comment": "six lasers"}, [check])
            self.assertEqual(files, ["state_checks.json", "output_readouts.csv", "readout_spectra.csv"])
            self.assertTrue(all((Path(directory) / file).is_file() for file in files))
            record = json.loads((Path(directory) / "state_checks.json").read_text(encoding="utf-8"))
            self.assertEqual(len(record["state_checks"]), 1)

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

    def test_unlit_inputs_carry_no_light(self):
        network = BenesNetwork()
        chip = SimulatedChip(network, lambda name: 0.0, WAVELENGTHS)
        chip.lit = {2}
        powers = chip.output_mw()
        self.assertTrue(all(powers[output][source] == 0 for output in range(6) for source in range(6) if source != 2))
        self.assertAlmostEqual(10 ** (chip.meter_dbm(network.mapping()[2] + 1) / 10),
                               max(powers[output][2] for output in range(6)) + 1e-7)

    def test_heaters_warm_their_neighbours(self):
        network = BenesNetwork()
        voltages = dict.fromkeys(network.switches, 0.0)
        chip = SimulatedChip(network, lambda name: voltages[name], WAVELENGTHS)
        before = chip.cross_fraction("S1.2")
        voltages["S1.1"] = 4.0
        self.assertNotAlmostEqual(chip.cross_fraction("S1.2"), before)
        self.assertEqual(chip.cross_fraction("S5.2"), SimulatedChip(network, lambda name: 0.0, {}).cross_fraction("S5.2"))


if __name__ == "__main__":
    unittest.main()
