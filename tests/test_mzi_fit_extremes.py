"""Calibrated dark states use the deepest measured null."""

import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from benes.measurements.mzi_fit import MZIFit, SweepPoint, extract_calibration


class MZIFitExtremesTests(unittest.TestCase):
    def test_trusted_fit_does_not_hide_a_deeper_later_null(self):
        probe = SimpleNamespace(name="S2.2", input=4, output=2, bright_crossed=False)
        readings_dbm = ((0.0, -42), (2.1, -24), (3.1, -51), (3.8, -24))
        points = [SweepPoint(voltage, 10 ** (dbm / 10)) for voltage, dbm in readings_dbm]
        fit = MZIFit(offset=0, amplitude=0.01, x_pi=2.1 ** 2,
                     phase0=math.pi, rms=0, x_unit="V²")

        with patch("benes.measurements.mzi_fit.fit_mzi_response", return_value=fit):
            result = extract_calibration(probe, points, [point.voltage ** 2 for point in points])

        self.assertEqual(result.bar.voltage, 2.1)
        self.assertEqual(result.cross.voltage, 3.1)

    def test_raw_dark_state_uses_deeper_later_null(self):
        probe = SimpleNamespace(name="S2.2", input=4, output=2, bright_crossed=False)
        readings_dbm = ((0.0, -42), (2.1, -24), (2.9, -49.5), (3.1, -51))
        points = [SweepPoint(voltage, 10 ** (dbm / 10)) for voltage, dbm in readings_dbm]

        with patch("benes.measurements.mzi_fit.fit_mzi_response", return_value=None):
            result = extract_calibration(probe, points, [point.voltage ** 2 for point in points])

        self.assertEqual(result.cross.voltage, 3.1)


if __name__ == "__main__":
    unittest.main()
