import json
import tempfile
import unittest
from pathlib import Path

from benes.hardware.osa_output_switch import OSAOutputSwitch


class FakeJDSU:
    """Mimics the SC command set: CLOSE n, CNB? settle bit and CLOSE? readback."""

    def __init__(self, settle_polls=0, reported=None):
        self.commands = []
        self.channel = 0
        self.settle_polls = settle_polls
        self.reported = reported
        self.closed = False

    def write(self, command):
        self.commands.append(command)
        self.channel = int(command.split()[1])

    def query(self, command):
        self.commands.append(command)
        if command == "CNB?":
            if self.settle_polls:
                self.settle_polls -= 1
                return "0"
            return "4"
        return str(self.reported if self.reported is not None else self.channel)

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, instrument):
        self.instrument = instrument
        self.opened = None
        self.closed = False

    def open_resource(self, resource):
        self.opened = resource
        return self.instrument

    def list_resources(self):
        return ("GPIB0::25::INSTR",)

    def close(self):
        self.closed = True


def physical_switch(directory, instrument, channel_map=None, clock=None):
    manager = FakeManager(instrument)
    selector = OSAOutputSwitch(Path(directory) / "selector.json", resource_manager_factory=lambda: manager,
                               sleep=lambda _: None, **({"clock": clock} if clock else {}))
    selector.configure("GPIB0::7::INSTR", False, 0, channel_map or {str(n): n for n in range(1, 7)})
    selector.connect()
    return selector, manager


class OSAOutputSwitchTests(unittest.TestCase):
    def test_defaults_map_each_output_to_matching_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            selector = OSAOutputSwitch(Path(directory) / "selector.json")
            self.assertEqual(selector.mapped_outputs(), [1, 2, 3, 4, 5, 6])
            self.assertEqual(selector.channel_for(5), 5)

    def test_dry_run_selection_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            selector = OSAOutputSwitch(path, sleep=lambda _: None)
            selector.connect()
            self.assertEqual(selector.select_output(4), 4)
            self.assertEqual(selector.routed_output, 4)
            self.assertEqual(OSAOutputSwitch(path).settings["selected_output"], 4)

    def test_selecting_output_closes_its_mapped_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            jdsu = FakeJDSU(settle_polls=2)
            selector, manager = physical_switch(directory, jdsu, {"1": 3, "2": "—", "6": 11})
            self.assertEqual(manager.opened, "GPIB0::7::INSTR")
            self.assertEqual(jdsu.write_termination, "\r\n")
            selector.select_output(6)
            self.assertEqual(jdsu.commands, ["CLOSE 11", "CNB?", "CNB?", "CNB?", "CLOSE?"])
            self.assertEqual(selector.mapped_outputs(), [1, 6])
            with self.assertRaisesRegex(ValueError, "not mapped"):
                selector.select_output(2)
            selector.disconnect()
            self.assertTrue(jdsu.closed and manager.closed)
            self.assertIsNone(selector.routed_output)

    def test_mapping_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            physical_switch(directory, FakeJDSU(), {"1": 2, "2": 1})
            stored = json.loads((Path(directory) / "selector.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["channel_map"], {"1": 2, "2": 1, "3": None, "4": None, "5": None, "6": None})
            self.assertEqual(OSAOutputSwitch(Path(directory) / "selector.json").channel_for(1), 2)

    def test_reports_wrong_channel_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            selector, _ = physical_switch(directory, FakeJDSU(reported=1))
            with self.assertRaisesRegex(RuntimeError, "reports 1"):
                selector.select_output(3)
            self.assertIsNone(selector.routed_output)

    def test_times_out_when_switch_never_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            ticks = iter(range(100))
            selector, _ = physical_switch(directory, FakeJDSU(settle_polls=1000), clock=lambda: next(ticks))
            with self.assertRaisesRegex(RuntimeError, "did not settle"):
                selector.select_output(1)

    def test_rejects_duplicate_and_out_of_range_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            selector = OSAOutputSwitch(Path(directory) / "selector.json")
            with self.assertRaisesRegex(ValueError, "both mapped"):
                selector.configure("GPIB0::25::INSTR", True, 0, {"1": 2, "4": 2})
            with self.assertRaisesRegex(ValueError, "from 1 to 16"):
                selector.configure("GPIB0::25::INSTR", True, 0, {"1": 17})

    def test_changing_dry_run_while_connected_reconnects(self):
        with tempfile.TemporaryDirectory() as directory:
            jdsu = FakeJDSU()
            manager = FakeManager(jdsu)
            selector = OSAOutputSwitch(Path(directory) / "selector.json", resource_manager_factory=lambda: manager,
                                       sleep=lambda _: None)
            selector.connect()
            selector.configure("GPIB0::25::INSTR", True, 0, {"1": 6, "6": 1})
            self.assertEqual(selector.channel_for(6), 1)
            selector.configure("GPIB0::7::INSTR", False, 0, {"1": 6, "6": 1})
            self.assertTrue(selector.connected)
            self.assertEqual(manager.opened, "GPIB0::7::INSTR")
            selector.select_output(1)
            self.assertEqual(jdsu.commands[0], "CLOSE 6")

if __name__ == "__main__":
    unittest.main()
