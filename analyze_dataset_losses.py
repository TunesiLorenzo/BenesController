"""Diagnose relative optical loss in a saved six-output WDM dataset.

Usage: python analyze_dataset_losses.py Results/281026_ProvaSofia4 --input 1

The report uses measured line peaks, summed over all six output ports in mW.
It cannot separate laser power, chip loss, grating coupling, and detector gain.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from benes.core.network import BenesNetwork


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def read_scans(root):
    """Return (time, state bits, permutation, total dBm per input) for valid scans."""
    network = BenesNetwork()
    names = tuple(network.switches)
    scans = []
    for path in (Path(root) / "Measurements").glob("*/wdm_scan.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("simulated_osa"):
                continue
            stamp = datetime.fromisoformat(record["started_at"])
            states = {name: record["switch_states"][name] == "cross" for name in names}
            bits = tuple(states[name] for name in names)
            permutation = tuple(int(output) for output in record["permutation"])
            if tuple(output + 1 for output in network.mapping(states)) != permutation:
                continue
            outputs = record["outputs"]
            if len(outputs) != 6 or {int(item["output"]) for item in outputs} != set(range(1, 7)):
                continue
            totals = []
            for input_port in range(1, 7):
                lines = [item["lines"][str(input_port)] for item in outputs]
                powers = [float(line["peak_dbm"]) for line in lines]
                total_mw = math.fsum(10 ** (power / 10) for power in powers)
                if total_mw <= 0 or not math.isfinite(total_mw):
                    raise ValueError("invalid line power")
                totals.append(10 * math.log10(total_mw))
            scans.append((stamp, bits, permutation, tuple(totals)))
        except (OSError, KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
            continue
    return sorted(scans, key=lambda item: item[0]), names


def input_output_report(scans, names):
    """Summarize relative output loss and repeated measurements of each path."""
    network = BenesNetwork()
    grouped = defaultdict(list)
    paths = defaultdict(lambda: defaultdict(list))
    for _stamp, bits, permutation, totals in scans:
        states = dict(zip(names, bits))
        for index, output in enumerate(permutation):
            key = index + 1, output
            grouped[key].append(totals[index])
            paths[key][network.trace(index, states).states].append(totals[index])
    best = {input_port: max(statistics.median(grouped[input_port, output]) for output in range(1, 7))
            for input_port in range(1, 7) if all(grouped[input_port, output] for output in range(1, 7))}
    rows = []
    for (input_port, output), values in sorted(grouped.items()):
        path_medians = [statistics.median(points) for points in paths[input_port, output].values()]
        rows.append({"input": input_port, "output": output, "n": len(values),
                     "median_dbm": statistics.median(values),
                     "relative_to_best_output_db": statistics.median(values) - best.get(input_port, math.nan),
                     "p10_p90_spread_db": percentile(values, .9) - percentile(values, .1),
                     "path_variants": len(path_medians),
                     "path_median_span_db": max(path_medians) - min(path_medians)})
    return rows


def matched_switch_report(scans, names, max_minutes=10):
    """Compare scans differing in exactly one switch, retaining the same I/O pair.

    Such a switch is off the measured input's path. Time proximity limits drift
    but does not make the comparison causal.
    """
    by_bits = {bits: (stamp, permutation, totals) for stamp, bits, permutation, totals in scans}
    differences = defaultdict(list)
    for stamp, bits, permutation, totals in scans:
        for index, name in enumerate(names):
            if bits[index]:
                continue
            alternate = bits[:index] + (True,) + bits[index + 1:]
            other = by_bits.get(alternate)
            if other is None or abs((other[0] - stamp).total_seconds()) > max_minutes * 60:
                continue
            for input_index, output in enumerate(permutation):
                if output == other[1][input_index]:
                    differences[input_index + 1, name].append(other[2][input_index] - totals[input_index])
    rows = []
    for (input_port, name), values in sorted(differences.items()):
        rows.append({"input": input_port, "switch": name, "n_pairs": len(values),
                     "median_cross_minus_bar_db": statistics.median(values),
                     "p10_p90_spread_db": percentile(values, .9) - percentile(values, .1)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Linked dataset folder containing Measurements/")
    parser.add_argument("--input", type=int, choices=range(1, 7), default=1,
                        help="Input channel to print (default: 1)")
    parser.add_argument("--max-minutes", type=float, default=10,
                        help="Maximum separation for matched switch comparisons (default: 10)")
    args = parser.parse_args()
    scans, names = read_scans(args.dataset)
    if not scans:
        parser.error("No complete, physical six-output WDM scans found")
    print(f"{len(scans)} scans; input {args.input}; all powers measured after the chip and output chain")
    print("Output  n    median dBm  vs best dB  p10-p90 dB  path variants  path median span dB")
    for row in input_output_report(scans, names):
        if row["input"] == args.input:
            print(f"{row['output']:>6} {row['n']:>4} {row['median_dbm']:>13.2f}"
                  f" {row['relative_to_best_output_db']:>11.2f} {row['p10_p90_spread_db']:>11.2f}"
                  f" {row['path_variants']:>14} {row['path_median_span_db']:>20.2f}")
    print("\nOff-path switch matches (only that switch differs; same input and output):")
    print("Switch  pairs  median cross-bar dB  p10-p90 dB")
    for row in matched_switch_report(scans, names, args.max_minutes):
        if row["input"] == args.input:
            print(f"{row['switch']:<7} {row['n_pairs']:>5} {row['median_cross_minus_bar_db']:>20.2f}"
                  f" {row['p10_p90_spread_db']:>11.2f}")


if __name__ == "__main__":
    main()
