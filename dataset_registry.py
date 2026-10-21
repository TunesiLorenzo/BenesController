"""Portable dataset registry for routed Benes measurements.

Each measurement folder has a small JSON sidecar describing exactly which
permutation and input/output connections it covers.  This avoids trying to
infer scientific coverage from filenames or plot labels.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from itertools import permutations
from pathlib import Path


DATASET_FILE = "benes_dataset.json"
MEASUREMENT_FILE = "measurement.json"
SCHEMA_VERSION = 1


def normalize_permutation(values) -> tuple[int, ...]:
    values = tuple(int(value) for value in values)
    if len(values) != 6 or set(values) != set(range(1, 7)):
        raise ValueError("A Benes permutation must contain each output 1–6 exactly once")
    return values


def permutation_key(permutation) -> str:
    return " ".join(map(str, normalize_permutation(permutation)))


def all_permutation_keys() -> tuple[str, ...]:
    return tuple(" ".join(map(str, item)) for item in permutations(range(1, 7)))


def _validate_ports(ports, label) -> tuple[int, ...]:
    values = tuple(sorted({int(port) for port in ports}))
    if not values or any(port not in range(1, 7) for port in values):
        raise ValueError(f"Select one or more {label} ports from 1 to 6")
    return values


def create_dataset(folder, scope="full_permutations", expected_permutations=None) -> dict:
    """Create or load a dataset root and state its expected coverage scope."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / DATASET_FILE
    if path.exists():
        return load_dataset(folder)
    if expected_permutations is None:
        expected = all_permutation_keys() if scope == "full_permutations" else []
    else:
        expected = [permutation_key(item) for item in expected_permutations]
    dataset = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": scope,
        "expected_permutations": expected,
    }
    _write_json(path, dataset)
    return dataset


def load_dataset(folder) -> dict:
    path = Path(folder) / DATASET_FILE
    if not path.exists():
        raise ValueError(f"{path.parent} is not a linked Benes dataset; initialize it first")
    try:
        dataset = json.loads(path.read_text(encoding="utf-8"))
        if dataset.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        dataset["expected_permutations"] = [permutation_key(item.split())
                                            for item in dataset.get("expected_permutations", [])]
        return dataset
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read {path.name}: {error}") from error


def connection_pairs(permutation, input_ports, output_ports, mode="routed_pairs") -> tuple[tuple[int, int], ...]:
    """Return atomic input/output records represented by one saved file.

    ``routed_pairs`` retains only pairs selected by the active permutation;
    ``cartesian`` records every selected input/output combination.  The latter
    supports future many-input/one-output and many-input/many-output fixtures.
    """
    permutation = normalize_permutation(permutation)
    inputs = _validate_ports(input_ports, "input")
    outputs = _validate_ports(output_ports, "output")
    if mode == "routed_pairs":
        pairs = tuple((input_port, permutation[input_port - 1]) for input_port in inputs
                      if permutation[input_port - 1] in outputs)
    elif mode == "cartesian":
        pairs = tuple((input_port, output_port) for input_port in inputs for output_port in outputs)
    else:
        raise ValueError("Connection mode must be routed_pairs or cartesian")
    if not pairs:
        raise ValueError("The selected ports contain no routed connection for this permutation")
    return pairs


def save_measurement(folder, *, source, permutation, input_ports, output_ports, mode,
                     files, comment="", extra=None) -> Path:
    """Register a measurement folder and return its durable metadata path."""
    root = Path(folder)
    load_dataset(root)
    permutation = normalize_permutation(permutation)
    inputs = _validate_ports(input_ports, "input")
    outputs = _validate_ports(output_ports, "output")
    pairs = connection_pairs(permutation, inputs, outputs, mode)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    measurement_dir = root / "Measurements" / f"{stamp}_{source.lower()}"
    measurement_dir.mkdir(parents=True, exist_ok=False)
    record = {
        "schema_version": SCHEMA_VERSION,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source),
        "permutation": list(permutation),
        "input_ports": list(inputs),
        "output_ports": list(outputs),
        "connection_mode": mode,
        "connections": [{"input": input_port, "output": output_port} for input_port, output_port in pairs],
        "files": list(files),
        "comment": str(comment),
        "extra": dict(extra or {}),
    }
    _write_json(measurement_dir / MEASUREMENT_FILE, record)
    return measurement_dir


def iter_measurements(folder):
    root = Path(folder)
    for path in sorted((root / "Measurements").glob(f"*/{MEASUREMENT_FILE}")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            permutation = normalize_permutation(record["permutation"])
            pairs = {(int(item["input"]), int(item["output"])) for item in record.get("connections", [])}
            if not pairs:
                continue
            yield path.parent, permutation, pairs, record
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue


def analyze_dataset(folder) -> dict:
    """Summarise every expected configuration and its missing routed paths."""
    root = Path(folder)
    dataset = load_dataset(root)
    expected = tuple(dataset["expected_permutations"])
    observed: dict[str, set[tuple[int, int]]] = {key: set() for key in expected}
    measurement_count = 0
    for _path, permutation, pairs, _record in iter_measurements(root):
        key = permutation_key(permutation)
        if key in observed:
            observed[key].update(pairs)
        measurement_count += 1
    configurations = []
    covered_paths = 0
    for key in expected:
        permutation = normalize_permutation(key.split())
        required = {(input_port, output_port) for input_port, output_port in enumerate(permutation, start=1)}
        covered = observed[key] & required
        missing = required - covered
        covered_paths += len(covered)
        configurations.append({
            "permutation": permutation,
            "covered": tuple(sorted(covered)),
            "missing": tuple(sorted(missing)),
            "complete": not missing,
        })
    return {
        "dataset": dataset,
        "measurement_count": measurement_count,
        "expected_configurations": len(configurations),
        "complete_configurations": sum(item["complete"] for item in configurations),
        "expected_paths": len(configurations) * 6,
        "covered_paths": covered_paths,
        "missing_paths": len(configurations) * 6 - covered_paths,
        "configurations": configurations,
    }


def write_coverage_report(folder, report) -> Path:
    path = Path(folder) / "coverage_report.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["permutation_outputs_for_inputs_1_to_6", "status", "covered_paths", "missing_paths"])
        for item in report["configurations"]:
            format_pairs = lambda pairs: "; ".join(f"In{input_port}->Out{output_port}" for input_port, output_port in pairs)
            writer.writerow([" ".join(map(str, item["permutation"])), "complete" if item["complete"] else "missing",
                             format_pairs(item["covered"]), format_pairs(item["missing"])])
    return path


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
