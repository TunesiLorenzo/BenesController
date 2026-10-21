import tempfile
import unittest
from pathlib import Path

from osa_output_switch import OSAOutputSwitch


class FakeSerial:
    def __init__(self):
        self.commands = []
        self.closed = False

    def write(self, data):
        self.commands.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class OSAOutputSwitchTests(unittest.TestCase):
    def test_dry_run_selection_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            selector = OSAOutputSwitch(path, sleep=lambda _: None)
            selector.connect()
            self.assertEqual(selector.select_output(4), 4)
            self.assertEqual(OSAOutputSwitch(path).settings["selected_output"], 4)

    def test_template_sends_one_based_port_command(self):
        with tempfile.TemporaryDirectory() as directory:
            serial = FakeSerial()
            selector = OSAOutputSwitch(Path(directory) / "selector.json",
                                       serial_factory=lambda *args, **kwargs: serial, sleep=lambda _: None)
            selector.configure("COM7", 9600, False, "OUT {port}", 0)
            selector.connect()
            selector.select_output(6)
            self.assertEqual(serial.commands, [b"OUT 6\n"])
            selector.disconnect()
            self.assertTrue(serial.closed)

    def test_rejects_template_without_port_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            selector = OSAOutputSwitch(Path(directory) / "selector.json")
            with self.assertRaisesRegex(ValueError, "{port}"):
                selector.configure("COM7", 9600, False, "OUT", 0)


if __name__ == "__main__":
    unittest.main()
