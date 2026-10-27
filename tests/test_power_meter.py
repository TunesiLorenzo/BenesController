import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benes.hardware.power_meter import ArduinoPowerMeter


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

    def test_dual_stream_pairs_survive_split_frames_and_power_uses_a0(self):
        def frame(optical, electrical, sequence):
            return [0xA5, 0x5A, optical, electrical, sequence,
                    optical ^ electrical ^ sequence ^ 0xFF]

        first = frame(100, 40, 0)
        second = frame(120, 80, 1)
        chunks = iter(([99] + first + second[:3], second[3:], frame(80, 200, 2)))

        def burst(serial, **kwargs):
            return list(next(chunks)), {"start_time": 1.0, "end_time": 1.1}

        sampler = SimpleNamespace(open_arduino=lambda **kwargs: self.serial,
                                  close_arduino=lambda serial: None,
                                  burst_read_binary=burst)
        meter = ArduinoPowerMeter(self.path, sampler_loader=lambda: sampler)
        meter.configure("COM9", 230400, 0.05, 0.5, 0, "uW", "dual")
        meter.connect()
        pairs, _ = meter.read_timed_samples(0.05)
        self.assertEqual(pairs, [(100, 40)])
        pairs, _ = meter.read_timed_samples(0.05, reset_buffer=False)
        self.assertEqual(pairs, [(120, 80)])
        self.assertEqual(meter.read_current_power().value, 40.0)

    def test_optical_frames_align_mid_stream_and_resync_after_a_dropped_byte(self):
        pairs, tail, skipped = ArduinoPowerMeter._decode_optical_frames(
            bytes((0xC0, 10, 0xC1, 20, 21, 0xC0, 30)))
        # Leading status skipped; A0 "20" lost its status, so it is skipped too.
        self.assertEqual(pairs, [(10, True), (21, False)])
        self.assertEqual(tail, bytes((30,)))
        self.assertEqual(skipped, 2)
        self.assertEqual(ArduinoPowerMeter._decode_optical_frames(bytes((5, 0xC0, 0xC1))),
                         ([(5, False)], b"", 1))

    def test_raw_stream_sends_no_commands_and_measures_the_pair_period(self):
        clock = SimpleNamespace(now=10.0)

        def sleep(duration):
            clock.now += duration

        class RawSerial:
            def __init__(self):
                self.incoming = bytearray((99, 98, 97))  # stale bytes from before the run
                self.chunks = []

            @property
            def in_waiting(self):
                if not self.incoming and self.chunks:
                    self.incoming.extend(self.chunks.pop(0))
                return len(self.incoming)

            def reset_input_buffer(self):
                self.incoming.clear()

            def write(self, data):
                raise AssertionError("The tagged stream needs no commands")

            def read(self, count):
                result = bytes(self.incoming[:count])
                del self.incoming[:count]
                return result

        serial = RawSerial()
        meter = ArduinoPowerMeter(self.path)
        meter.serial = serial
        with patch("benes.hardware.power_meter.time.perf_counter", side_effect=lambda: clock.now),                 patch("benes.hardware.power_meter.time.sleep", side_effect=sleep):
            meter.start_stream()
            serial.chunks = [bytes((0xC1, 1, 0xC1))]
            first, timing1 = meter.read_timed_samples(0.001, reset_buffer=False)
            serial.chunks = [bytes((3, 0xC0, 5, 0xC0, 6))]
            second, timing2 = meter.read_timed_samples(0.001, reset_buffer=False)
            self.assertEqual(first + second, [(1, True), (3, False), (5, False)])
            self.assertEqual(timing1["skipped_bytes"], 1)
            self.assertEqual(timing2["first_index"], 1)
            elapsed = timing2["end_time"] - timing2["start_time"]
            self.assertAlmostEqual(timing2["sample_period_s"], elapsed / 3)
            # A restart drops stale bytes and any unfinished pair.
            meter.start_stream()
            serial.chunks = [bytes((10, 0xC0))]
            pairs, _ = meter.read_timed_samples(0.001, reset_buffer=False)
            self.assertEqual(pairs, [(10, False)])
            self.assertEqual(meter._raw_index, 1)
        self.assertEqual(meter.full_scale, 127)

    def test_raw_power_averages_a0_and_pauses_the_stream_even_on_error(self):
        meter = ArduinoPowerMeter(self.path)
        meter.serial = object()
        with patch.object(meter, "read_timed_samples", return_value=([(10, False), (30, False)], {})), \
                patch.object(meter, "stop_stream") as stop:
            reading = meter.read_current_power()
            self.assertEqual(reading.raw_mean, 20)
            self.assertEqual(reading.sample_count, 2)
            stop.assert_called_once()
        with patch.object(meter, "read_timed_samples", side_effect=OSError("unplugged")), \
                patch.object(meter, "stop_stream") as stop:
            with self.assertRaisesRegex(OSError, "unplugged"):
                meter.read_current_power()
            stop.assert_called_once()

    def test_raw_stream_reports_stalled_output(self):
        clock = SimpleNamespace(now=0.0)
        meter = ArduinoPowerMeter(self.path)
        meter.serial = SimpleNamespace(in_waiting=0)
        meter._raw_running = True
        meter.stream_start_time = 0.0

        def sleep(duration):
            clock.now += duration

        with patch("benes.hardware.power_meter.time.perf_counter", side_effect=lambda: clock.now), \
                patch("benes.hardware.power_meter.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, "stream stopped"):
                meter.read_timed_samples(0.6, reset_buffer=False)

    def test_raw_stream_requires_matching_firmware_baud(self):
        meter = ArduinoPowerMeter(self.path)
        with self.assertRaisesRegex(ValueError, "1000000 or 2000000 baud"):
            meter.configure("COM9", 230400, 0.1, 1, 0, "ADC counts", "raw")
        for baud in (1_000_000, 2_000_000):
            meter.configure("COM9", baud, 0.1, 1, 0, "ADC counts", "raw")
            self.assertEqual(meter.settings["baud"], baud)

    def test_ceiling_defaults_to_80_persists_and_is_capped_at_full_scale(self):
        meter = ArduinoPowerMeter(self.path)
        self.assertEqual(meter.ceiling, 80)
        meter.configure("COM9", 2_000_000, 0.1, 1, 0, "ADC counts", "raw", "75")
        self.assertEqual(ArduinoPowerMeter(self.path).ceiling, 75)
        meter.configure("COM9", 2_000_000, 0.1, 1, 0, "ADC counts", "raw", 300)
        self.assertEqual(meter.ceiling, 127)
        with self.assertRaisesRegex(ValueError, "ceiling"):
            meter.configure("COM9", 2_000_000, 0.1, 1, 0, "ADC counts", "raw", 0)

    def test_jump_guard_rejects_transition_and_saturation(self):
        meter = ArduinoPowerMeter(self.path)
        meter.serial = object()
        meter.settings["jump_guard_ms"] = 2
        frames = [(10, False), (70, True), (68, False), (65, False),
                  (64, False), (90, False), (63, False)]
        with patch.object(meter, "read_timed_samples", return_value=(frames, {"sample_period_s": 0.001})), \
                patch.object(meter, "stop_stream"):
            reading = meter.read_current_power()
        self.assertEqual(reading.raw_mean, (10 + 64 + 63) / 3)
        self.assertEqual(reading.sample_count, 3)
        self.assertEqual(reading.rejected_count, 4)
        self.assertEqual(reading.jump_count, 1)

    def test_all_flagged_samples_are_rejected(self):
        meter = ArduinoPowerMeter(self.path)
        meter.serial = object()
        with patch.object(meter, "read_timed_samples", return_value=([(10, True)], {})), \
                patch.object(meter, "stop_stream"):
            with self.assertRaisesRegex(RuntimeError, "no usable optical samples"):
                meter.read_current_power()


if __name__ == "__main__":
    unittest.main()
