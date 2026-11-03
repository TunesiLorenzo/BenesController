"""Aggregate analysis for a complete six-port permutation measurement set.

The command-line entry point accepts both dataset layouts used by the GUI:
scan folders may be direct children of the dataset root or children of a
``Measurements`` directory.  Each scan is read from ``wdm_scan.json`` because
that file contains the full-precision 6 x 6 line-power matrix.

Run with::

    python -m benes.measurements.permutation_analysis Datasets/Dataset1

The default output directory is ``<dataset>/Analysis``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import permutations
from pathlib import Path
from typing import Iterable, Sequence

from benes.core.network import BenesNetwork


PERMUTATIONS = tuple(permutations(range(1, 7)))
PERMUTATION_NUMBER = {permutation: number for number, permutation in enumerate(PERMUTATIONS, start=1)}
SWITCH_STATES = {"bar", "cross"}


class PermutationAnalysisError(ValueError):
    """Raised when a scan cannot provide a valid six-port power matrix."""


@dataclass(frozen=True)
class LoadedScan:
    folder: Path
    started_at: str
    permutation: tuple[int, ...]
    peak_dbm: tuple[tuple[float, ...], ...]  # [output - 1][input - 1]
    switch_states: dict[str, str]

    @property
    def permutation_number(self) -> int:
        return PERMUTATION_NUMBER[self.permutation]


@dataclass(frozen=True)
class ChannelMetric:
    permutation_number: int
    permutation: tuple[int, ...]
    started_at: str
    input_port: int
    target_output: int
    target_power_dbm: float
    strongest_leak_power_dbm: float
    routing_isolation_db: float
    target_share: float
    target_share_db: float
    total_crosstalk_db: float
    output_isolation_db: float


@dataclass(frozen=True)
class PermutationMetric:
    permutation_number: int
    permutation: tuple[int, ...]
    started_at: str
    worst_input: int
    worst_routing_isolation_db: float
    median_routing_isolation_db: float
    minimum_target_share: float
    worst_total_crosstalk_db: float
    minimum_output_isolation_db: float
    median_target_power_dbm: float
    passes_threshold: bool


@dataclass
class PermutationAnalysis:
    dataset_root: Path
    threshold_db: float
    discovered_scans: int
    scans: list[LoadedScan]
    channels: list[ChannelMetric]
    permutations: list[PermutationMetric]
    missing_permutation_numbers: list[int]
    duplicate_permutation_numbers: list[int]
    invalid_scans: list[tuple[Path, str]]


def discover_scan_files(dataset_root: Path | str) -> list[Path]:
    """Return every WDM context file below ``dataset_root`` in stable order."""
    root = Path(dataset_root)
    if not root.is_dir():
        raise PermutationAnalysisError(f"Dataset folder does not exist: {root}")
    return sorted(root.rglob("wdm_scan.json"))


def _six_port_permutation(values: Iterable[object]) -> tuple[int, ...]:
    try:
        permutation = tuple(int(value) for value in values)
    except (TypeError, ValueError) as error:
        raise PermutationAnalysisError("invalid permutation values") from error
    if len(permutation) != 6 or set(permutation) != set(range(1, 7)):
        raise PermutationAnalysisError("permutation must contain outputs 1 through 6 exactly once")
    return permutation


def load_scan(context_path: Path | str) -> LoadedScan:
    """Load and validate one full-precision 6 x 6 WDM line-power matrix."""
    path = Path(context_path)
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
        permutation = _six_port_permutation(context["permutation"])
        outputs = context["outputs"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise PermutationAnalysisError(f"could not read scan context: {error}") from error

    if not isinstance(outputs, list) or len(outputs) != 6:
        raise PermutationAnalysisError("scan must contain six output records")
    matrix: list[list[float] | None] = [None] * 6
    for output_record in outputs:
        try:
            output = int(output_record["output"])
            routed_input = int(output_record["routed_input"])
            lines = output_record["lines"]
            row = [float(lines[str(input_port)]["peak_dbm"]) for input_port in range(1, 7)]
        except (KeyError, TypeError, ValueError) as error:
            raise PermutationAnalysisError(f"invalid output line matrix: {error}") from error
        if output not in range(1, 7) or matrix[output - 1] is not None:
            raise PermutationAnalysisError("output records must contain outputs 1 through 6 exactly once")
        expected_input = permutation.index(output) + 1
        if routed_input != expected_input:
            raise PermutationAnalysisError(
                f"output {output} declares input {routed_input}, expected input {expected_input}"
            )
        if any(not math.isfinite(value) for value in row):
            raise PermutationAnalysisError(f"output {output} contains a non-finite peak power")
        matrix[output - 1] = row

    raw_states = context.get("switch_states", {})
    switch_states = {
        str(name): str(state)
        for name, state in raw_states.items()
        if str(state) in SWITCH_STATES
    } if isinstance(raw_states, dict) else {}
    started_at = str(context.get("started_at") or path.parent.name)
    return LoadedScan(
        folder=path.parent,
        started_at=started_at,
        permutation=permutation,
        peak_dbm=tuple(tuple(row) for row in matrix if row is not None),
        switch_states=switch_states,
    )


def _dbm_to_mw(power_dbm: float) -> float:
    return 10.0 ** (power_dbm / 10.0)


def _ratio_db(numerator: float, denominator: float) -> float:
    if numerator <= 0:
        return -math.inf
    if denominator <= 0:
        return math.inf
    return 10.0 * math.log10(numerator / denominator)


def channel_metrics(scan: LoadedScan) -> list[ChannelMetric]:
    """Calculate six same-wavelength routing metrics for one permutation."""
    result = []
    for input_port, target_output in enumerate(scan.permutation, start=1):
        target_dbm = scan.peak_dbm[target_output - 1][input_port - 1]
        leak_dbm = [
            scan.peak_dbm[output - 1][input_port - 1]
            for output in range(1, 7)
            if output != target_output
        ]
        strongest_leak_dbm = max(leak_dbm)
        target_mw = _dbm_to_mw(target_dbm)
        leakage_mw = math.fsum(_dbm_to_mw(value) for value in leak_dbm)
        total_mw = target_mw + leakage_mw
        target_share = target_mw / total_mw

        target_row = scan.peak_dbm[target_output - 1]
        wrong_line_dbm = max(
            target_row[other_input - 1]
            for other_input in range(1, 7)
            if other_input != input_port
        )
        result.append(ChannelMetric(
            permutation_number=scan.permutation_number,
            permutation=scan.permutation,
            started_at=scan.started_at,
            input_port=input_port,
            target_output=target_output,
            target_power_dbm=target_dbm,
            strongest_leak_power_dbm=strongest_leak_dbm,
            routing_isolation_db=target_dbm - strongest_leak_dbm,
            target_share=target_share,
            target_share_db=_ratio_db(target_mw, total_mw),
            total_crosstalk_db=_ratio_db(leakage_mw, target_mw),
            output_isolation_db=target_dbm - wrong_line_dbm,
        ))
    return result


def permutation_metric(
    scan: LoadedScan,
    channels: Sequence[ChannelMetric],
    threshold_db: float,
) -> PermutationMetric:
    worst = min(channels, key=lambda item: item.routing_isolation_db)
    return PermutationMetric(
        permutation_number=scan.permutation_number,
        permutation=scan.permutation,
        started_at=scan.started_at,
        worst_input=worst.input_port,
        worst_routing_isolation_db=worst.routing_isolation_db,
        median_routing_isolation_db=statistics.median(item.routing_isolation_db for item in channels),
        minimum_target_share=min(item.target_share for item in channels),
        worst_total_crosstalk_db=max(item.total_crosstalk_db for item in channels),
        minimum_output_isolation_db=min(item.output_isolation_db for item in channels),
        median_target_power_dbm=statistics.median(item.target_power_dbm for item in channels),
        passes_threshold=all(item.routing_isolation_db >= threshold_db for item in channels),
    )


def analyze_permutation_set(
    dataset_root: Path | str,
    threshold_db: float = 10.0,
) -> PermutationAnalysis:
    """Load a dataset and select the newest valid scan for each permutation."""
    root = Path(dataset_root).resolve()
    context_paths = discover_scan_files(root)
    valid: list[LoadedScan] = []
    invalid: list[tuple[Path, str]] = []
    for path in context_paths:
        try:
            valid.append(load_scan(path))
        except PermutationAnalysisError as error:
            invalid.append((path, str(error)))

    grouped: dict[int, list[LoadedScan]] = defaultdict(list)
    for scan in valid:
        grouped[scan.permutation_number].append(scan)
    duplicates = sorted(number for number, scans in grouped.items() if len(scans) > 1)
    scans = [
        max(grouped[number], key=lambda item: (item.started_at, str(item.folder)))
        for number in sorted(grouped)
    ]
    missing = sorted(set(range(1, 721)) - set(grouped))
    channels: list[ChannelMetric] = []
    permutation_rows: list[PermutationMetric] = []
    for scan in scans:
        scan_channels = channel_metrics(scan)
        channels.extend(scan_channels)
        permutation_rows.append(permutation_metric(scan, scan_channels, threshold_db))

    return PermutationAnalysis(
        dataset_root=root,
        threshold_db=float(threshold_db),
        discovered_scans=len(context_paths),
        scans=scans,
        channels=channels,
        permutations=permutation_rows,
        missing_permutation_numbers=missing,
        duplicate_permutation_numbers=duplicates,
        invalid_scans=invalid,
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Linear percentile with no NumPy dependency in the analysis layer."""
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if math.isfinite(value) else str(value)


