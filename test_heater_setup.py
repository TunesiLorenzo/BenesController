import tempfile
import unittest
from pathlib import Path

from heater_setup import load_heater_csv, voltage_limit


CSV = """﻿# file,chip.gds,cell,TOP
# Rs heater [ohm/sq],3.03,Rs routing [ohm/sq],0.14,t heater [nm],200,t routing [nm],200,V [V],2.0
Device,R total [Ω],R heater [Ω],R routing [Ω],I [mA],P [mW],J typ [mA/µm²],J p99 [mA/µm²],J peak [mA/µm²],J routing pk,Note,+ pad box [µm],− pad box [µm]
mzi1,157.4,121.5,35.92,12.7,12.7,21.18,21.18,21.56,22.24,grid,"(-1, 2, 3, 4)","(5, 6, 7, 8)"
mzi2,162.8,121.5,41.32,12.3,12.3,20.48,20.48,20.84,20.0,grid,"(-1, 2, 3, 4)","(5, 6, 7, 8)"
"""


class HeaterSetupTests(unittest.TestCase):
    def write(self, text):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "mzi_results.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_resistances_and_worst_peak_density_per_volt(self):
        devices = load_heater_csv(self.write(CSV))
        self.assertEqual(list(devices), ["mzi1", "mzi2"])
        mzi1, mzi2 = devices["mzi1"], devices["mzi2"]
        self.assertEqual((mzi1.r_total_ohm, mzi1.r_heater_ohm), (157.4, 121.5))
        # Densities are tabulated at the header's 2 V; the higher routing peak is ignored.
        self.assertAlmostEqual(mzi1.j_peak_per_v, 21.56 / 2)
        self.assertAlmostEqual(mzi2.j_peak_per_v, 20.84 / 2)
        self.assertAlmostEqual(voltage_limit(mzi1.j_peak_per_v, 50), 50 / 10.78)

    def test_reference_voltage_falls_back_to_tabulated_current(self):
        text = CSV.replace(",V [V],2.0", "")
        devices = load_heater_csv(self.write(text))
        self.assertAlmostEqual(devices["mzi1"].j_peak_per_v, 21.56 / (12.7 * 157.4 / 1000))

    def test_rejects_a_file_without_the_device_table(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            load_heater_csv(self.write("Name,Value\nmzi1,3\n"))


if __name__ == "__main__":
    unittest.main()
