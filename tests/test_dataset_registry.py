import tempfile
import unittest
from pathlib import Path

from benes.measurements.dataset_registry import analyze_dataset, connection_pairs, create_dataset, save_measurement


class DatasetRegistryTests(unittest.TestCase):
    def test_single_pair_marks_only_that_routed_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_dataset(root, expected_permutations=[(1, 2, 3, 4, 5, 6)])
            save_measurement(root, source="OSA", permutation=(1, 2, 3, 4, 5, 6),
                             input_ports=[2], output_ports=[2], mode="routed_pairs", files=["trace.csv"])
            report = analyze_dataset(root)
            self.assertEqual(report["covered_paths"], 1)
            self.assertEqual(report["missing_paths"], 5)
            self.assertIn((2, 2), report["configurations"][0]["covered"])

    def test_many_input_many_output_routed_measurement_completes_permutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permutation = (2, 1, 4, 3, 6, 5)
            create_dataset(root, expected_permutations=[permutation])
            save_measurement(root, source="Power meter", permutation=permutation,
                             input_ports=range(1, 7), output_ports=range(1, 7),
                             mode="routed_pairs", files=["power.csv"])
            report = analyze_dataset(root)
            self.assertEqual(report["complete_configurations"], 1)
            self.assertEqual(report["missing_paths"], 0)

    def test_many_input_one_output_can_be_registered_as_cartesian_future_fixture(self):
        self.assertEqual(connection_pairs((1, 2, 3, 4, 5, 6), [1, 2, 3], [4], "cartesian"),
                         ((1, 4), (2, 4), (3, 4)))


if __name__ == "__main__":
    unittest.main()