def _write_channel_csv(path: Path, analysis: PermutationAnalysis) -> None:
    fields = [
        "permutation_number", "permutation", "started_at", "input", "target_output",
        "target_power_dbm", "strongest_leak_power_dbm", "routing_isolation_db",
        "target_share_percent", "target_share_db", "total_crosstalk_db", "output_isolation_db",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for item in analysis.channels:
            writer.writerow({
                "permutation_number": item.permutation_number,
                "permutation": " ".join(map(str, item.permutation)),
                "started_at": item.started_at,
                "input": item.input_port,
                "target_output": item.target_output,
                "target_power_dbm": _fmt(item.target_power_dbm, 4),
                "strongest_leak_power_dbm": _fmt(item.strongest_leak_power_dbm, 4),
                "routing_isolation_db": _fmt(item.routing_isolation_db, 4),
                "target_share_percent": _fmt(100.0 * item.target_share, 4),
                "target_share_db": _fmt(item.target_share_db, 4),
                "total_crosstalk_db": _fmt(item.total_crosstalk_db, 4),
                "output_isolation_db": _fmt(item.output_isolation_db, 4),
            })


def _write_permutation_csv(path: Path, analysis: PermutationAnalysis) -> None:
    fields = [
        "permutation_number", "permutation", "started_at", "worst_input",
        "worst_routing_isolation_db", "median_routing_isolation_db",
        "minimum_target_share_percent", "worst_total_crosstalk_db",
        "minimum_output_isolation_db", "median_target_power_dbm",
        f"passes_{analysis.threshold_db:g}_db_routing_isolation",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for item in analysis.permutations:
            writer.writerow(dict(zip(fields, (
                item.permutation_number,
                " ".join(map(str, item.permutation)),
                item.started_at,
                item.worst_input,
                _fmt(item.worst_routing_isolation_db, 4),
                _fmt(item.median_routing_isolation_db, 4),
                _fmt(100.0 * item.minimum_target_share, 4),
                _fmt(item.worst_total_crosstalk_db, 4),
                _fmt(item.minimum_output_isolation_db, 4),
                _fmt(item.median_target_power_dbm, 4),
                "yes" if item.passes_threshold else "no",
            ))))


def _port_pair_rows(analysis: PermutationAnalysis) -> list[dict[str, float | int]]:
    grouped: dict[tuple[int, int], list[ChannelMetric]] = defaultdict(list)
    input_power: dict[int, list[float]] = defaultdict(list)
    for item in analysis.channels:
        grouped[(item.input_port, item.target_output)].append(item)
        input_power[item.input_port].append(item.target_power_dbm)
    input_baselines = {key: statistics.median(values) for key, values in input_power.items()}
    rows = []
    for input_port in range(1, 7):
        for output in range(1, 7):
            items = grouped[(input_port, output)]
            if not items:
                continue
            isolation = [item.routing_isolation_db for item in items]
            power = [item.target_power_dbm for item in items]
            rows.append({
                "input": input_port,
                "output": output,
                "samples": len(items),
                "median_routing_isolation_db": statistics.median(isolation),
                "p10_routing_isolation_db": _percentile(isolation, 10),
                "pass_rate_percent": 100.0 * sum(value >= analysis.threshold_db for value in isolation) / len(isolation),
                "median_target_share_percent": 100.0 * statistics.median(item.target_share for item in items),
                "median_target_power_dbm": statistics.median(power),
                "target_power_offset_from_input_median_db": statistics.median(power) - input_baselines[input_port],
            })
    return rows


def _write_port_pair_csv(path: Path, rows: Sequence[dict[str, float | int]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _fmt(value, 4) if isinstance(value, float) else value for key, value in row.items()})


def _switch_diagnostic_rows(analysis: PermutationAnalysis) -> tuple[list[dict[str, float | int | str]], dict]:
    """Associate measured leakage with the output predicted by one MZI flip.

    A switch is a topology-consistent suspect when flipping it while holding all
    other saved states fixed sends the routed wavelength to its measured
    strongest wrong output.  When several switches predict the same output, one
    channel of attribution is divided equally among them.  The result is a
    diagnostic association rather than a causal decomposition.
    """
    network = BenesNetwork()
    groups: dict[tuple[str, str], dict[str, object]] = defaultdict(lambda: {
        "isolations": [],
        "strongest_matches": 0,
        "bad_strongest_matches": 0,
        "fractional_bad_attribution": 0.0,
        "attributed_shortfall_db": 0.0,
    })
    usable_scans = 0
    bad_channels = 0
    unmatched_bad_channels = 0
    for scan in analysis.scans:
        states = {name: value == "cross" for name, value in scan.switch_states.items()}
        if set(states) != set(network.switches):
            continue
        expected_mapping = tuple(output - 1 for output in scan.permutation)
        if network.mapping(states) != expected_mapping:
            continue
        usable_scans += 1
        for input_port, target_output in enumerate(scan.permutation, start=1):
            powers = [scan.peak_dbm[output - 1][input_port - 1] for output in range(1, 7)]
            target_power = powers[target_output - 1]
            strongest_leak_power, strongest_leak_output = max(
                (power, output)
                for output, power in enumerate(powers, start=1)
                if output != target_output
            )
            routing_isolation = target_power - strongest_leak_power
            candidates: list[tuple[str, str]] = []
            for switch, crossed in network.trace(input_port - 1, states).states:
                flipped = dict(states)
                flipped[switch] = not flipped[switch]
                alternative_output = network.trace(input_port - 1, flipped).rows[-1] + 1
                flip_isolation = target_power - powers[alternative_output - 1]
                key = (switch, "cross" if crossed else "bar")
                group = groups[key]
                group["isolations"].append(flip_isolation)
                if alternative_output == strongest_leak_output:
                    group["strongest_matches"] += 1
                    candidates.append(key)
                    if routing_isolation < analysis.threshold_db:
                        group["bad_strongest_matches"] += 1
            if routing_isolation < analysis.threshold_db:
                bad_channels += 1
                if not candidates:
                    unmatched_bad_channels += 1
                    continue
                share = 1.0 / len(candidates)
                shortfall = max(0.0, analysis.threshold_db - routing_isolation)
                for key in candidates:
                    groups[key]["fractional_bad_attribution"] += share
                    groups[key]["attributed_shortfall_db"] += share * shortfall

    rows: list[dict[str, float | int | str]] = []
    for (switch, state), group in groups.items():
        values = list(group["isolations"])
        exposures = len(values)
        below = sum(value < analysis.threshold_db for value in values)
        rows.append({
            "switch": switch,
            "stage": network.switches[switch][0] + 1,
            "state": state,
            "route_exposures": exposures,
            "median_single_flip_isolation_db": statistics.median(values),
            "p10_single_flip_isolation_db": _percentile(values, 10),
            "p90_single_flip_isolation_db": _percentile(values, 90),
            "below_threshold_rate_percent": 100.0 * below / exposures,
            "strongest_leak_matches": int(group["strongest_matches"]),
            "bad_strongest_leak_matches": int(group["bad_strongest_matches"]),
            "fractional_bad_channel_attribution": float(group["fractional_bad_attribution"]),
            "attributed_shortfall_db": float(group["attributed_shortfall_db"]),
            "shortfall_per_exposure_db": float(group["attributed_shortfall_db"]) / exposures,
        })
    rows.sort(key=lambda row: (-float(row["shortfall_per_exposure_db"]), str(row["switch"]), str(row["state"])))
    metadata = {
        "scans_with_valid_switch_states": usable_scans,
        "bad_channels_below_threshold": bad_channels,
        "unmatched_bad_channels": unmatched_bad_channels,
        "attribution_note": (
            "A suspect is counted when a one-switch state flip predicts the measured strongest-leak output. "
            "Shared predictions receive fractional attribution; association does not prove causality."
        ),
    }
    return rows, metadata


def _input_rows(analysis: PermutationAnalysis) -> list[dict[str, float | int]]:
    grouped: dict[int, list[ChannelMetric]] = defaultdict(list)
    worst_counts: dict[int, int] = defaultdict(int)
    for item in analysis.channels:
        grouped[item.input_port].append(item)
    for item in analysis.permutations:
        worst_counts[item.worst_input] += 1
    rows = []
    for input_port in range(1, 7):
        items = grouped[input_port]
        if not items:
            continue
        isolation = [item.routing_isolation_db for item in items]
        rows.append({
            "input": input_port,
            "samples": len(items),
            "median_routing_isolation_db": statistics.median(isolation),
            "p05_routing_isolation_db": _percentile(isolation, 5),
            "p10_routing_isolation_db": _percentile(isolation, 10),
            "p25_routing_isolation_db": _percentile(isolation, 25),
            "p75_routing_isolation_db": _percentile(isolation, 75),
            "p90_routing_isolation_db": _percentile(isolation, 90),
            "p95_routing_isolation_db": _percentile(isolation, 95),
            "pass_rate_percent": 100.0 * sum(value >= analysis.threshold_db for value in isolation) / len(isolation),
            "median_target_share_percent": 100.0 * statistics.median(item.target_share for item in items),
            "times_worst_channel": worst_counts[input_port],
            "worst_channel_rate_percent": (
                100.0 * worst_counts[input_port] / len(analysis.permutations)
                if analysis.permutations else 0.0
            ),
        })
    return rows


def _output_rows(analysis: PermutationAnalysis) -> list[dict[str, float | int]]:
    grouped: dict[int, list[ChannelMetric]] = defaultdict(list)
    worst_counts: dict[int, int] = defaultdict(int)
    for item in analysis.channels:
        grouped[item.target_output].append(item)
    for item in analysis.permutations:
        worst_output = item.permutation[item.worst_input - 1]
        worst_counts[worst_output] += 1
    rows = []
    for output in range(1, 7):
        items = grouped[output]
        if not items:
            continue
        isolation = [item.routing_isolation_db for item in items]
        rows.append({
            "output": output,
            "samples": len(items),
            "median_routing_isolation_db": statistics.median(isolation),
            "p05_routing_isolation_db": _percentile(isolation, 5),
            "p10_routing_isolation_db": _percentile(isolation, 10),
            "p25_routing_isolation_db": _percentile(isolation, 25),
            "p75_routing_isolation_db": _percentile(isolation, 75),
            "p90_routing_isolation_db": _percentile(isolation, 90),
            "p95_routing_isolation_db": _percentile(isolation, 95),
            "pass_rate_percent": 100.0 * sum(value >= analysis.threshold_db for value in isolation) / len(isolation),
            "median_target_share_percent": 100.0 * statistics.median(item.target_share for item in items),
            "times_worst_channel": worst_counts[output],
            "worst_channel_rate_percent": (
                100.0 * worst_counts[output] / len(analysis.permutations)
                if analysis.permutations else 0.0
            ),
        })
    return rows


def summary_payload(
    analysis: PermutationAnalysis,
    pair_rows: Sequence[dict[str, float | int]],
    input_rows: Sequence[dict[str, float | int]],
    output_rows: Sequence[dict[str, float | int]],
    switch_rows: Sequence[dict[str, float | int | str]],
    switch_metadata: dict,
) -> dict:
    isolation = [item.routing_isolation_db for item in analysis.channels]
    worst = [item.worst_routing_isolation_db for item in analysis.permutations]
    passing = sum(item.passes_threshold for item in analysis.permutations)
    weakest_pair = min(pair_rows, key=lambda row: float(row["median_routing_isolation_db"])) if pair_rows else None
    strongest_pair = max(pair_rows, key=lambda row: float(row["median_routing_isolation_db"])) if pair_rows else None
    limiting_input = max(input_rows, key=lambda row: int(row["times_worst_channel"])) if input_rows else None
    worst_permutations = sorted(
        analysis.permutations,
        key=lambda item: item.worst_routing_isolation_db,
    )[:10]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset_root": str(analysis.dataset_root),
        "discovered_scan_files": analysis.discovered_scans,
        "valid_permutations_used": len(analysis.permutations),
        "channels_analyzed": len(analysis.channels),
        "missing_permutation_numbers": analysis.missing_permutation_numbers,
        "duplicate_permutation_numbers": analysis.duplicate_permutation_numbers,
        "invalid_scans": [{"path": str(path), "error": error} for path, error in analysis.invalid_scans],
        "routing_isolation_db": {
            "channel_p05": _percentile(isolation, 5),
            "channel_median": _percentile(isolation, 50),
            "channel_p95": _percentile(isolation, 95),
            "permutation_worst_p05": _percentile(worst, 5),
            "permutation_worst_median": _percentile(worst, 50),
            "permutation_worst_p95": _percentile(worst, 95),
        },
        "threshold": {
            "routing_isolation_db": analysis.threshold_db,
            "passing_permutations": passing,
            "evaluated_permutations": len(analysis.permutations),
            "yield_percent": 100.0 * passing / len(analysis.permutations) if analysis.permutations else 0.0,
        },
        "weakest_port_pair": weakest_pair,
        "strongest_port_pair": strongest_pair,
        "limiting_input": limiting_input,
        "input_summary": list(input_rows),
        "output_summary": list(output_rows),
        "switch_diagnostics": switch_metadata | {"leading_suspects": list(switch_rows[:8])},
        "worst_permutations": [
            {
                "number": item.permutation_number,
                "permutation": list(item.permutation),
                "worst_input": item.worst_input,
                "worst_routing_isolation_db": item.worst_routing_isolation_db,
                "minimum_target_share_percent": 100.0 * item.minimum_target_share,
            }
            for item in worst_permutations
        ],
    }


def _write_markdown_summary(path: Path, payload: dict) -> None:
    threshold = payload["threshold"]
    isolation = payload["routing_isolation_db"]
    weak = payload["weakest_port_pair"]
    strong = payload["strongest_port_pair"]
    limiting_input = payload["limiting_input"]
    lines = [
        "# 720-permutation dataset analysis",
        "",
        f"- Valid permutations: **{payload['valid_permutations_used']} / 720**",
        f"- Routed channels: **{payload['channels_analyzed']}**",
        f"- Yield at the selected **{threshold['routing_isolation_db']:g} dB** worst-channel criterion: "
        f"**{threshold['passing_permutations']} / {threshold['evaluated_permutations']} "
        f"({threshold['yield_percent']:.1f}%)**",
        f"- Channel routing isolation: median **{isolation['channel_median']:.2f} dB**, "
        f"5th–95th percentile **{isolation['channel_p05']:.2f} to {isolation['channel_p95']:.2f} dB**",
        f"- Per-permutation worst-channel isolation: median **{isolation['permutation_worst_median']:.2f} dB**, "
        f"5th percentile **{isolation['permutation_worst_p05']:.2f} dB**",
    ]
    if weak and strong:
        lines.extend((
            f"- Weakest median port pair: **In{weak['input']} → Out{weak['output']}** "
            f"({weak['median_routing_isolation_db']:.2f} dB; n={weak['samples']})",
            f"- Strongest median port pair: **In{strong['input']} → Out{strong['output']}** "
            f"({strong['median_routing_isolation_db']:.2f} dB; n={strong['samples']})",
        ))
    if limiting_input:
        lines.append(
            f"- Most frequent limiting channel: **Input {limiting_input['input']}**, worst in "
            f"**{limiting_input['times_worst_channel']} / {payload['valid_permutations_used']} "
            f"permutations ({limiting_input['worst_channel_rate_percent']:.1f}%)**, with median routing "
            f"isolation **{limiting_input['median_routing_isolation_db']:.2f} dB**"
        )
    if payload["missing_permutation_numbers"]:
        lines.append(f"- Missing permutation numbers: {payload['missing_permutation_numbers']}")
    if payload["duplicate_permutation_numbers"]:
        lines.append(
            "- Duplicate permutations (newest scan used): "
            f"{payload['duplicate_permutation_numbers']}"
        )
    if payload["invalid_scans"]:
        lines.append(f"- Invalid scan files ignored: {len(payload['invalid_scans'])}")
    lines.extend((
        "",
        "## Per-input diagnosis",
        "",
        "| Input | Median isolation | 5th percentile | Channels passing criterion | Median target share | Times worst in permutation |",
        "|---:|---:|---:|---:|---:|---:|",
    ))
    for item in payload["input_summary"]:
        lines.append(
            f"| In{item['input']} | {item['median_routing_isolation_db']:.2f} dB | "
            f"{item['p05_routing_isolation_db']:.2f} dB | {item['pass_rate_percent']:.1f}% | "
            f"{item['median_target_share_percent']:.1f}% | {item['times_worst_channel']} "
            f"({item['worst_channel_rate_percent']:.1f}%) |"
        )
    lines.extend((
        "",
        "## Per-output diagnosis",
        "",
        "These groups use the channel's intended output port.",
        "",
        "| Output | Median isolation | 5th percentile | Channels passing criterion | Median target share | Times worst in permutation |",
        "|---:|---:|---:|---:|---:|---:|",
    ))
    for item in payload["output_summary"]:
        lines.append(
            f"| Out{item['output']} | {item['median_routing_isolation_db']:.2f} dB | "
            f"{item['p05_routing_isolation_db']:.2f} dB | {item['pass_rate_percent']:.1f}% | "
            f"{item['median_target_share_percent']:.1f}% | {item['times_worst_channel']} "
            f"({item['worst_channel_rate_percent']:.1f}%) |"
        )
    switch_diagnostics = payload["switch_diagnostics"]
    lines.extend((
        "",
        "## Topology-aware switch suspects",
        "",
        switch_diagnostics["attribution_note"],
        "",
        "| Rank | Switch | State | Exposures | Median flip isolation | 10th percentile | Fractional bad-channel attribution | Shortfall per exposure |",
        "|---:|:---|:---|---:|---:|---:|---:|---:|",
    ))
    for rank, item in enumerate(switch_diagnostics["leading_suspects"], start=1):
        lines.append(
            f"| {rank} | {item['switch']} | {item['state']} | {item['route_exposures']} | "
            f"{item['median_single_flip_isolation_db']:.2f} dB | "
            f"{item['p10_single_flip_isolation_db']:.2f} dB | "
            f"{item['fractional_bad_channel_attribution']:.1f} | "
            f"{item['shortfall_per_exposure_db']:.2f} dB |"
        )
    lines.extend((
        "",
        "## Worst permutations",
        "",
        "| Rank | Permutation | Mapping (outputs for inputs 1–6) | Worst input | Worst isolation | Minimum target share |",
        "|---:|---:|:---|---:|---:|---:|",
    ))
    for rank, item in enumerate(payload["worst_permutations"], start=1):
        lines.append(
            f"| {rank} | P{item['number']:03d} | {' '.join(map(str, item['permutation']))} | "
            f"In{item['worst_input']} | {item['worst_routing_isolation_db']:.2f} dB | "
            f"{item['minimum_target_share_percent']:.1f}% |"
        )
    lines.extend((
        "",
        "## Metric interpretation",
        "",
        "**Routing isolation** compares one input wavelength at its intended output with the strongest "
        "appearance of that same wavelength at any wrong output. This is the primary switch-quality "
        "metric because unequal laser powers cancel.",
        "",
        "**Target share** is the intended-output power divided by that wavelength's total power across "
        "all six outputs. **Output isolation** instead compares different wavelengths at one output and "
        "is therefore affected by unequal source powers.",
        "",
        "Target power is reported, but it is not absolute insertion loss unless a calibrated input-power "
        "reference is available.",
        "",
    ))
    path.write_text("\n".join(lines), encoding="utf-8")


def _annotated_heatmap(ax, values, title, colorbar_label, *, vmin=None, vmax=None, cmap="RdYlGn"):
    import numpy as np

    image = ax.imshow(values, origin="lower", cmap=cmap, vmin=vmin, vmax=vmax, aspect="equal")
    ax.set_xticks(range(6), [f"Out {value}" for value in range(1, 7)])
    ax.set_yticks(range(6), [f"In {value}" for value in range(1, 7)])
    ax.set_title(title, loc="left", fontweight="bold")
    for row in range(6):
        for column in range(6):
            value = values[row, column]
            if np.isfinite(value):
                ax.text(column, row, f"{value:.1f}", ha="center", va="center", fontsize=8)
    colorbar = ax.figure.colorbar(image, ax=ax, shrink=0.82)
    colorbar.set_label(colorbar_label)
    return image


def render_dashboard(path: Path, analysis: PermutationAnalysis, pair_rows: Sequence[dict[str, float | int]]) -> None:
    """Render a publication-friendly overview without opening a GUI window."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    isolation_grid = np.full((6, 720), np.nan)
    for item in analysis.channels:
        isolation_grid[item.input_port - 1, item.permutation_number - 1] = item.routing_isolation_db

    pair_isolation = np.full((6, 6), np.nan)
    pair_power_offset = np.full((6, 6), np.nan)
    for row in pair_rows:
        index = (int(row["input"]) - 1, int(row["output"]) - 1)
        pair_isolation[index] = float(row["median_routing_isolation_db"])
        pair_power_offset[index] = float(row["target_power_offset_from_input_median_db"])

    leakage_groups: dict[tuple[int, int], list[float]] = defaultdict(list)
    for scan in analysis.scans:
        for input_port, target_output in enumerate(scan.permutation, start=1):
            powers = [_dbm_to_mw(scan.peak_dbm[output - 1][input_port - 1]) for output in range(1, 7)]
            total = math.fsum(powers)
            for measured_output, power in enumerate(powers, start=1):
                leakage_groups[(target_output, measured_output)].append(power / total)
    leakage_map = np.full((6, 6), np.nan)
    for target_output in range(1, 7):
        for measured_output in range(1, 7):
            values = leakage_groups[(target_output, measured_output)]
            if values:
                leakage_map[target_output - 1, measured_output - 1] = 10.0 * math.log10(statistics.median(values))

    fig = plt.figure(figsize=(18, 14), constrained_layout=True)
    grid = fig.add_gridspec(3, 3, height_ratios=(1.05, 1.2, 1.2))
    ax_barcode = fig.add_subplot(grid[0, :])
    image = ax_barcode.imshow(isolation_grid, origin="upper", aspect="auto", cmap="RdYlGn", vmin=-10, vmax=30)
    ax_barcode.set_title("Permutation quality map — same-wavelength routing isolation", loc="left", fontweight="bold")
    ax_barcode.set_ylabel("Routed input")
    ax_barcode.set_yticks(range(6), [f"In {value}" for value in range(1, 7)])
    tick_numbers = list(range(1, 721, 60)) + [720]
    ax_barcode.set_xticks([value - 1 for value in tick_numbers], [f"P{value:03d}" for value in tick_numbers])
    ax_barcode.set_xlabel("Lexicographic permutation number")
    colorbar = fig.colorbar(image, ax=ax_barcode, orientation="horizontal", shrink=0.55, pad=0.18, aspect=45)
    colorbar.set_label("Target output minus strongest wrong output [dB] — higher is better")

    ax_yield = fig.add_subplot(grid[1, 0])
    worst = np.asarray([item.worst_routing_isolation_db for item in analysis.permutations])
    if len(worst):
        lower = min(-10.0, math.floor(float(np.nanmin(worst))))
        upper = max(30.0, math.ceil(float(np.nanmax(worst))))
        thresholds = np.linspace(lower, upper, 400)
        yields = [100.0 * float(np.count_nonzero(worst >= value)) / len(worst) for value in thresholds]
        ax_yield.plot(thresholds, yields, color="#2673b8", linewidth=2.2)
        selected_yield = 100.0 * float(np.count_nonzero(worst >= analysis.threshold_db)) / len(worst)
        ax_yield.axvline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=1.5)
        ax_yield.scatter([analysis.threshold_db], [selected_yield], color="#d1495b", zorder=3)
        ax_yield.annotate(
            f"{selected_yield:.1f}% at {analysis.threshold_db:g} dB",
            (analysis.threshold_db, selected_yield), xytext=(8, 8), textcoords="offset points",
        )
    ax_yield.set_title("Permutation yield curve", loc="left", fontweight="bold")
    ax_yield.set_xlabel("Required worst-channel isolation [dB]")
    ax_yield.set_ylabel("Permutations passing [%]")
    ax_yield.set_ylim(-2, 102)
    ax_yield.grid(alpha=0.25)

    ax_pair_iso = fig.add_subplot(grid[1, 1])
    finite_isolation = pair_isolation[np.isfinite(pair_isolation)]
    iso_min = min(0.0, float(np.min(finite_isolation))) if len(finite_isolation) else 0.0
    iso_max = max(20.0, float(np.max(finite_isolation))) if len(finite_isolation) else 20.0
    _annotated_heatmap(
        ax_pair_iso, pair_isolation,
        "Median isolation by routed port pair", "Median routing isolation [dB]",
        vmin=iso_min, vmax=iso_max,
    )

    ax_power = fig.add_subplot(grid[1, 2])
    power_limit = max(1.0, float(np.nanmax(np.abs(pair_power_offset))))
    _annotated_heatmap(
        ax_power, pair_power_offset,
        "Route-associated target-power offset", "Offset from each input's median [dB]",
        vmin=-power_limit, vmax=power_limit, cmap="RdBu_r",
    )

    ax_leakage = fig.add_subplot(grid[2, 0])
    image = ax_leakage.imshow(leakage_map, origin="lower", cmap="viridis", vmin=-40, vmax=0, aspect="equal")
    ax_leakage.set_xticks(range(6), [f"Out {value}" for value in range(1, 7)])
    ax_leakage.set_yticks(range(6), [f"Out {value}" for value in range(1, 7)])
    ax_leakage.set_xlabel("Measured output")
    ax_leakage.set_ylabel("Intended output")
    ax_leakage.set_title("Median wavelength power distribution", loc="left", fontweight="bold")
    for row in range(6):
        for column in range(6):
            if np.isfinite(leakage_map[row, column]):
                ax_leakage.text(column, row, f"{leakage_map[row, column]:.1f}", ha="center", va="center", fontsize=8,
                                color="white" if leakage_map[row, column] < -18 else "black")
    colorbar = fig.colorbar(image, ax=ax_leakage, shrink=0.82)
    colorbar.set_label("Median share of wavelength power [dB]")

    ax_hist = fig.add_subplot(grid[2, 1])
    channel_isolation = [item.routing_isolation_db for item in analysis.channels]
    if channel_isolation:
        ax_hist.hist(channel_isolation, bins=45, color="#4f8fba", alpha=0.82, label="All routed channels")
        ax_hist.axvline(analysis.threshold_db, color="#d1495b", linestyle="--", label=f"{analysis.threshold_db:g} dB criterion")
    ax_hist.set_title("Routing-isolation distribution", loc="left", fontweight="bold")
    ax_hist.set_xlabel("Routing isolation [dB]")
    ax_hist.set_ylabel("Channel count")
    ax_hist.grid(axis="y", alpha=0.2)
    ax_hist.legend(frameon=False)

    ax_table = fig.add_subplot(grid[2, 2])
    ax_table.axis("off")
    worst_rows = sorted(analysis.permutations, key=lambda item: item.worst_routing_isolation_db)[:10]
    cell_text = [[
        str(rank), f"P{item.permutation_number:03d}",
        " ".join(map(str, item.permutation)), f"In{item.worst_input}",
        f"{item.worst_routing_isolation_db:.2f}", f"{100 * item.minimum_target_share:.1f}%",
    ] for rank, item in enumerate(worst_rows, start=1)]
    table = ax_table.table(
        cellText=cell_text,
        colLabels=["Rank", "Perm.", "Outputs for inputs 1–6", "Worst", "Iso. [dB]", "Share"],
        cellLoc="center", colLoc="center", loc="center",
        colWidths=[0.08, 0.12, 0.34, 0.12, 0.16, 0.12],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8.2)
    table.scale(1.0, 1.45)
    ax_table.set_title("Ten weakest permutations", loc="left", fontweight="bold", pad=14)

    fig.suptitle(
        f"Six-port Beneš permutation dataset — {len(analysis.permutations)}/720 permutations, "
        f"{len(analysis.channels)} routed channels",
        fontsize=16, fontweight="bold",
    )
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def render_switch_diagnostics(
    path: Path,
    analysis: PermutationAnalysis,
    rows: Sequence[dict[str, float | int | str]],
) -> None:
    """Plot the one-switch-flip isolation distribution for each commanded state."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ordered = sorted(rows, key=lambda row: (int(row["stage"]), str(row["switch"]), str(row["state"])))
    if not ordered:
        return
    positions = list(range(len(ordered)))
    medians = [float(row["median_single_flip_isolation_db"]) for row in ordered]
    lower = [median - float(row["p10_single_flip_isolation_db"]) for median, row in zip(medians, ordered)]
    upper = [float(row["p90_single_flip_isolation_db"]) - median for median, row in zip(medians, ordered)]
    colors = ["#2673b8" if row["state"] == "bar" else "#e07a3f" for row in ordered]
    labels = [f"{row['switch']} {row['state']} (n={row['route_exposures']})" for row in ordered]

    fig, ax = plt.subplots(figsize=(11, 10), constrained_layout=True)
    for position, median, low, high, color in zip(positions, medians, lower, upper, colors):
        ax.errorbar(median, position, xerr=[[low], [high]], fmt="o", color=color,
                    capsize=3, markersize=6, linewidth=1.5)
    ax.axvline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=1.5,
               label=f"{analysis.threshold_db:g} dB diagnostic criterion")
    ax.axvline(0, color="black", linewidth=0.8, alpha=0.5)
    ax.set_yticks(positions, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Target power minus power at the one-switch-flip output [dB]")
    ax.set_title("Topology-aware switch/state diagnostic", loc="left", fontweight="bold")
    ax.grid(axis="x", alpha=0.25)
    ax.legend(frameon=False)
    ax.text(
        0.0, -0.07,
        "Points are medians; bars span the 10th–90th percentiles. Lower values indicate a stronger "
        "measured leak at the output predicted by flipping that MZI.",
        transform=ax.transAxes, fontsize=9,
    )
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _style_isolation_histogram(ax, values, bins, analysis: PermutationAnalysis, title: str, ymax: float | None = None) -> None:
    import statistics as stats

    ax.hist(values, bins=bins, color="#4f8fba", alpha=0.86, edgecolor="white", linewidth=0.35)
    ax.axvline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=1.5,
               label=f"{analysis.threshold_db:g} dB criterion")
    median = stats.median(values)
    passing = 100.0 * sum(value >= analysis.threshold_db for value in values) / len(values)
    ax.axvline(median, color="#303030", linestyle=":", linewidth=1.4, label=f"Median {median:.2f} dB")
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_xlabel("Routing isolation [dB]")
    ax.set_ylabel("Channel count")
    if ymax is not None:
        ax.set_ylim(0, ymax)
    ax.grid(axis="y", alpha=0.2)
    ax.text(0.98, 0.95, f"n={len(values)}\npass={passing:.1f}%", transform=ax.transAxes,
            ha="right", va="top", fontsize=9,
            bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.8, "edgecolor": "none"})
    ax.legend(frameon=False, fontsize=8)


