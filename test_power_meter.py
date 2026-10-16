import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from power_meter import ArduinoPowerMeter


class ArduinoPowerMeterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "meter.json"
        self.serial = object()
        self.closed = False
        self.sampler = SimpleNamespace(
            open_arduino=lambda **kwargs: self.serial,
            close_arduino=lambda serial: setattr(self, "closed", serial is self.serial),
            burst_read_binary=lambda serial, duration: [100, 120, 140],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_current_power_is_average_with_persisted_conversion(self):
        meter = ArduinoPowerMeter(self.path, sampler_loader=lambda: self.sampler)
        meter.configure("COM9", 230400, 0.05, 0.5, -10, "uW")
        meter.connect()
        reading = meter.read_current_power()
        self.assertEqual(reading.value, 50.0)
        self.assertEqual(reading.unit, "uW")
        self.assertEqual(reading.raw_mean, 120.0)
        self.assertEqual(reading.sample_count, 3)
        meter.disconnect()
        self.assertTrue(self.closed)
        self.assertEqual(ArduinoPowerMeter(self.path).settings["unit"], "uW")


if __name__ == "__main__":
    unittest.main()
