import unittest
from types import SimpleNamespace

from benes.hardware.hp8152a import HPRangeUnavailable
from benes.measurements.arduino_hp_range import (BNC_CLIP_ADC, ArduinoHPRangeSweep, BNCReference,
                                                 measure_bnc_reference)
from benes.measurements.mzi_characterization import Reading, extract_calibration, sweep


class FakeHP:
    def __init__(self, range_dbm=-20):
        self.range_dbm = range_dbm
        self.commands = []

    def read_range_dbm(self, channel):
        return self.range_dbm

    def set_fixed_analog_range(self, channel, range_dbm):
        self.range_dbm = range_dbm
        self.commands.append((channel, range_dbm))


class FakeController:
    def __init__(self):
        self.voltage = None
        self.drives = []

    def drive_voltage(self, name, voltage):
        self.voltage = voltage
        self.drives.append((name, voltage))


class FakeArduino:
    full_scale = 127
    clip = 86.4  # where the real HP BNC output saturates

    def __init__(self, hp, controller, powers):
        self.hp = hp
        self.controller = controller
        self.powers = powers
        self.settings = {"scale": 1.0, "offset": 0.0}
        self.reads = []

    def read_current_power(self, *, ceiling_override=None, range_tracking_mode=None, ignore_jumps=False):
        self.reads.append((self.controller.voltage, self.hp.range_dbm, ceiling_override))
        optical = self.powers[self.controller.voltage]
        raw = min(self.clip, optical / 10 ** ((self.hp.range_dbm + 20) // 10))
        return SimpleNamespace(raw_mean=raw)


class ArduinoHPRangeTests(unittest.TestCase):
    def test_bnc_reference_converts_counts_and_range_to_absolute_dbm(self):
        reference = BNCReference(40.0, -45.0, -40, "GPIB0::23::INSTR", 1)
        self.assertAlmostEqual(reference.dbm_at(40.0, -40), -45.0)
        self.assertAlmostEqual(reference.dbm_at(4.0, -30), -45.0)
        self.assertAlmostEqual(reference.dbm_at(20.0, -50), -58.0102999566)
        self.assertIsNone(reference.dbm_at(0.0, -40))

    def make_scan(self, powers, hp=None, reference=None, settle=None):
        hp = hp or FakeHP()
        controller = FakeController()
        arduino = FakeArduino(hp, controller, powers)
        waits = [] if settle is None else settle
        reader = ArduinoHPRangeSweep(arduino, hp, 1, sleep=waits.append,
                                     bnc_reference=reference)
        reader.begin_curve()
        probe = SimpleNamespace(name="S2.1")
        voltages = list(powers)
        detector = SimpleNamespace(read=lambda _probe: Reading(reader.read()))
        points = sweep(controller, detector, probe, probe.name, voltages, 0,
                       sleep=lambda _: None)
        final = reader.finish_curve_points(probe, voltages, points)
        return hp, controller, arduino, reader, final, waits

    def test_range_changes_during_one_forward_sweep(self):
        hp, controller, arduino, reader, points, waits = self.make_scan(
            {0: 20, 1: 3, 2: 950, 3: 40})
        self.assertEqual(controller.drives, [("S2.1", v) for v in range(4)])
        self.assertEqual([(v, r) for v, r, _ in arduino.reads],
                         [(0, -20), (1, -20), (1, -30),
                          (2, -30), (2, -20), (2, -10), (2, 0),
                          (3, 0), (3, -10), (3, -20)])
        self.assertEqual([point.power for point in points], [20, 3, 950, 40])
        self.assertEqual([row["range_steps"] for row in reader.history["S2.1"]],
                         [0, 1, 3, 2])
        self.assertEqual(waits, [0.3] * len(hp.commands))
        self.assertTrue(all(ceiling > arduino.full_scale for _, _, ceiling in arduino.reads))

    def test_range_adjusted_arduino_values_are_absolute_mw_for_cross_bar_fit(self):
        reference = BNCReference(40.0, -40.0, -20, "GPIB0::23::INSTR", 1)
        hp, controller, arduino, reader, points, _ = self.make_scan(
            {0: 0.2, 1: 40, 2: 950}, reference=reference)
        self.assertEqual([row["measurement_source"] for row in reader.history["S2.1"]],
                         ["Arduino"] * 3)
        for point, relative_adc in zip(points, (0.2, 40, 950)):
            self.assertAlmostEqual(point.power, 10 ** -4 * relative_adc / 40)
        probe = SimpleNamespace(name="S2.1", input=0, output=0, bright_crossed=True)
        result = extract_calibration(probe, points, [0, 1, 2], min_extinction_db=3)
        self.assertEqual((result.cross.voltage, result.bar.voltage), (2, 0))

    def test_a_clipped_plateau_steps_the_range_up_instead_of_staying_flat(self):
        # Bench run S5.3 at -40 dBm: the BNC sat at 86.4 counts, under the old 88 limit.
        powers = {0: 300, 1: 600, 2: 40, 3: 2, 4: 600}
        hp, controller, arduino, reader, points, _ = self.make_scan(powers, hp=FakeHP(-20))
        self.assertEqual([point.power for point in points], [300, 600, 40, 2, 600])
        history = reader.history["S2.1"]
        self.assertTrue(all(row["final_raw_adc"] < BNC_CLIP_ADC for row in history))
        self.assertTrue(all(row["range_limit"] is None for row in history))

    def test_a_high_limit_at_the_clip_level_is_refused(self):
        hp = FakeHP()
        arduino = FakeArduino(hp, FakeController(), {})
        with self.assertRaisesRegex(ValueError, "clips"):
            ArduinoHPRangeSweep(arduino, hp, 1, low_adc=8, high_adc=88, sleep=lambda _: None)

    def test_floor_reading_at_the_lowest_available_range_is_kept_and_flagged(self):
        class LimitedHP(FakeHP):
            rejected = 0

            def set_fixed_analog_range(self, channel, range_dbm):
                if range_dbm == -60:
                    self.rejected += 1
                    raise HPRangeUnavailable(-60, self.range_dbm)
                super().set_fixed_analog_range(channel, range_dbm)

        hp = LimitedHP(-50)
        hp, controller, arduino, reader, points, _ = self.make_scan(
            {0: 0.004, 1: 0.1, 2: 0.02, 3: 0.0001, 4: 0.003}, hp=hp)
        self.assertAlmostEqual(points[0].power, 4.0)  # 4 counts at -50 dBm: kept, flagged
        self.assertIsNone(points[3].power)  # 0.1 counts is noise around zero
        self.assertIsNotNone(points[1].power)
        self.assertIsNotNone(points[2].power)
        self.assertAlmostEqual(points[4].power, 3.0)
        self.assertEqual(reader.history["S2.1"][0]["range_limit"], "low")
        self.assertIn("-60", reader.history["S2.1"][0]["range_error"])
        self.assertEqual(hp.rejected, 2)
        self.assertIn("previously stayed", reader.history["S2.1"][4]["range_error"])
        self.assertIn((1, -40), hp.commands)
        self.assertEqual(controller.drives, [("S2.1", v) for v in range(5)])

    def test_known_81521b_stops_at_minus_50_without_requesting_minus_60(self):
        class Head81521B(FakeHP):
            def read_head_id(self, channel):
                return "HEWLETT PACKARD,81521B,2827G01846,0"

            def set_fixed_analog_range(self, channel, range_dbm):
                if range_dbm < -50:
                    raise AssertionError("The 81521B has no -60 dBm fixed range")
                super().set_fixed_analog_range(channel, range_dbm)

        hp, _, _, reader, points, _ = self.make_scan(
            {0: 0.004, 1: 0.1, 2: 0.02}, hp=Head81521B(-50))
        self.assertEqual(reader.minimum_range_dbm, -50)
        self.assertEqual([point.power for point in points], [4.0, 100.0, 20.0])
        self.assertEqual(reader.history["S2.1"][0]["range_limit"], "low")
        self.assertIn("lowest fixed range", reader.history["S2.1"][0]["range_error"])
        self.assertNotIn((1, -60), hp.commands)

    def test_curve_without_two_usable_points_is_rejected(self):
        hp = FakeHP(-90)
        controller = FakeController()
        arduino = FakeArduino(hp, controller, {0: 1e-9, 1: 2e-9})
        reader = ArduinoHPRangeSweep(arduino, hp, 1, sleep=lambda _: None)
        probe = SimpleNamespace(name="S4.2")
        voltages = [0, 1]
        points = sweep(controller, SimpleNamespace(read=lambda _: Reading(reader.read())),
                       probe, probe.name, voltages, 0, sleep=lambda _: None)
        with self.assertRaisesRegex(ValueError, "only 0/2 Arduino points"):
            reader.finish_curve_points(probe, voltages, points)
        self.assertEqual([record["range_limit"] for record in reader.point_records], ["low", "low"])


class LaserOnLate:
    """HP and Arduino on one optical signal that only appears after a few HP reads."""

    full_scale = 127

    def __init__(self, dbm, dark_reads, gain_counts_at_range=25.0):
        self.dbm = dbm
        self.dark_reads = dark_reads
        self.gain = gain_counts_at_range  # counts when the power equals the range
        self.range_dbm = -30
        self.fixed = []
        self.settings = {"scale": 1.0, "offset": 0.0}

    def lit(self):
        return self.dark_reads <= 0

    # HP side
    def read_dbm_or_none(self):
        self.dark_reads -= 1
        return self.dbm if self.lit() else -72.0  # a dark input reads a steady low level

    def read_dbm(self):
        return self.dbm

    def read_range_dbm(self, channel):
        return self.range_dbm

    def set_fixed_analog_range(self, channel, range_dbm):
        self.range_dbm = range_dbm
        self.fixed.append(range_dbm)

    # Arduino side
    def read_current_power(self, **_):
        raw = self.gain * 10 ** ((self.dbm - self.range_dbm) / 10) if self.lit() else 0.0
        return SimpleNamespace(raw_mean=min(FakeArduino.clip, raw))


class BNCReferenceMeasurementTests(unittest.TestCase):
    def test_waits_for_the_laser_then_fixes_a_range_inside_the_window(self):
        bench = LaserOnLate(-44.0, dark_reads=4)
        waits = []
        reference = measure_bnc_reference(bench, bench, 1, "GPIB0::23::INSTR", sleep=waits.append)
        self.assertTrue(bench.lit())
        # -44 dBm on -30 gives ~1 count: one step down to -40 puts it at ~10 counts.
        self.assertEqual(bench.fixed, [-30, -40])
        self.assertEqual(reference.range_dbm, -40)
        self.assertAlmostEqual(reference.dbm_at(reference.adc, reference.range_dbm), -44.0)
        self.assertGreaterEqual(len(waits), 4)

    def test_a_clipped_bnc_steps_the_range_up(self):
        bench = LaserOnLate(-22.0, dark_reads=0)
        reference = measure_bnc_reference(bench, bench, 1, "GPIB0::23::INSTR", sleep=lambda _: None)
        self.assertEqual(reference.range_dbm, -20)
        self.assertLess(reference.adc, BNC_CLIP_ADC)

    def test_no_light_is_reported_instead_of_a_bad_reference(self):
        bench = LaserOnLate(-44.0, dark_reads=10 ** 6)
        with self.assertRaisesRegex(ValueError, "No steady light"):
            measure_bnc_reference(bench, bench, 1, "GPIB0::23::INSTR", light_timeout_s=2,
                                  sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
