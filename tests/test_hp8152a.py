import unittest

from benes.hardware.hp8152a import HP8152A, validate_stream_rate


class FakeInstrument:
    def __init__(self, readings=("-32.50",)):
        self.writes = []
        self.readings = iter(readings)
        self.polls = iter((0, 4) * 20)
        self.closed = False
        self.settings = {"M?": "2", "CH?": "1", "U?": "0", "T?": "0", "AR?": "0",
                         "RNG? 1": "-10.00", "RNG? 2": "-10.00",
                         "WVL? 1": "0.1550E-05", "WVL? 2": "0.1550E-05"}

    def query(self, command):
        return self.settings[command]

    def write(self, command):
        self.writes.append(command)

    def read_stb(self):
        return next(self.polls)

    def read(self):
        return next(self.readings)

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, instrument):
        self.instrument = instrument
        self.resource = None
        self.closed = False

    def open_resource(self, resource):
        self.resource = resource
        return self.instrument

    def close(self):
        self.closed = True


class HP8152ATests(unittest.TestCase):
    def test_triggered_read_uses_gpib_status_and_restores_previous_settings(self):
        instrument = FakeInstrument()
        manager = FakeManager(instrument)
        meter = HP8152A(resource_manager_factory=lambda: manager, sleep=lambda _: None)
        meter.connect()
        meter.configure(channel=1, wavelength_nm=1550, range_dbm=None)
        self.assertEqual(meter.read_dbm(), -32.5)
        meter.disconnect()
        self.assertEqual(manager.resource, "GPIB0::23::INSTR")
        self.assertIn("M2;CH1;U0;T1;WVL 1,1550NM", instrument.writes)
        self.assertIn("AR1", instrument.writes)
        self.assertEqual(instrument.writes[-3:-1], ["CSB", "TRG"])
        self.assertIn("AR0", instrument.writes[-1])
        self.assertIn("T0", instrument.writes[-1])
        self.assertTrue(instrument.closed and manager.closed)

    def test_manual_range_and_invalid_reading(self):
        instrument = FakeInstrument(readings=("-999.99",))
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument), sleep=lambda _: None)
        meter.connect()
        meter.configure(channel=2, wavelength_nm=1550, range_dbm=-30)
        self.assertIn("AR0;RNG 2,-30DBM", instrument.writes)
        with self.assertRaisesRegex(ValueError, "invalid or out-of-range"):
            meter.read_dbm()
        meter.disconnect()

    def test_read_rate_counts_out_of_range_values_separately(self):
        instrument = FakeInstrument(readings=("-999.99", "-25.0", "-24.5"))
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument), sleep=lambda _: None)
        meter.connect()
        meter.configure()
        result = meter.sample_rate(3)
        self.assertEqual(result["samples"], 3)
        self.assertGreater(result["rate_hz"], 0)
        self.assertEqual(result["valid_values_dbm"], [-25.0, -24.5])
        meter.disconnect()

    def test_stream_rate_validation_and_nullable_out_of_range_read(self):
        self.assertEqual(validate_stream_rate("5.5"), 5.5)
        for invalid in ("0", "101", "nan", "inf", "oops"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_stream_rate(invalid)
        instrument = FakeInstrument(readings=("-999.99", "-24.0"))
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument), sleep=lambda _: None)
        meter.connect()
        meter.configure()
        self.assertIsNone(meter.read_dbm_or_none())
        self.assertEqual(meter.read_dbm_or_none(), -24.0)
        meter.disconnect()


if __name__ == "__main__":
    unittest.main()
