import math
import unittest

from benes.hardware.osa import (apply_settings, distinct_laser_wavelengths, find_laser_wavelengths, read_settings,
                                single_acquisition, validate_settings, wait_for_fresh_trace, wait_until_stopped)


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


class FakeOSA:
    """Answers the OSASetup and acquisition queries like the MTS 8000 FO port."""

    def __init__(self, mode="1", status="STOPPED", timeout=10, accept=lambda command: True):
        self.timeout = timeout
        self.status = status
        self.buffer = "#14AAAA"
        self.sweeps = 0
        self.accept = accept
        self.settings = {"MODe": mode, "RESO": "FULL", "HSEns": "AUT", "AVG": "NO", "SEAcq": "SEM",
                         "MSAcq": "193.290", "MEAcq": "192.298"}
        self.sent = []

    def write(self, command):
        self.sent.append(command)
        if command == "KEY START":
            if self.settings["MODe"] == "1" or self.status == "STOPPED":
                self.status = "IN_PROGRESS"
            else:
                self.status = "STOPPED"
        elif command.startswith("OSASetup:") and self.accept(command):
            name, value = command[len("OSASetup:"):].split(" ", 1)
            if name in ("MSAcq", "MEAcq"):
                start = float(value) if name == "MSAcq" else float(self.settings["MSAcq"])
                end = float(value) if name == "MEAcq" else float(self.settings["MEAcq"])
                if start <= end:
                    return  # The module refuses a start at or below the end.
            self.settings[name] = {"AUTO": "AUT", "SEManual": "SEM", "MEDium": "MED"}.get(value, value)

    def query(self, command):
        self.sent.append(command)
        if command == "STAT:ACQ?":
            return self.status
        if command.startswith("STAT:WEAcq?"):
            if self.status == "IN_PROGRESS" and self.settings["MODe"] == "1":
                self.sweeps += 1
                self.buffer = f"#14{self.sweeps:04X}"
                self.status = "STOPPED"
            return self.status
        if command == ":CUR:BUFF?":
            return self.buffer
        if command == ":CUR:SIZE?":
            return "801"
        return self.settings[command[len("OSASetup:"):-1]]


class SingleAcquisitionTests(unittest.TestCase):
    def test_single_mode_starts_one_sweep_and_waits_for_its_end(self):
        session = FakeOSA()
        self.assertEqual(single_acquisition(session), "#140001")
        self.assertEqual(session.sent.count("KEY START"), 1)
        self.assertEqual(session.sent.count(":CUR:BUFF?"), 1)

    def test_a_sweep_already_running_is_finished_and_discarded(self):
        session = FakeOSA(status="IN_PROGRESS")
        self.assertEqual(single_acquisition(session), "#140002")

    def test_an_unchanged_buffer_is_still_a_new_sweep(self):
        # Below the floor every sweep returns the same buffer; the status shows it ran.
        session = FakeOSA()
        session.query = lambda command, query=session.query: "#14AAAA" if command == ":CUR:BUFF?" else query(command)
        self.assertEqual(single_acquisition(session), "#14AAAA")

    def test_a_start_the_osa_ignores_is_reported(self):
        session = FakeOSA()
        session.write = lambda command: None
        clock = FakeClock()
        with self.assertRaisesRegex(TimeoutError, "did not start"):
            single_acquisition(session, clock=clock, sleep=clock.sleep)

    def test_waits_stay_inside_the_socket_timeout(self):
        session = FakeOSA(timeout=5)
        single_acquisition(session, max_wait_s=30)
        self.assertIn("STAT:WEAcq? 4", session.sent)

    def test_a_sweep_that_never_ends_times_out(self):
        session = FakeOSA(mode="0", status="IN_PROGRESS")
        clock = FakeClock()
        session.query = lambda command, query=session.query: (clock.sleep(4), query(command))[1]
        with self.assertRaisesRegex(TimeoutError, "did not finish"):
            wait_until_stopped(session, 10, clock=clock)


class SettingsTests(unittest.TestCase):
    def test_reads_abbreviated_replies_as_keywords_and_nm(self):
        settings = read_settings(FakeOSA())
        self.assertEqual((settings.mode, settings.sensitivity, settings.range_preset), ("Single", "AUTO", "SEManual"))
        self.assertAlmostEqual(settings.start_nm, 1551.0, delta=0.01)
        self.assertAlmostEqual(settings.end_nm, 1559.0, delta=0.01)
        self.assertEqual(settings.points, 801)

    def test_applies_and_reads_back_a_range_moved_either_way(self):
        for start, end in ((1552.2, 1558.8), (1560.0, 1565.0), (1530.0, 1535.0)):
            session = FakeOSA(mode="0", status="IN_PROGRESS")
            settings = apply_settings(session, "Single", "R01nm", "NO", "MEDium", start, end)
            self.assertEqual((settings.mode, settings.resolution, settings.sensitivity, settings.averaging),
                             ("Single", "R01nm", "NO", "MEDium"))
            self.assertAlmostEqual(settings.start_nm, start, delta=0.01)
            self.assertAlmostEqual(settings.end_nm, end, delta=0.01)
            self.assertLess(session.sent.index("KEY START"), session.sent.index("OSASetup:MODe 1"))  # stopped first
            self.assertEqual(session.status, "STOPPED")

    def test_continuous_mode_restarts_the_sweep(self):
        session = FakeOSA(mode="0")
        apply_settings(session, "Continuous", "FULL", "AUTO", "NO", 1552.2, 1558.8)
        self.assertEqual(session.sent[-1], "KEY START")

    def test_a_setting_the_module_rejects_is_reported(self):
        session = FakeOSA(accept=lambda command: not command.startswith("OSASetup:RESO"))
        with self.assertRaisesRegex(ValueError, "resolution"):
            apply_settings(session, "Single", "R05nm", "AUTO", "NO", 1552.2, 1558.8)

    def test_rejects_an_inverted_or_tiny_range(self):
        with self.assertRaisesRegex(ValueError, "low to high"):
            validate_settings("Single", "FULL", "AUTO", "NO", 1558, 1552)
        with self.assertRaisesRegex(ValueError, "0.1 THz"):
            validate_settings("Single", "FULL", "AUTO", "NO", 1550.0, 1550.5)


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
