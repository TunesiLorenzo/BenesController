import unittest

from benes.hardware.hp8152a import HP8152A, HPRangeUnavailable, validate_stream_rate


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
    def test_fixed_analog_range_uses_gpib_without_triggering_power_read(self):
        class RangeInstrument(FakeInstrument):
            def write(self, command):
                super().write(command)
                if command.startswith("CH1;AR0;RNG 1,"):
                    self.settings["RNG? 1"] = command.split(",", 1)[1].removesuffix("DBM")

        instrument = RangeInstrument()
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument), sleep=lambda _: None)
        meter.connect()
        self.assertEqual(meter.read_range_dbm(1), -10)
        meter.set_fixed_analog_range(1, -20)
        self.assertEqual(meter.read_range_dbm(1), -20)
        self.assertIn("CH1;AR0;RNG 1,-20DBM", instrument.writes)
        self.assertNotIn("TRG", instrument.writes)
        meter.disconnect()
        self.assertTrue(instrument.closed)

    def test_unaccepted_range_reports_requested_and_actual_ranges(self):
        instrument = FakeInstrument()
        times = iter((0.0, 2.1))
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument),
                        clock=lambda: next(times), sleep=lambda _: None)
        meter.connect()
        with self.assertRaises(HPRangeUnavailable) as caught:
            meter.set_fixed_analog_range(1, -60)
        self.assertEqual(caught.exception.requested_range_dbm, -60)
        self.assertEqual(caught.exception.actual_range_dbm, -10)
        meter.disconnect()

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
        self.assertTrue(instrument.closed)
        self.assertFalse(manager.closed)

    def test_disconnect_does_not_close_shared_visa_manager(self):
        class SharedManager:
            def __init__(self):
                self.closed = False
                self.instruments = []

            def open_resource(self, _resource):
                if self.closed:
                    raise RuntimeError("Invalid session: resource might be closed")
                instrument = FakeInstrument()
                self.instruments.append(instrument)
                return instrument

            def close(self):
                self.closed = True

        manager = SharedManager()
        for _ in range(2):
            meter = HP8152A(resource_manager_factory=lambda: manager)
            meter.connect()
            meter.configure(range_dbm=-20)
            meter.disconnect()
        self.assertFalse(manager.closed)
        self.assertTrue(all(instrument.closed for instrument in manager.instruments))

    def test_manual_range_and_invalid_reading(self):
        instrument = FakeInstrument(readings=("-999.99",))
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument), sleep=lambda _: None)
        meter.connect()
        meter.configure(channel=2, wavelength_nm=1550, range_dbm=-30)
        self.assertIn("CH2;AR0;RNG 2,-30DBM", instrument.writes)
        with self.assertRaisesRegex(ValueError, "invalid or out-of-range"):
            meter.read_dbm()
        meter.disconnect()

    def test_remote_range_diagnostic_reports_refusal_and_restores_both_channels(self):
        class DiagnosticInstrument(FakeInstrument):
            def __init__(self):
                super().__init__()
                self.selected = 2
                self.autorange = {1: "1", 2: "0"}
                self.ranges = {1: -30, 2: -10}
                self.error = "0"

            def query(self, command):
                if command == "CH?":
                    return str(self.selected)
                if command == "AR?":
                    return self.autorange[self.selected]
                if command.startswith("RNG? "):
                    return str(self.ranges[int(command[-1])])
                if command == "IDN?1":
                    return "HP 81521B"
                if command == "ERR?":
                    return self.error
                return super().query(command)

            def write(self, command):
                super().write(command)
                for part in command.split(";"):
                    if part.startswith("CH"):
                        self.selected = int(part[2:])
                    elif part.startswith("AR"):
                        self.autorange[self.selected] = part[2:]
                    elif part.startswith("RNG "):
                        channel, value = part[4:].split(",")
                        if value == "-60DBM":
                            self.error = "220"
                        else:
                            self.ranges[int(channel)] = int(value.removesuffix("DBM"))

        instrument = DiagnosticInstrument()
        clock = iter((0.0, 0.1, 0.2, 2.3))
        meter = HP8152A(resource_manager_factory=lambda: FakeManager(instrument),
                        clock=lambda: next(clock), sleep=lambda _: None)
        meter.connect()
        result = meter.diagnose_fixed_range(1, -60)
        self.assertEqual((result["head_id"], result["actual_dbm"], result["error_after"]),
                         ("HP 81521B", -50, "220"))
        self.assertEqual(result["steps"], [
            {"requested_dbm": -40, "actual_dbm": -40, "error": "0"},
            {"requested_dbm": -50, "actual_dbm": -50, "error": "0"},
            {"requested_dbm": -60, "actual_dbm": -50, "error": "220"},
        ])
        self.assertEqual(instrument.selected, 2)
        self.assertEqual(instrument.autorange, {1: "1", 2: "0"})
        self.assertEqual(instrument.ranges, {1: -30, 2: -10})
        self.assertEqual(instrument.timeout, 10000)
        self.assertIn("RNG 1,-60DBM", instrument.writes)
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
