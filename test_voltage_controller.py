import tempfile
import unittest
from pathlib import Path

from voltage_controller import BenesVoltageController
from network import BenesNetwork


class FakeBoard:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.sent = []
        self.closed = False

    def send(self, pin, voltage):
        self.sent.append((pin, voltage))

    def close(self):
        self.closed = True


class VoltageControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.names = ("S1.1", "S1.2")

    def tearDown(self):
        self.temp.cleanup()

    def controller(self):
        return BenesVoltageController(self.names, self.path, board_factory=FakeBoard,
                                      unsafe_voltage_authorizer=lambda name, voltage: True)

    def test_unsafe_voltage_requires_approval_before_board_send(self):
        controller = BenesVoltageController(self.names, self.path, board_factory=FakeBoard)
        controller.connect()
        with self.assertRaisesRegex(ValueError, "outside the default"):
            controller.drive_voltage("S1.1", -1)
        self.assertNotIn((0, -1.0), controller.board.sent)
        with self.assertRaisesRegex(ValueError, "outside the default"):
            controller.set_voltage("S1.1", 7)
        self.assertEqual(controller.settings["voltage"]["S1.1"], 0.0)

    def test_mirrored_edge_is_one_unpaced_write_on_a_real_serial_board(self):
        class Serial:
            def __init__(self):
                self.writes = []

            def write(self, payload):
                self.writes.append(payload)
                return len(payload)

        controller = self.controller()
        controller.connect()
        controller.board.serial = Serial()
        target_time, mirror_time = controller.drive_with_mirror("S1.1", 2.5, 8)
        self.assertEqual(controller.board.serial.writes, [b"DC 0 2.50\nDC 8 2.50\n"])
        self.assertAlmostEqual(mirror_time - target_time, 10 * 10 / 115200)
        guarded = BenesVoltageController(self.names, self.path, board_factory=FakeBoard)
        guarded.connect()
        with self.assertRaisesRegex(ValueError, "outside the default"):
            guarded.drive_with_mirror("S1.1", 7, 8)
        self.assertNotIn((0, 7.0), guarded.board.sent)

    def test_new_default_mapping_reserves_pin_8(self):
        names = tuple(BenesNetwork().switches)
        controller = BenesVoltageController(names, self.path, board_factory=FakeBoard)
        self.assertNotIn(8, controller.settings["mapping"].values())

    def test_mapping_and_voltage_persist(self):
        controller = self.controller()
        controller.set_mapping({"S1.1": 7, "S1.2": None})
        self.assertEqual(controller.set_voltage("S1.1", 11), 10.0)

        restored = self.controller()
        self.assertEqual(restored.settings["mapping"], {"S1.1": 7, "S1.2": None})
        self.assertEqual(restored.settings["voltage"]["S1.1"], 10.0)

    def test_connect_sends_saved_values_and_calibration_follows_state(self):
        controller = self.controller()
        controller.set_voltage("S1.1", 1.25)
        controller.set_calibration("S1.1", straight=2, cross=3)
        controller.connect()
        self.assertEqual(controller.board.sent, [(0, 1.25), (1, 0.0)])
        controller.apply_calibrated_states({"S1.1": True, "S1.2": False})
        self.assertEqual(controller.board.sent[-1], (0, 3.0))
        self.assertEqual(controller.applied_voltages["S1.1"], 3.0)
        controller.drive_voltage("S1.1", -1.5)
        self.assertEqual(controller.applied_voltages["S1.1"], -1.5)
        self.assertEqual(controller.settings["voltage"]["S1.1"], 1.25)

    def test_duplicate_pins_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "only be assigned"):
            self.controller().set_mapping({"S1.1": 2, "S1.2": 2})

    def test_switch_toggle_sends_only_its_selected_calibration(self):
        controller = self.controller()
        controller.set_calibration("S1.1", straight=-2, cross=4)
        controller.connect()
        controller.board.sent.clear()
        self.assertEqual(controller.apply_calibrated_state("S1.1", True), 4.0)
        self.assertEqual(controller.apply_calibrated_state("S1.1", False), -2.0)
        self.assertEqual(controller.board.sent, [(0, 4.0), (0, -2.0)])
        self.assertIsNone(controller.apply_calibrated_state("S1.2", True))
        self.assertEqual(len(controller.board.sent), 2)

    def test_forget_one_or_all_calibrations_persists_without_sending_voltage(self):
        controller = self.controller()
        controller.set_calibration("S1.1", straight=1, cross=2)
        controller.set_calibration("S1.2", straight=3, cross=4)
        controller.connect()
        sent_before = list(controller.board.sent)

        controller.clear_calibration(("S1.1",), "straight")
        self.assertEqual(self.controller().settings["calibration"]["S1.1"],
                         {"straight": None, "cross": 2.0})
        controller.clear_calibration(("S1.1",))
        self.assertEqual(controller.board.sent, sent_before)
        self.assertIsNone(controller.apply_calibrated_state("S1.1", True))
        self.assertEqual(self.controller().settings["calibration"]["S1.2"],
                         {"straight": 3.0, "cross": 4.0})

        controller.clear_calibration(self.names)
        restored = self.controller()
        self.assertTrue(all(values == {"straight": None, "cross": None}
                            for values in restored.settings["calibration"].values()))
        self.assertEqual(controller.board.sent, sent_before)


if __name__ == "__main__":
    unittest.main()
