"""Re-extract cross/bar voltages from a saved MZI calibration sweep.

Reads ``mzi_calibration.json`` and the ``mzi_sweeps.csv`` beside it and runs
the current extraction (``extract_calibration``) on each sweep again, so a
change to how extremes are picked needs no new measurement. The sweeps are
fitted against V², which places extremes as heater power does when the
heater resistance is constant.

Run with::

    python -m benes.measurements.recalibrate Results/<run>/calibration/<time>_complete
    python -m benes.measurements.recalibrate <folder> --apply

``--apply`` writes the new voltages into ``benes_voltage_config.json`` after
copying it to ``benes_voltage_config.json.<time>.bak``.
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from benes.measurements.calibration_analysis import CalibrationAnalysisError, find_calibration_files, load_calibration
from benes.measurements.mzi_fit import SweepPoint, extract_calibration, fit_trusted
from benes.measurements.mzi_probing import Probe


@dataclass(frozen=True)
class Recalibration:
    name: str
    cross_before: float | None
    bar_before: float | None
    cross: float | None
    bar: float | None
    # "fit" when the fit placed the extremes, "raw" when the points did; else why none.
    method: str


def recalibrate(json_path: Path | str) -> list[Recalibration]:
    run = load_calibration(json_path)
    results = []
    for item in run.mzis:
        probe = Probe(item.name, item.input_port - 1, item.output_port - 1, item.bright_state == "cross", (), 0)
        points = [SweepPoint(voltage, power) for voltage, power in zip(item.voltages, item.powers_mw)]
        try:
            sweep = extract_calibration(probe, points, [voltage * voltage for voltage in item.voltages],
                                        x_unit="V²")
        except ValueError as error:
            results.append(Recalibration(item.name, item.cross_v, item.bar_v, None, None, str(error)))
            continue
        results.append(Recalibration(item.name, item.cross_v, item.bar_v, sweep.cross.voltage, sweep.bar.voltage,
                                     "fit" if fit_trusted(sweep.fit) else "raw"))
    return results


def apply(results: Sequence[Recalibration], config_path: Path) -> Path:
    """Write the new voltages into the voltage config; returns the backup's path."""
    from benes.core.network import BenesNetwork
    from benes.hardware.voltage_controller import BenesVoltageController

    backup = config_path.with_name(f"{config_path.name}.{datetime.now():%Y%m%d_%H%M%S}.bak")
    shutil.copy2(config_path, backup)
    controller = BenesVoltageController(BenesNetwork().switches, config_path)
    if controller.load_error:
        raise OSError(controller.load_error)
    for result in results:
        if result.cross is not None:
            controller.set_calibration(result.name, result.bar, result.cross)
    return backup


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Re-extract cross/bar voltages from saved MZI calibration sweeps.")
    parser.add_argument("path", type=Path, help="Folder holding mzi_calibration.json, or the file itself")
    parser.add_argument("--apply", action="store_true",
                        help="Write the new voltages into the voltage config (a backup is kept)")
    parser.add_argument("--config", type=Path, default=Path("benes_voltage_config.json"),
                        help="Voltage config to update with --apply")
    return parser


def _volts(value):
    return "  -  " if value is None else f"{value:5.2f}"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    files = find_calibration_files(args.path)
    if len(files) != 1:
        raise SystemExit(f"Expected one mzi_calibration.json under {args.path}, found {len(files)}")
    try:
        results = recalibrate(files[0])
    except (OSError, CalibrationAnalysisError) as error:
        raise SystemExit(f"Re-extraction failed: {error}") from error
    print(f"{files[0].parent.name}: cross / bar [V], saved -> re-extracted")
    for result in results:
        changed = "" if (result.cross, result.bar) == (result.cross_before, result.bar_before) else "  *"
        print(f"  {result.name:5} cross {_volts(result.cross_before)} -> {_volts(result.cross)}   "
              f"bar {_volts(result.bar_before)} -> {_volts(result.bar)}   {result.method}{changed}")
    if args.apply:
        backup = apply(results, args.config)
        print(f"Updated {args.config}; the previous version is {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
