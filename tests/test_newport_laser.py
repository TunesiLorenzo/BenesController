import unittest

from benes.hardware.newport_laser import NewportLaserController


class Fake9016:
    def __init__(self):
        self.selected = {"LAS": "1,1", "TEC": "1,2"}
        self.outputs = {(kind, slot): (kind == "TEC" and slot == 1)
                        for kind in ("LAS", "TEC") for slot in range(1, 17)}
        self.commands = []
        self.fail = None

    def query(self, command):
        if command == "*IDN?":
            return "Newport 9016 v2.22m B222"
        if command == "EQUIP?":
            return ",".join(["9016"] + ["9605 LDD/TEC"] * 16)
        kind, query = command.split(":", 1)
        if query == "CHAN?":
            return self.selected[kind]
        if query == "OUTPUT?":
            return str(int(self.outputs[(kind, int(self.selected[kind].split(",")[0]))]))
        raise AssertionError(command)

    def write(self, command):
        self.commands.append(command)
        kind, suffix = command.split(":", 1)
        action, value = suffix.split(" ", 1)
        if action == "CHAN":
            self.selected[kind] = value
        elif action == "OUTPUT":
            slot = int(self.selected[kind].split(",")[0])
            if self.fail == (kind, slot):
                raise RuntimeError("simulated GPIB error")
            self.outputs[(kind, slot)] = bool(int(value))
        else:
            raise AssertionError(command)

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
        self.assertFalse(self.instrument.outputs[("TEC", 1)])
        self.assertFalse(self.instrument.outputs[("TEC", 16)])
        self.assertEqual(self.instrument.selected, {"LAS": "1,1", "TEC": "1,2"})


if __name__ == "__main__":
    unittest.main()