def _style_percentile_curve(
    ax,
    values,
    analysis: PermutationAnalysis,
    title: str,
    isolation_limits: tuple[float, float] | None = None,
) -> None:
    import numpy as np

    ordered = np.sort(np.asarray(values, dtype=float))
    percentiles = 100.0 * (np.arange(len(ordered)) + 0.5) / len(ordered)
    ax.plot(percentiles, ordered, color="#2673b8", linewidth=2.0)
    ax.axhline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=1.5,
               label=f"{analysis.threshold_db:g} dB criterion")
    markers = ((5, "P05"), (50, "P50"), (95, "P95"))
    for percentile, label in markers:
        value = _percentile(values, percentile)
        ax.scatter([percentile], [value], s=34, zorder=3,
                   color="#303030" if percentile == 50 else "#e09f3e")
        ax.annotate(f"{label} {value:.2f} dB", (percentile, value),
                    xytext=(6, 5 if percentile != 95 else -14), textcoords="offset points",
                    fontsize=8, va="bottom" if percentile != 95 else "top")
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_xlabel("Empirical percentile [%]")
    ax.set_ylabel("Routing isolation [dB]")
    ax.set_xlim(0, 100)
    if isolation_limits is not None:
        ax.set_ylim(*isolation_limits)
    ax.grid(alpha=0.22)
    ax.legend(frameon=False, fontsize=8)


