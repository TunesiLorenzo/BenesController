import csv
import tempfile
import threading
import time
import unittest
from pathlib import Path

from benes.measurements.impulse_test import (plot_trace_samples, received_pair_rate, run_impulse_test,
                          validate_impulse_settings, write_impulse_csv)


class FakeBoard:
    def __init__(self):
        self.sent = []

    def send(self, pin, voltage):
        self.sent.append((pin, voltage))


class FakeController:
    connected = True

    def __init__(self):
        self.board = FakeBoard()
        self.settings = {"dry_run": False, "mapping": {"S1.1": 3}}

    def drive_with_mirror(self, name, voltage, mirror_pin):
        self.board.send(self.settings["mapping"][name], voltage)
        self.board.send(mirror_pin, voltage)
        now = time.perf_counter()
        return now, now

    def drive_voltage(self, name, voltage):
        self.board.send(self.settings["mapping"][name], voltage)


class FakeMeter:
    connected = True
    settings = {"stream_format": "dual", "scale": 0.5, "offset": 1.0, "unit": "uW"}

    def read_timed_samples(self, duration, reset_buffer=True):
        start = time.perf_counter()
        time.sleep(duration)
        return [(100, 60), (120, 80)], {"start_time": start, "end_time": time.perf_counter()}


class FakeRawMeter(FakeMeter):
    settings = dict(FakeMeter.settings, stream_format="raw")

    def start_stream(self):
        self.stream_start_time = time.perf_counter()
        self.index = 0
        self.stopped = False

    def stop_stream(self):
        self.stopped = True

    def read_timed_samples(self, duration, reset_buffer=True):
        if reset_buffer:
            raise AssertionError("A raw impulse must keep its stream running across chunks")
        time.sleep(duration)
        self.index += 3
        # The measured period settles from 200 us to 100 us as the run goes on.
        period = 0.0002 if self.index <= 3 else 0.0001
        return [(100, False), (110, True), (100, False)], {
            "start_time": self.stream_start_time, "end_time": time.perf_counter(),
            "first_index": self.index - 3, "sample_period_s": period, "skipped_bytes": 2,
        }


