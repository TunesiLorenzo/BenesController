import math
import unittest

from benes.hardware.osa import distinct_laser_wavelengths, find_laser_wavelengths, wait_for_fresh_trace


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FreshTraceTests(unittest.TestCase):
    def test_waits_for_the_requested_number_of_new_sweeps(self):
        clock = FakeClock()
        buffers = iter(["old", "old", "partly fresh", "partly fresh", "fresh", "later"])
        result = wait_for_fresh_trace(lambda: next(buffers), 2, clock=clock, sleep=clock.sleep)
        self.assertEqual(result, "fresh")

    def test_zero_sweeps_returns_current_buffer(self):
        self.assertEqual(wait_for_fresh_trace(lambda: "now", 0), "now")

    def test_baseline_marks_buffer_before_starting_a_sweep(self):
        clock = FakeClock()
        buffers = iter(["stale", "new sweep"])
        result = wait_for_fresh_trace(lambda: next(buffers), 1, baseline="stale", clock=clock, sleep=clock.sleep)
        self.assertEqual(result, "new sweep")

    def test_times_out_when_the_osa_is_not_sweeping(self):
        clock = FakeClock()
        with self.assertRaisesRegex(TimeoutError, "did not refresh"):
            wait_for_fresh_trace(lambda: "same", 1, max_wait_s=2, clock=clock, sleep=clock.sleep)


class LaserChannelTests(unittest.TestCase):
    def test_finds_six_distinct_peaks_and_orders_them_high_to_low(self):
        expected = [1548.1, 1549.0, 1550.2, 1551.4, 1552.7, 1553.8]
        wave = [1547.0 + step * 0.01 for step in range(800)]
        power = [-65 + sum(45 * math.exp(-((item - center) / 0.025) ** 2)
                           for center in expected) for item in wave]
        self.assertEqual(find_laser_wavelengths(wave, power), list(reversed(expected)))

    def test_rejects_a_trace_without_six_prominent_channels(self):
        wave = [1549 + step * 0.01 for step in range(200)]
        power = [-60 + 40 * math.exp(-((item - 1550) / 0.03) ** 2) for item in wave]
        with self.assertRaisesRegex(ValueError, "Found only 1 of 6"):
            find_laser_wavelengths(wave, power)

    def test_range_ignores_stronger_peaks_outside_1550_to_1560(self):
        expected = [1550.0, 1551.5, 1553.0, 1555.0, 1557.0, 1560.0]
        wave = [1548.0 + step * 0.01 for step in range(1401)]
        power = [-65 + sum(35 * math.exp(-((item - center) / 0.025) ** 2)
                           for center in expected)
                 + sum(50 * math.exp(-((item - center) / 0.025) ** 2)
                       for center in (1549.0, 1561.0)) for item in wave]
        self.assertEqual(find_laser_wavelengths(
            wave, power, wavelength_range_nm=(1550.0, 1560.0)), list(reversed(expected)))

    def test_combines_one_peak_from_each_output_in_wavelength_order(self):
        self.assertEqual(distinct_laser_wavelengths([1551.1, 1549.2, 1550.5, 1552.3, 1548.9, 1553.0]),
                         [1553.0, 1552.3, 1551.1, 1550.5, 1549.2, 1548.9])


if __name__ == "__main__":
    unittest.main()