def _render_histogram_and_percentile(
    path: Path,
    values: Sequence[float],
    bins,
    analysis: PermutationAnalysis,
    label: str,
    histogram_ymax: float,
    isolation_limits: tuple[float, float],
) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.4), constrained_layout=True)
    _style_isolation_histogram(axes[0], values, bins, analysis, f"{label} — count histogram", histogram_ymax)
    _style_percentile_curve(axes[1], values, analysis, f"{label} — percentile curve", isolation_limits)
    fig.suptitle(
        "Same-wavelength routing isolation: target output minus strongest wrong output",
        fontsize=13, fontweight="bold",
    )
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def render_separate_figures(
    output_dir: Path,
    analysis: PermutationAnalysis,
    pair_rows: Sequence[dict[str, float | int]],
) -> dict[str, Path]:
    """Render every analysis view as its own figure, plus marginal histograms."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    figures = output_dir / "Figures"
    input_histograms = figures / "isolation_by_input"
    output_histograms = figures / "isolation_by_output"
    figures.mkdir(parents=True, exist_ok=True)
    input_histograms.mkdir(parents=True, exist_ok=True)
    output_histograms.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    isolation_grid = np.full((6, 720), np.nan)
    for item in analysis.channels:
        isolation_grid[item.input_port - 1, item.permutation_number - 1] = item.routing_isolation_db
    fig, ax = plt.subplots(figsize=(18, 4.5), constrained_layout=True)
    image = ax.imshow(isolation_grid, origin="upper", aspect="auto", cmap="RdYlGn", vmin=-10, vmax=30)
    ax.set_title("Permutation quality map — same-wavelength routing isolation", loc="left", fontweight="bold")
    ax.set_ylabel("Routed input")
    ax.set_yticks(range(6), [f"In {value}" for value in range(1, 7)])
    tick_numbers = list(range(1, 721, 60)) + [720]
    ax.set_xticks([value - 1 for value in tick_numbers], [f"P{value:03d}" for value in tick_numbers])
    ax.set_xlabel("Lexicographic permutation number")
    colorbar = fig.colorbar(image, ax=ax, orientation="horizontal", shrink=0.65, pad=0.22, aspect=50)
    colorbar.set_label("Target output minus strongest wrong output for the same wavelength [dB]")
    paths["quality_map"] = figures / "01_permutation_quality_map.png"
    fig.savefig(paths["quality_map"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    worst = np.asarray([item.worst_routing_isolation_db for item in analysis.permutations])
    fig, ax = plt.subplots(figsize=(8.5, 5.5), constrained_layout=True)
    if len(worst):
        lower = min(-10.0, math.floor(float(np.nanmin(worst))))
        upper = max(30.0, math.ceil(float(np.nanmax(worst))))
        thresholds = np.linspace(lower, upper, 400)
        yields = [100.0 * float(np.count_nonzero(worst >= value)) / len(worst) for value in thresholds]
        selected_yield = 100.0 * float(np.count_nonzero(worst >= analysis.threshold_db)) / len(worst)
        ax.plot(thresholds, yields, color="#2673b8", linewidth=2.2)
        ax.axvline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=1.5)
        ax.scatter([analysis.threshold_db], [selected_yield], color="#d1495b", zorder=3)
        ax.annotate(f"{selected_yield:.1f}% at {analysis.threshold_db:g} dB",
                    (analysis.threshold_db, selected_yield), xytext=(8, 8), textcoords="offset points")
    ax.set_title("Permutation yield — all six channels must pass", loc="left", fontweight="bold")
    ax.set_xlabel("Required worst-channel routing isolation [dB]")
    ax.set_ylabel("Permutations passing [%]")
    ax.set_ylim(-2, 102)
    ax.grid(alpha=0.25)
    paths["yield_curve"] = figures / "02_permutation_yield_curve.png"
    fig.savefig(paths["yield_curve"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    pair_isolation = np.full((6, 6), np.nan)
    pair_power_offset = np.full((6, 6), np.nan)
    for row in pair_rows:
        index = (int(row["input"]) - 1, int(row["output"]) - 1)
        pair_isolation[index] = float(row["median_routing_isolation_db"])
        pair_power_offset[index] = float(row["target_power_offset_from_input_median_db"])
    fig, ax = plt.subplots(figsize=(8, 7), constrained_layout=True)
    finite = pair_isolation[np.isfinite(pair_isolation)]
    _annotated_heatmap(ax, pair_isolation, "Median routing isolation by input–output pair",
                       "Median routing isolation [dB]", vmin=min(0.0, float(np.min(finite))),
                       vmax=max(20.0, float(np.max(finite))))
    paths["port_pair_isolation"] = figures / "03_port_pair_isolation_heatmap.png"
    fig.savefig(paths["port_pair_isolation"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 7), constrained_layout=True)
    power_limit = max(1.0, float(np.nanmax(np.abs(pair_power_offset))))
    _annotated_heatmap(ax, pair_power_offset, "Route-associated target-power offset",
                       "Offset from each input's median [dB]", vmin=-power_limit,
                       vmax=power_limit, cmap="RdBu_r")
    paths["target_power_offset"] = figures / "04_target_power_offset_heatmap.png"
    fig.savefig(paths["target_power_offset"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    leakage_groups: dict[tuple[int, int], list[float]] = defaultdict(list)
    for scan in analysis.scans:
        for input_port, target_output in enumerate(scan.permutation, start=1):
            powers = [_dbm_to_mw(scan.peak_dbm[output - 1][input_port - 1]) for output in range(1, 7)]
            total = math.fsum(powers)
            for measured_output, power in enumerate(powers, start=1):
                leakage_groups[(target_output, measured_output)].append(power / total)
    leakage_map = np.full((6, 6), np.nan)
    for target_output in range(1, 7):
        for measured_output in range(1, 7):
            values = leakage_groups[(target_output, measured_output)]
            if values:
                leakage_map[target_output - 1, measured_output - 1] = 10.0 * math.log10(statistics.median(values))
    fig, ax = plt.subplots(figsize=(8, 7), constrained_layout=True)
    image = ax.imshow(leakage_map, origin="lower", cmap="viridis", vmin=-40, vmax=0, aspect="equal")
    ax.set_xticks(range(6), [f"Out {value}" for value in range(1, 7)])
    ax.set_yticks(range(6), [f"Out {value}" for value in range(1, 7)])
    ax.set_xlabel("Measured output")
    ax.set_ylabel("Intended output")
    ax.set_title("Median distribution of each wavelength across outputs", loc="left", fontweight="bold")
    for row in range(6):
        for column in range(6):
            value = leakage_map[row, column]
            ax.text(column, row, f"{value:.1f}", ha="center", va="center", fontsize=9,
                    color="white" if value < -18 else "black")
    colorbar = fig.colorbar(image, ax=ax, shrink=0.82)
    colorbar.set_label("Median share of wavelength power [dB]")
    paths["power_distribution"] = figures / "05_wavelength_power_distribution.png"
    fig.savefig(paths["power_distribution"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    all_isolation = [item.routing_isolation_db for item in analysis.channels]
    minimum = math.floor(min(all_isolation))
    maximum = math.ceil(max(all_isolation))
    bins = np.linspace(minimum, maximum, 51)
    isolation_limits = (minimum, maximum)
    fig, ax = plt.subplots(figsize=(8.5, 5.5), constrained_layout=True)
    _style_isolation_histogram(ax, all_isolation, bins, analysis,
                               "Routing-isolation distribution — all 4,320 channels")
    paths["all_channel_histogram"] = figures / "06_isolation_histogram_all_channels.png"
    fig.savefig(paths["all_channel_histogram"], dpi=180, bbox_inches="tight")
    plt.close(fig)

    input_groups = {port: [item.routing_isolation_db for item in analysis.channels if item.input_port == port]
                    for port in range(1, 7)}
    output_groups = {port: [item.routing_isolation_db for item in analysis.channels if item.target_output == port]
                     for port in range(1, 7)}
    marginal_groups = list(input_groups.values()) + list(output_groups.values())
    ymax = 1.08 * max(int(np.histogram(values, bins=bins)[0].max()) for values in marginal_groups)
    for port, values in input_groups.items():
        fig, ax = plt.subplots(figsize=(8.5, 5.5), constrained_layout=True)
        _style_isolation_histogram(ax, values, bins, analysis, f"Routing isolation — Input {port}", ymax)
        path = input_histograms / f"input_{port}_isolation_histogram.png"
        paths[f"input_{port}_histogram"] = path
        fig.savefig(path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        combined_path = input_histograms / f"input_{port}_histogram_and_percentile.png"
        paths[f"input_{port}_histogram_and_percentile"] = combined_path
        _render_histogram_and_percentile(
            combined_path, values, bins, analysis, f"Input {port}", ymax, isolation_limits,
        )
    for port, values in output_groups.items():
        fig, ax = plt.subplots(figsize=(8.5, 5.5), constrained_layout=True)
        _style_isolation_histogram(ax, values, bins, analysis,
                                   f"Routing isolation — channels intended for Output {port}", ymax)
        path = output_histograms / f"output_{port}_isolation_histogram.png"
        paths[f"output_{port}_histogram"] = path
        fig.savefig(path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        combined_path = output_histograms / f"output_{port}_histogram_and_percentile.png"
        paths[f"output_{port}_histogram_and_percentile"] = combined_path
        _render_histogram_and_percentile(
            combined_path, values, bins, analysis, f"Intended Output {port}", ymax, isolation_limits,
        )

    for grouping_name, groups, filename in (
        ("input", input_groups, "08_input_isolation_percentile_curves.png"),
        ("intended output", output_groups, "09_output_isolation_percentile_curves.png"),
    ):
        fig, ax = plt.subplots(figsize=(9, 6), constrained_layout=True)
        for port, values in groups.items():
            ordered = np.sort(np.asarray(values, dtype=float))
            percentiles = 100.0 * (np.arange(len(ordered)) + 0.5) / len(ordered)
            ax.plot(percentiles, ordered, linewidth=1.8, label=f"{grouping_name.title()} {port}")
        ax.axhline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=1.5,
                   label=f"{analysis.threshold_db:g} dB criterion")
        ax.set_title(f"Routing-isolation percentile curves by {grouping_name}", loc="left", fontweight="bold")
        ax.set_xlabel("Empirical percentile [%]")
        ax.set_ylabel("Routing isolation [dB]")
        ax.set_xlim(0, 100)
        ax.set_ylim(*isolation_limits)
        ax.grid(alpha=0.22)
        ax.legend(frameon=False, ncol=2)
        key = f"{grouping_name.replace(' ', '_')}_percentile_curves"
        paths[key] = figures / filename
        fig.savefig(paths[key], dpi=180, bbox_inches="tight")
        plt.close(fig)

    pair_groups = {
        (input_port, output): [
            item.routing_isolation_db for item in analysis.channels
            if item.input_port == input_port and item.target_output == output
        ]
        for input_port in range(1, 7) for output in range(1, 7)
    }
    nonempty_pair_groups = [values for values in pair_groups.values() if values]
    pair_ymax = 1.08 * max(int(np.histogram(values, bins=bins)[0].max()) for values in nonempty_pair_groups)
    fig, axes = plt.subplots(6, 6, figsize=(19, 15), sharex=True, sharey=True, constrained_layout=True)
    for input_port in range(1, 7):
        for output in range(1, 7):
            ax = axes[input_port - 1, output - 1]
            values = pair_groups[(input_port, output)]
            ax.axvline(analysis.threshold_db, color="#d1495b", linestyle="--", linewidth=0.9)
            ax.set_ylim(0, pair_ymax)
            ax.grid(axis="y", alpha=0.15)
            if values:
                median = statistics.median(values)
                ax.hist(values, bins=bins, color="#4f8fba", alpha=0.88)
                ax.axvline(median, color="#303030", linestyle=":", linewidth=0.9)
                ax.text(0.96, 0.9, f"med {median:.1f}", transform=ax.transAxes,
                        ha="right", va="top", fontsize=7)
            else:
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                        ha="center", va="center", fontsize=8, color="#666666")
            if input_port == 1:
                ax.set_title(f"Out {output}")
            if output == 1:
                ax.set_ylabel(f"In {input_port}\ncount")
            if input_port == 6:
                ax.set_xlabel("isolation [dB]")
    fig.suptitle("Routing-isolation histograms for every input–output pair (n=120 per cell)",
                 fontsize=15, fontweight="bold")
    paths["port_pair_histograms"] = figures / "07_isolation_histograms_all_input_output_pairs.png"
    fig.savefig(paths["port_pair_histograms"], dpi=180, bbox_inches="tight")
    plt.close(fig)
    return paths


def write_analysis_outputs(analysis: PermutationAnalysis, output_dir: Path | str) -> dict[str, Path]:
    """Write tables, machine-readable summaries, and the dashboard image."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    pair_rows = _port_pair_rows(analysis)
    input_rows = _input_rows(analysis)
    output_rows = _output_rows(analysis)
    switch_rows, switch_metadata = _switch_diagnostic_rows(analysis)
    payload = summary_payload(analysis, pair_rows, input_rows, output_rows, switch_rows, switch_metadata)
    paths = {
        "dashboard": output / "permutation_analysis.png",
        "summary_markdown": output / "analysis_summary.md",
        "summary_json": output / "analysis_summary.json",
        "permutations_csv": output / "permutation_metrics.csv",
        "channels_csv": output / "channel_metrics.csv",
        "port_pairs_csv": output / "port_pair_summary.csv",
        "inputs_csv": output / "input_summary.csv",
        "outputs_csv": output / "output_summary.csv",
        "switches_csv": output / "switch_diagnostics.csv",
        "switches_plot": output / "switch_diagnostics.png",
    }
    _write_channel_csv(paths["channels_csv"], analysis)
    _write_permutation_csv(paths["permutations_csv"], analysis)
    _write_port_pair_csv(paths["port_pairs_csv"], pair_rows)
    _write_port_pair_csv(paths["inputs_csv"], input_rows)
    _write_port_pair_csv(paths["outputs_csv"], output_rows)
    _write_port_pair_csv(paths["switches_csv"], switch_rows)
    paths["summary_json"].write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _write_markdown_summary(paths["summary_markdown"], payload)
    render_dashboard(paths["dashboard"], analysis, pair_rows)
    render_switch_diagnostics(paths["switches_plot"], analysis, switch_rows)
    paths.update(render_separate_figures(output, analysis, pair_rows))
    return paths


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze all six-port WDM permutation scans in a Benes dataset.",
    )
    parser.add_argument("dataset", type=Path, help="Dataset root containing wdm_scan.json files")
    parser.add_argument(
        "--output", type=Path,
        help="Output directory (default: <dataset>/Analysis)",
    )
    parser.add_argument(
        "--isolation-threshold-db", type=float, default=10.0,
        help="Worst-channel routing-isolation criterion used for yield (default: 10 dB)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    output = args.output or args.dataset / "Analysis"
    try:
        analysis = analyze_permutation_set(args.dataset, args.isolation_threshold_db)
        if not analysis.permutations:
            raise PermutationAnalysisError("no valid WDM scans were found")
        paths = write_analysis_outputs(analysis, output)
    except (OSError, PermutationAnalysisError) as error:
        raise SystemExit(f"Analysis failed: {error}") from error

    passing = sum(item.passes_threshold for item in analysis.permutations)
    yield_percent = 100.0 * passing / len(analysis.permutations)
    print(f"Analyzed {len(analysis.permutations)}/720 permutations ({len(analysis.channels)} channels).")
    print(
        f"Yield at {analysis.threshold_db:g} dB worst-channel routing isolation: "
        f"{passing}/{len(analysis.permutations)} ({yield_percent:.1f}%)."
    )
    if analysis.missing_permutation_numbers:
        print(f"Missing permutations: {len(analysis.missing_permutation_numbers)}")
    if analysis.duplicate_permutation_numbers:
        print(f"Duplicate permutations (newest used): {len(analysis.duplicate_permutation_numbers)}")
    if analysis.invalid_scans:
        print(f"Invalid scans ignored: {len(analysis.invalid_scans)}")
    print(f"Dashboard: {paths['dashboard']}")
    print(f"Figures:   {Path(output) / 'Figures'}")
    print(f"Summary:   {paths['summary_markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
