import tempfile
import unittest
from pathlib import Path

from benes.hardware.bench_power import power_down, restore, snapshot
from benes.hardware.newport_laser import ModuleState
from benes.hardware.voltage_controller import BenesVoltageController
from tests.test_voltage_controller import FakeBoard

NAMES = ("S1.1", "S1.2", "S1.3")


class FakeNewport:
    def __init__(self, lasers, tec=True):
        self.connected = True
        self.modules = [ModuleState("LD", laser, tec) for laser in lasers]

    def all_off(self):
        self.modules = [ModuleState(module.model, False, module.tec) for module in self.modules]
        return []

    def set_output(self, slot, kind, enabled):
        old = self.modules[slot - 1]
        self.modules[slot - 1] = ModuleState(old.model, enabled, old.tec)
        return enabled


class BenchPowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = BenesVoltageController(NAMES, Path(self.temp.name) / "config.json", board_factory=FakeBoard)
        self.controller.set_calibration("S1.1", 2.0, 0.5)
        self.controller.set_calibration("S1.2", 3.0, 1.0)
        self.controller.connect()
        # S1.1 at its bar, S1.2 at a hand-set voltage, S1.3 at 0 V.
        self.controller.apply_calibrated_state("S1.1", False)
        self.controller.drive_voltage("S1.2", 1.7)
        self.states = {"S1.1": False, "S1.2": True, "S1.3": True}

    def tearDown(self):
        self.temp.cleanup()

    def test_power_down_then_restore_brings_back_routing_and_lasers(self):
        newport = FakeNewport([True, False, True])
        before = snapshot(self.controller, self.states, {"S1.3"}, newport)
        self.assertEqual(before.lasers, (1, 3))
        self.assertEqual(before.calibrated, {"S1.1"})
        self.assertFalse(before.is_off)

        self.assertEqual(power_down(self.controller, NAMES, newport), [])
        self.assertEqual(set(self.controller.applied_voltages.values()), {0.0})
        self.assertFalse(any(module.laser for module in newport.modules))
        self.assertTrue(snapshot(self.controller, self.states, set(), newport).is_off)

        # A recalibration in between is picked up; the hand-set voltage is sent as it was.
        self.controller.set_calibration("S1.1", 2.2, 0.5)
        sent, errors = restore(self.controller, before, newport)
        self.assertEqual(errors, [])
        self.assertEqual(sent, {"S1.1": 2.2, "S1.2": 1.7, "S1.3": 0.0})
        self.assertEqual([module.laser for module in newport.modules], [True, False, True])

    def test_restore_reports_what_it_cannot_turn_back_on(self):
        newport = FakeNewport([True], tec=False)
        before = snapshot(self.controller, self.states, set(), newport)
        power_down(self.controller, NAMES, newport)
        self.controller.disconnect()
        sent, errors = restore(self.controller, before, newport)
        self.assertEqual(sent, {})
        self.assertIn("Connect the voltage board", errors[0])
        self.assertIn("TEC is off", errors[1])
        self.assertFalse(newport.modules[0].laser)

    def test_nothing_connected_is_already_off(self):
        self.controller.disconnect()
        state = snapshot(self.controller, self.states, set())
        self.assertTrue(state.is_off)
        self.assertEqual(power_down(self.controller, NAMES), [])


if __name__ == "__main__":
    unittest.main()