class ImpulseTests(unittest.TestCase):
    def test_raw_stream_uses_one_clock_across_chunks_and_exports_timing_limits(self):
        controller = FakeController()
        meter = FakeRawMeter()
        trace = run_impulse_test(controller, meter, "S1.1", "single", 0, 2,
                                 threading.Event(), pulse_s=0.05, measure_s=0.2, lead_in_s=0.01)
        self.assertTrue(meter.stopped)
        self.assertGreater(len(trace.samples), 3)
        for index, sample in enumerate(trace.samples):
            self.assertAlmostEqual(sample[0], index * 0.0001, places=7)
        self.assertEqual(trace.sample_period_s, 0.0001)
        self.assertEqual(trace.skipped_bytes, 2)
        self.assertEqual(controller.board.sent[-1:], [(3, 0)])
        self.assertNotIn((8, 0), controller.board.sent)
        self.assertTrue(trace.jump_indices)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            write_impulse_csv(path, trace)
            text = path.read_text(encoding="utf-8")
            rows = list(csv.reader(line for line in text.splitlines() if not line.startswith("#")))
        self.assertIn("frame period measured over the whole run", text)
        self.assertIn("2 byte(s) were skipped to resynchronize", text)
        self.assertIn("alignment to host command timestamps approximate", text)
        self.assertEqual(rows[1][2], "")
        self.assertEqual(rows[1][5], "")
        self.assertIn("1", [row[-1] for row in rows[1:]])

    def test_raw_capture_error_stops_stream_and_returns_both_board_pins_to_low(self):
        class FailingMeter(FakeRawMeter):
            def read_timed_samples(self, duration, reset_buffer=True):
                raise RuntimeError("serial disconnected")

        controller = FakeController()
        meter = FailingMeter()
        with self.assertRaisesRegex(RuntimeError, "serial disconnected"):
            run_impulse_test(controller, meter, "S1.1", "single", 0, 2,
                             threading.Event(), pulse_s=0.05, measure_s=0.2, lead_in_s=0)
        self.assertTrue(meter.stopped)
        self.assertEqual(controller.board.sent[-1:], [(3, 0)])

    def test_live_raw_times_stay_sorted_while_the_measured_period_settles(self):
        seen = []
        trace = run_impulse_test(FakeController(), FakeRawMeter(), "S1.1", "single", 0, 2,
                                 threading.Event(), pulse_s=0.01, measure_s=0.15, lead_in_s=0,
                                 on_progress=lambda trace: seen.append([s[0] for s in trace.samples]))
        live = seen[0]
        self.assertEqual(live, sorted(live))
        self.assertAlmostEqual(live[3] - live[2], 0.0001)
        self.assertAlmostEqual(trace.samples[3][0], 0.0003)

    def test_received_rate_uses_raw_pairs_and_recent_window(self):
        samples = [(index / 1000, 0, 0, 0) for index in range(2001)]
        self.assertAlmostEqual(received_pair_rate(samples), 1000)
        self.assertAlmostEqual(received_pair_rate(samples, window_s=1), 1000)
        self.assertEqual(received_pair_rate(samples[:1]), 0)

    def test_live_plot_keeps_recent_samples_and_short_peaks(self):
        samples = [(index / 10, 0, 255 if index == 95 else 10,
                    100 if index == 93 else 1) for index in range(100)]
        shown = plot_trace_samples(samples, window_s=2, max_bins=3)
        self.assertTrue(all(sample[0] >= 7.9 for sample in shown))
        self.assertIn(samples[93], shown)
        self.assertIn(samples[95], shown)
        self.assertEqual(shown[-1], samples[-1])

    def test_zoom_uses_raw_100us_detail_and_preserves_average_history(self):
        samples = [(index / 10000, 0, index % 256, float(index)) for index in range(20000)]
        shown = plot_trace_samples(samples, time_range=(0.5, 0.502))
        self.assertEqual(shown, samples[5000:5021])
        averaged = plot_trace_samples(samples, time_range=(0.5, 0.502), optical_average_s=0.001)
        self.assertEqual(averaged[0][0], 0.5)
        self.assertAlmostEqual(averaged[0][3], 4995.0)
        self.assertEqual(plot_trace_samples(samples, time_range=(3, 4)), [])

    def test_optical_display_average_does_not_erase_or_advance_a_step(self):
        samples = [(index / 1000, 0, 42, 0 if index < 500 else 100)
                   for index in range(1000)]
        shown = plot_trace_samples(samples, max_bins=1000, optical_average_s=0.02)
        self.assertEqual(samples[500][3], 100)
        self.assertLess(shown[500][3], 10)
        self.assertGreater(shown[520][3], 95)
        self.assertEqual(shown[500][2], 42)

    def test_single_pulse_mirrors_pin_8_and_exports_both_measured_channels(self):
        controller = FakeController()
        trace = run_impulse_test(controller, FakeMeter(), "S1.1", "single", 0, 2,
                                 threading.Event(), pulse_s=0.05, measure_s=0.2, lead_in_s=0.05)
        self.assertIn((3, 2), controller.board.sent)
        self.assertIn((8, 2), controller.board.sent)
        self.assertEqual(controller.board.sent[-2:], [(3, 0), (8, 0)])
        self.assertGreater(len(trace.samples), 0)
        self.assertEqual(trace.samples[0][1:3], (100, 60))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.csv"
            write_impulse_csv(path, trace)
            rows = list(csv.reader(line for line in path.read_text(encoding="utf-8").splitlines()
                                   if not line.startswith("#")))
        self.assertEqual(rows[0], ["time_s", "optical_adc_a0", "electrical_adc_a1",
                                   "optical_power", "target_command_v", "dummy_pin8_command_v",
                                   "jump_flag"])
        self.assertEqual(rows[1][1:4], ["100", "60", "51.0"])

    def test_impulse_rejects_unsafe_a1_voltage_and_occupied_pin_8(self):
        with self.assertRaisesRegex(ValueError, "0–5 V"):
            validate_impulse_settings("single", 0, 6, pulse_s=0.1, measure_s=1)
        controller = FakeController()
        controller.settings["mapping"]["S1.2"] = 8
        with self.assertRaisesRegex(ValueError, "pin 8 is mapped"):
            run_impulse_test(controller, FakeMeter(), "S1.1", "single", 0, 2,
                             threading.Event(), pulse_s=0.1, measure_s=1)

    def test_timing_has_no_limits_beyond_positive_and_ordered(self):
        validate_impulse_settings("single", 0, 2, pulse_s=1e-4, measure_s=1e-3, lead_in_s=60)
        validate_impulse_settings("square", 0, 2, period_s=1e-3, high_time_s=5e-4)
        with self.assertRaisesRegex(ValueError, "positive"):
            validate_impulse_settings("single", 0, 2, pulse_s=0, measure_s=0.5)
        with self.assertRaisesRegex(ValueError, "shorter than the period"):
            validate_impulse_settings("square", 0, 2, period_s=2e-3, high_time_s=2e-3)

    def test_square_wave_edges_follow_absolute_millisecond_schedule(self):
        controller = FakeController()
        stop = threading.Event()

        def progress(trace):
            if trace.samples and trace.samples[-1][0] >= 0.15:
                stop.set()

        trace = run_impulse_test(controller, FakeMeter(), "S1.1", "square", 0, 2,
                                 stop, period_s=0.01, high_time_s=0.005,
                                 lead_in_s=0.02, on_progress=progress)
        highs = [t for t, v in trace.voltage_events if v == 2]
        self.assertGreater(len(highs), 5)
        for index, edge in enumerate(highs[1:5], start=1):
            self.assertAlmostEqual(edge - highs[0], index * 0.01, delta=0.003)

    def test_square_wave_stop_returns_target_and_dummy_to_low(self):
        controller = FakeController()
        stop = threading.Event()

        def progress(trace):
            if trace.samples and trace.samples[-1][0] >= 0.2:
                stop.set()

        trace = run_impulse_test(controller, FakeMeter(), "S1.1", "square", 0, 2,
                                 stop, period_s=0.12, high_time_s=0.06,
                                 lead_in_s=0.02, on_progress=progress)
        self.assertTrue(trace.stopped)
        self.assertGreaterEqual(controller.board.sent.count((8, 2)), 2)
        self.assertEqual(controller.board.sent[-2:], [(3, 0), (8, 0)])


if __name__ == "__main__":
    unittest.main()
