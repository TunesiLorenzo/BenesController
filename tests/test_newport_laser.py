import unittest

from benes.hardware.newport_laser import NewportLaserController


class Fake9016:
    def __init__(self):
        self.selected = {"LAS": "1,1", "TEC": "1,2"}
        self.outputs = {(kind, slot): (kind == "TEC" and slot == 1)
                        for kind in ("LAS", "TEC") for slot in range(1, 17)}
        self.setpoints = {(kind, slot): (25.0 if kind == "TEC" else 10.0)
                          for kind in ("LAS", "TEC") for slot in range(1, 17)}
        self.modes = {(kind, slot): ("T" if kind == "TEC" else "ILBW")
                      for kind in ("LAS", "TEC") for slot in range(1, 17)}
        self.commands = []
        self.fail = None
        self.reject_setpoint = False
        self.reject_ldi = False
        self.error_text = "0"
        self.raw_commands = []

    def query(self, command):
        if command == "*IDN?":
            return "Newport 9016 v2.22m B222"
        if command == "EQUIP?":
            return ",".join(["9016"] + ["9605 LDD/TEC"] * 16)
        if command == "ERRSTR?":
            error = self.error_text
            self.error_text = "0"
            return error
        if command == "LAS:LIM:LDI?":
            return "500"
        kind, query = command.split(":", 1)
        if query == "CHAN?":
            return self.selected[kind]
        if query == "OUTPUT?":
            return str(int(self.outputs[(kind, int(self.selected[kind].split(",")[0]))]))
        if query in ("SET:LDI?", "SET:T?"):
            return str(self.setpoints[(kind, int(self.selected[kind].split(",")[0]))])
        if query == "MODE?":
            return self.modes[(kind, int(self.selected[kind].split(",")[0]))]
        raise AssertionError(command)

    def write(self, command):
        self.commands.append(command)
        kind, suffix = command.split(":", 1)
        if suffix.startswith("MODE:"):
            slot = int(self.selected[kind].split(",")[0])
            self.modes[(kind, slot)] = suffix.removeprefix("MODE:")
            return
        action, value = suffix.split(" ", 1)
        if action == "CHAN":
            slot, subchannel = value.split(",")
            self.selected[kind] = f"{slot},{2 if kind == 'TEC' else 1}"
        elif action == "OUTPUT":
            slot = int(self.selected[kind].split(",")[0])
            if self.fail == (kind, slot):
                raise RuntimeError("simulated GPIB error")
            self.outputs[(kind, slot)] = bool(int(value))
        elif action in ("LDI", "T"):
            slot = int(self.selected[kind].split(",")[0])
            if action == "LDI" and self.reject_ldi:
                self.error_text = '106,"Digit Expected"'
                return
            if "." not in value:
                self.error_text = '106,"Digit Expected"'
                return
            if not self.reject_setpoint:
                self.setpoints[(kind, slot)] = float(value)
            else:
                self.error_text = '201,"VALUE OUT OF RANGE"'
        else:
            raise AssertionError(command)

    def write_raw(self, command):
        self.raw_commands.append(command)
        saved = self.reject_ldi
        self.reject_ldi = False
        try:
            self.write(command.decode("ascii").rstrip("\n"))
        finally:
            self.reject_ldi = saved

    def close(self):
        pass


class FakeManager:
    def __init__(self, instrument):
        self.instrument = instrument

    def open_resource(self, resource):
        if resource != "GPIB0::19::INSTR":
            raise AssertionError(resource)
        return self.instrument

    def close(self):
        pass


class NewportTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Fake9016()
        manager = FakeManager(self.instrument)
        self.controller = NewportLaserController(resource_manager_factory=lambda: manager)
        self.controller.connect()

    def tearDown(self):
        self.controller.disconnect()

    def test_reads_laser_and_tec_from_separate_subchannels_and_restores_selection(self):
        states = self.controller.read_states()
        self.assertEqual(len(states), 16)
        self.assertEqual((states[0].laser, states[0].tec), (False, True))
        self.assertEqual((states[0].laser_setpoint, states[0].tec_setpoint), (10.0, 25.0))
        self.assertIn("LAS:CHAN 16,1", self.instrument.commands)
        self.assertIn("TEC:CHAN 16,2", self.instrument.commands)
        self.assertEqual(self.instrument.selected, {"LAS": "1,1", "TEC": "1,2"})

    def test_toggle_changes_only_requested_output(self):
        self.controller.read_states()
        self.controller.set_output(2, "laser", True)
        self.assertTrue(self.instrument.outputs[("LAS", 2)])
        self.assertFalse(self.instrument.outputs[("TEC", 2)])
        self.assertIn("LAS:OUTPUT 1", self.instrument.commands)
        self.assertEqual(self.instrument.selected, {"LAS": "1,1", "TEC": "1,2"})

    def test_all_off_continues_after_an_error_and_reports_it(self):
        self.instrument.outputs[("LAS", 3)] = True
        self.instrument.outputs[("TEC", 16)] = True
        self.instrument.fail = ("LAS", 3)
        errors = self.controller.all_off()
        self.assertEqual(len(errors), 1)
        self.assertIn("slot 3 laser", errors[0])
        self.assertTrue(self.instrument.outputs[("LAS", 3)])
        self.assertTrue(self.instrument.outputs[("TEC", 1)])
        self.assertTrue(self.instrument.outputs[("TEC", 16)])
        self.assertFalse(any(command.startswith("TEC:OUTPUT") for command in self.instrument.commands))
        self.assertEqual(self.instrument.selected, {"LAS": "1,1", "TEC": "1,2"})

    def test_setpoints_are_independent_and_read_back(self):
        self.controller.read_states()
        self.assertEqual(self.controller.set_setpoint(2, "laser", 12.5), 12.5)
        self.assertEqual(self.controller.set_setpoint(2, "tec", 24.75), 24.75)
        self.assertEqual(self.instrument.setpoints[("LAS", 2)], 12.5)
        self.assertEqual(self.instrument.setpoints[("TEC", 2)], 24.75)
        self.assertEqual(self.controller.modules[1].laser_setpoint, 12.5)
        self.assertEqual(self.controller.modules[1].tec_setpoint, 24.75)
        self.assertEqual(self.instrument.selected, {"LAS": "1,1", "TEC": "1,2"})

    def test_integer_setpoints_are_sent_with_decimal_digits(self):
        self.controller.set_setpoint(1, "laser", 23)
        self.controller.set_setpoint(2, "tec", 25)
        self.assertIn("LAS:LDI 23.000000", self.instrument.commands)
        self.assertIn("TEC:T 25.000000", self.instrument.commands)

    def test_rejects_invalid_setpoint_without_writing(self):
        before = list(self.instrument.commands)
        for value in ("bad", float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                self.controller.set_setpoint(1, "laser", value)
        self.assertEqual(self.instrument.commands, before)

    def test_setpoint_selects_mode_when_output_is_off(self):
        self.instrument.modes[("LAS", 2)] = "MDI"
        self.instrument.modes[("TEC", 2)] = "R"
        self.controller.set_setpoint(2, "laser", 12)
        self.controller.set_setpoint(2, "tec", 24)
        self.assertIn("LAS:MODE:ILBW", self.instrument.commands)
        self.assertIn("TEC:MODE:T", self.instrument.commands)
        self.assertEqual(self.instrument.modes[("LAS", 2)], "ILBW")
        self.assertEqual(self.instrument.modes[("TEC", 2)], "T")

    def test_setpoint_does_not_change_mode_of_live_output(self):
        self.instrument.modes[("TEC", 1)] = "R"
        with self.assertRaisesRegex(RuntimeError, "Turn it OFF"):
            self.controller.set_setpoint(1, "tec", 24)
        self.assertEqual(self.instrument.modes[("TEC", 1)], "R")
        self.assertFalse(any(command.startswith("TEC:MODE:") for command in self.instrument.commands))

    def test_unaccepted_setpoint_reports_controller_readback(self):
        self.instrument.reject_setpoint = True
        with self.assertRaisesRegex(RuntimeError, "controller reports 10 mA.*VALUE OUT OF RANGE.*Current limit: 500 mA"):
            self.controller.set_setpoint(2, "laser", 12)
        self.assertEqual(self.controller.modules[1].laser_setpoint, None)

    def test_retries_lf_only_after_parser_error(self):
        self.instrument.reject_ldi = True
        self.assertEqual(self.controller.set_setpoint(1, "laser", 23), 23)
        self.assertIn("LAS:LDI 23.000000", self.instrument.commands)
        self.assertIn(b"LAS:LDI 23.000000\n", self.instrument.raw_commands)


if __name__ == "__main__":
    unittest.main()
