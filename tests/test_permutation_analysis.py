import json
import math
import tempfile
import unittest
from pathlib import Path

from benes.measurements.permutation_analysis import (
    analyze_permutation_set,
    channel_metrics,
    load_scan,
)


def write_scan(root, folder_name, permutation, target_dbm=0.0, leak_dbm=-10.0, started_at=None):
    folder = Path(root) / folder_name
    folder.mkdir(parents=True)
    matrix = [[leak_dbm for _input in range(6)] for _output in range(6)]
    for input_index, output in enumerate(permutation):
        matrix[output - 1][input_index] = target_dbm
    outputs = []
    for output in range(1, 7):
        routed_input = permutation.index(output) + 1
        outputs.append({
            "output": output,
            "routed_input": routed_input,
            "lines": {
                str(input_port): {"peak_dbm": matrix[output - 1][input_port - 1]}
                for input_port in range(1, 7)
            },
        })
    payload = {
        "started_at": started_at or folder_name,
        "permutation": permutation,
        "outputs": outputs,
        "switch_states": {"S1.1": "bar"},
    }
    (folder / "wdm_scan.json").write_text(json.dumps(payload), encoding="utf-8")
    return folder / "wdm_scan.json"


class PermutationAnalysisTests(unittest.TestCase):
    def test_same_wavelength_metrics_are_power_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = write_scan(temporary, "scan", [1, 2, 3, 4, 5, 6])
            metrics = channel_metrics(load_scan(path))

        self.assertEqual(len(metrics), 6)
        self.assertAlmostEqual(metrics[0].routing_isolation_db, 10.0)
        self.assertAlmostEqual(metrics[0].target_share, 1.0 / 1.5)
        self.assertAlmostEqual(metrics[0].total_crosstalk_db, 10 * math.log10(0.5))

    def test_analysis_numbers_permutations_lexicographically(self):
        with tempfile.TemporaryDirectory() as temporary:
            write_scan(temporary, "first", [1, 2, 3, 4, 5, 6])
            write_scan(temporary, "second", [1, 2, 3, 4, 6, 5])
            analysis = analyze_permutation_set(temporary)

        self.assertEqual([item.permutation_number for item in analysis.permutations], [1, 2])
        self.assertEqual(len(analysis.channels), 12)
        self.assertEqual(len(analysis.missing_permutation_numbers), 718)

    def test_newest_duplicate_scan_is_used_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            permutation = [1, 2, 3, 4, 5, 6]
            write_scan(temporary, "old", permutation, target_dbm=-5, started_at="2026-01-01T00:00:00")
            write_scan(temporary, "new", permutation, target_dbm=5, started_at="2026-01-02T00:00:00")
            analysis = analyze_permutation_set(temporary)

        self.assertEqual(analysis.duplicate_permutation_numbers, [1])
        self.assertEqual(analysis.scans[0].folder.name, "new")
        self.assertAlmostEqual(analysis.channels[0].target_power_dbm, 5.0)


if __name__ == "__main__":
    unittest.main()
