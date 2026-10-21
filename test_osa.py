import unittest

from osa import wait_for_fresh_trace


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


if __name__ == "__main__":
    unittest.main()
