"""Compare two saved MZI calibrations, e.g. the primary and the alternative probe paths.

Each MZI's cross and bar voltages are compared as voltages and as heater
phase: the same state one fringe period away is the same calibration. The
phase comes from the fit (against V²) of either run's sweep, the first run's
when it is trusted. Pπ, the V² for a π phase, is a property of the heater and
should not depend on which laser and output fiber probed it.

Run with::

    python -m benes.measurements.calibration_compare <primary folder> <alternative folder>

A ``calibration_comparison.csv`` is written into the second folder.
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from benes.measurements.calibration_analysis import (CalibrationAnalysisError, CalibrationRun, MZIResult,
                                                     find_calibration_files, leakage_db, load_calibration)
from benes.measurements.mzi_fit import fit_trusted

# A phase error this small leaves under about -20 dB in the unwanted port.
AGREE_RAD = 0.2


@dataclass(frozen=True)
class StateComparison:
    first_v: float | None
    second_v: float | None
    # Wrapped heater phase between the two voltages, from the fit; None without a usable fit.
    phase_rad: float | None

    @property
    def delta_v(self) -> float | None:
        return None if self.first_v is None or self.second_v is None else self.second_v - self.first_v

    @property
    def leakage_db(self) -> float | None:
        return None if self.phase_rad is None else leakage_db(self.phase_rad)

    def verdict(self, step_v: float) -> str:
        if self.delta_v is None:
            return "missing"
        if abs(self.delta_v) <= step_v + 1e-9:
            return "agree"
        if self.phase_rad is None:
            return "differ (no fit)"
        if abs(self.phase_rad) <= AGREE_RAD:
            return "agree, other fringe" if abs(self.delta_v) > 0.5 else "agree"
        return "differ"


@dataclass(frozen=True)
class MZIComparison:
    name: str
    first: MZIResult
    second: MZIResult
    cross: StateComparison
    bar: StateComparison

    @property
    def p_pi_first(self) -> float | None:
        return self.first.fit.x_pi if fit_trusted(self.first.fit) else None

    @property
    def p_pi_second(self) -> float | None:
        return self.second.fit.x_pi if fit_trusted(self.second.fit) else None


def _phase_between(first: MZIResult, second: MZIResult, first_v, second_v) -> float | None:
    fit = next((item.fit for item in (first, second) if fit_trusted(item.fit)), None)
    if fit is None or first_v is None or second_v is None:
        return None
    delta = fit.phase(second_v ** 2) - fit.phase(first_v ** 2)
    return (delta + math.pi) % (2 * math.pi) - math.pi


def compare_runs(first: CalibrationRun, second: CalibrationRun) -> list[MZIComparison]:
    seconds = {item.name: item for item in second.mzis}
    rows = []
    for item in first.mzis:
        other = seconds.get(item.name)
        if other is None:
            continue
        rows.append(MZIComparison(
            item.name, item, other,
            StateComparison(item.cross_v, other.cross_v, _phase_between(item, other, item.cross_v, other.cross_v)),
            StateComparison(item.bar_v, other.bar_v, _phase_between(item, other, item.bar_v, other.bar_v))))
    if not rows:
        raise CalibrationAnalysisError("The two calibrations share no MZI")
    return rows


def load_run(path: Path | str) -> CalibrationRun:
    files = find_calibration_files(path)
    if len(files) != 1:
        raise CalibrationAnalysisError(f"Expected one mzi_calibration.json under {path}, found {len(files)}")
    return load_calibration(files[0])


def _fmt(value, digits=2) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def table_rows(rows: Sequence[MZIComparison], step_v: float) -> list[dict[str, str]]:
    path = lambda item: f"In {item.input_port} → Out {item.output_port}"
    return [{
        "mzi": row.name, "first_path": path(row.first), "second_path": path(row.second),
        "cross_first_v": _fmt(row.cross.first_v), "cross_second_v": _fmt(row.cross.second_v),
        "cross_delta_v": _fmt(row.cross.delta_v), "cross_phase_rad": _fmt(row.cross.phase_rad),
        "cross_leakage_db": _fmt(row.cross.leakage_db, 1), "cross_verdict": row.cross.verdict(step_v),
        "bar_first_v": _fmt(row.bar.first_v), "bar_second_v": _fmt(row.bar.second_v),
        "bar_delta_v": _fmt(row.bar.delta_v), "bar_phase_rad": _fmt(row.bar.phase_rad),
        "bar_leakage_db": _fmt(row.bar.leakage_db, 1), "bar_verdict": row.bar.verdict(step_v),
        "p_pi_first_v2": _fmt(row.p_pi_first), "p_pi_second_v2": _fmt(row.p_pi_second),
    } for row in rows]


def write_comparison(path: Path, first: CalibrationRun, second: CalibrationRun,
                     rows: Sequence[dict[str, str]]) -> Path:
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write(f"# First calibration: {first.folder}\n# Second calibration: {second.folder}\n")
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two saved MZI calibrations.")
    parser.add_argument("first", type=Path, help="Folder of the first (primary) calibration")
    parser.add_argument("second", type=Path, help="Folder of the second (alternative) calibration")
    args = parser.parse_args(argv)
    try:
        first, second = load_run(args.first), load_run(args.second)
        step_v = max(first.step_v, second.step_v)
        rows = table_rows(compare_runs(first, second), step_v)
    except CalibrationAnalysisError as error:
        parser.error(str(error))
    for row in rows:
        print(f"{row['mzi']:<5} {row['first_path']:>14} vs {row['second_path']:<14} "
              f"cross {row['cross_first_v']:>5} / {row['cross_second_v']:>5} V {row['cross_verdict']:<20} "
              f"bar {row['bar_first_v']:>5} / {row['bar_second_v']:>5} V {row['bar_verdict']:<20} "
              f"Ppi {row['p_pi_first_v2']:>6} / {row['p_pi_second_v2']:>6} V^2".replace("→", "->"))
    print(f"Saved {write_comparison(second.folder / 'calibration_comparison.csv', first, second, rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
