import threading
import unittest

from benes.measurements.measurement_control import CalibrationStopped, check_stop, select_output, wait


class FakeSelector:
    def __init__(self):
        self.outputs = []

    def select_output(self, output):
        self.outputs.append(output)


class MeasurementControlTests(unittest.TestCase):
    def test_select_output_translates_the_zero_based_chip_port(self):
        selector = FakeSelector()
        select_output(selector, 4)
        self.assertEqual(selector.outputs, [5])

    def test_wait_checks_for_stop_between_short_intervals(self):
        stop = threading.Event()
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            stop.set()

        with self.assertRaises(CalibrationStopped):
            wait(0.5, stop, sleep)
        self.assertEqual(waits, [0.1])

    def test_check_stop_is_a_noop_until_requested(self):
        check_stop(None)
        stop = threading.Event()
        check_stop(stop)
        stop.set()
        with self.assertRaises(CalibrationStopped):
            check_stop(stop)


if __name__ == "__main__":
    unittest.main()
