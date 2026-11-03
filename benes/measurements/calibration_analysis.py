"""Per-MZI figures of merit from a saved MZI calibration sweep.

Reads ``mzi_calibration.json`` and the ``mzi_sweeps.csv`` beside it, as written
by the MZI calibration tab, and reports for every probed MZI the highest and
lowest detected power, the extinction, the heater π drive and the phase error
at the saved cross/bar voltages.

The sweeps are refitted against V² rather than heater power, since the heater
resistances are not saved with the run; the saved heater-power fit then gives
each heater's effective resistance V²/P.

Run with::

    python -m benes.measurements.calibration_analysis Datasets/Calibration1

Every ``mzi_calibration.json`` below the path is analyzed; the default output
directory is ``<sweep folder>/Analysis``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from benes.measurements.mzi_fit import FIT_TRUST_FRACTION, MZIFit, fit_mzi_response


CROSS_COLOR = "#2a78d6"
BAR_COLOR = "#eb6834"
DATA_COLOR = "#52514e"
FLAG_COLOR = "#e34948"
LOW_EXTINCTION_DB = 20.0
PHASE_TOLERANCE_DEG = 10.0
LEAKAGE_FLOOR_DB = -60.0


class CalibrationAnalysisError(ValueError):
    """Raised when a calibration folder cannot be read."""


def _wrap(angle: float) -> float:
    """Angle in (-π, π]."""
    wrapped = math.fmod(angle + math.pi, 2 * math.pi)
    return (wrapped + 2 * math.pi if wrapped <= 0 else wrapped) - math.pi


def _dbm(power_mw: float) -> float:
    return 10 * math.log10(power_mw) if power_mw > 0 else -math.inf


def leakage_db(phase_error: float) -> float:
    """Power left in the unwanted port by a phase error, for ideal 50:50 couplers."""
    leak = math.sin(phase_error / 2) ** 2
    return max(10 * math.log10(leak), LEAKAGE_FLOOR_DB) if leak > 0 else LEAKAGE_FLOOR_DB


@dataclass(frozen=True)
class MZIResult:
    name: str
    input_port: int
    output_port: int
    bright_state: str
    voltages: tuple[float, ...]
    powers_mw: tuple[float, ...]
    cross_v: float | None
    bar_v: float | None
    cross_before_v: float | None
    bar_before_v: float | None
    fit: MZIFit | None  # against V²
    p_pi_mw: float | None  # from the saved heater-power fit

    @property
    def _max_index(self) -> int:
        return max(range(len(self.powers_mw)), key=self.powers_mw.__getitem__)

    @property
    def _min_index(self) -> int:
        return min(range(len(self.powers_mw)), key=self.powers_mw.__getitem__)

    @property
    def max_v(self) -> float:
        return self.voltages[self._max_index]

    @property
    def max_mw(self) -> float:
        return self.powers_mw[self._max_index]

    @property
    def min_v(self) -> float:
        return self.voltages[self._min_index]

    @property
    def min_mw(self) -> float:
        return self.powers_mw[self._min_index]

    @property
    def max_dbm(self) -> float:
        return _dbm(self.max_mw)

    @property
    def min_dbm(self) -> float:
        return _dbm(self.min_mw)

    @property
    def extinction_db(self) -> float:
        return self.max_dbm - self.min_dbm

    @property
    def fit_quality(self) -> float | None:
        """Fit rms as a fraction of the fringe amplitude."""
        return None if self.fit is None or self.fit.amplitude <= 0 else self.fit.rms / self.fit.amplitude

    @property
    def v_pi(self) -> float | None:
        return None if self.fit is None else math.sqrt(self.fit.x_pi)

    @property
    def r_eff_ohm(self) -> float | None:
        """V²/P of the heater: R_total²/R_heater, the resistance seen by the board."""
        if self.fit is None or not self.p_pi_mw:
            return None
        return self.fit.x_pi / self.p_pi_mw * 1000

    def cross_phase(self, voltage: float) -> float:
        """Fitted phase that is 0 in cross and π in bar, whichever state the probe sees bright."""
        return self.fit.phase(voltage * voltage) + (math.pi if self.bright_state == "bar" else 0.0)

    def phase_error_deg(self, voltage: float | None, state: str) -> float | None:
        if self.fit is None or voltage is None:
            return None
        target = 0.0 if state == "cross" else math.pi
        return math.degrees(_wrap(self.cross_phase(voltage) - target))

    def exact_voltage(self, state: str) -> float | None:
        """Lowest voltage at which the fitted phase reaches ``state`` exactly."""
        if self.fit is None:
            return None
        target = 0.0 if state == "cross" else math.pi
        start = self.cross_phase(0.0)
        turns = math.ceil((start - target) / (2 * math.pi))
        x = (target + 2 * math.pi * turns - start) / self.fit.phase_per_x
        return math.sqrt(max(x, 0.0))

    def phase_step_deg(self, voltage: float, step_v: float) -> float | None:
        """Phase change for one voltage step at ``voltage``."""
        if self.fit is None:
            return None
        return math.degrees(self.fit.phase_per_x * 2 * voltage * step_v)

    def flags(self, step_v: float) -> list[str]:
        notes = []
        if self.extinction_db < LOW_EXTINCTION_DB:
            notes.append(f"only {self.extinction_db:.1f} dB extinction at In {self.input_port} → "
                         f"Out {self.output_port}; compare other MZIs probed on that path")
        quality = self.fit_quality
        if quality is None:
            notes.append("no cos² fit")
            return notes
        if quality > FIT_TRUST_FRACTION:
            notes.append(f"fit rms is {100 * quality:.1f} % of the fringe, so the calibration "
                         f"took the raw extremes")
        for state, voltage in (("cross", self.cross_v), ("bar", self.bar_v)):
            exact = self.exact_voltage(state)
            if voltage is None:
                continue
            if exact is not None and voltage ** 2 > exact ** 2 + self.fit.x_pi:
                notes.append(f"{state} at {voltage:g} V is a later fringe; the first {state} "
                             f"is at {exact:.2f} V")
            error = self.phase_error_deg(voltage, state)
            if abs(error) > PHASE_TOLERANCE_DEG + self.phase_step_deg(voltage, step_v) / 2:
                notes.append(f"{state} at {voltage:g} V is {error:+.0f}° off "
                             f"(≈ {leakage_db(math.radians(error)):.0f} dB leak); "
                             f"exact {state} at {exact:.2f} V")
        return notes


@dataclass(frozen=True)
class CalibrationRun:
    folder: Path
    started_at: str
    meter: str
    step_v: float
    mzis: tuple[MZIResult, ...]


def find_calibration_files(root: Path | str) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root]
    return sorted(root.rglob("mzi_calibration.json"))


def _read_sweeps(path: Path, unit: str) -> dict[str, list[tuple[float, float]]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    column = f"power_{unit}"
    sweeps = defaultdict(list)
    for row in csv.DictReader(line for line in lines if line.strip() and not line.startswith("#")):
        if row.get("sweep", "calibration") != "calibration":
            continue
        power = row.get(column) or ""
        dbm = row.get("power_dbm") or ""
        if unit == "mW" and power:
            value = float(power)
        elif dbm:
            value = 10 ** (float(dbm) / 10)
        else:
            continue  # a missed reading
        sweeps[row["mzi"]].append((float(row["voltage_v"]), value))
    return sweeps


def load_calibration(json_path: Path | str) -> CalibrationRun:
    json_path = Path(json_path)
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CalibrationAnalysisError(f"{json_path}: {error}") from error
    sweeps_path = json_path.with_name("mzi_sweeps.csv")
    if not sweeps_path.exists():
        raise CalibrationAnalysisError(f"{sweeps_path} is missing")
    sweeps = _read_sweeps(sweeps_path, meta.get("unit", "mW"))
    before = meta.get("calibration_before", {})
    voltages = meta.get("voltages_v") or []
    step_v = min((b - a for a, b in zip(voltages, voltages[1:]) if b > a), default=0.1)

    results = []
    for name, record in meta.get("mzi_calibrations", {}).items():
        points = sorted(sweeps.get(name, []))
        if len(points) < 4:
            continue
        xs, powers = [v * v for v, _ in points], [p for _, p in points]
        saved_fit = record.get("fit") or {}
        results.append(MZIResult(
            name=name,
            input_port=int(record["input"]),
            output_port=int(record["output"]),
            bright_state=record.get("bright_state", "cross"),
            voltages=tuple(v for v, _ in points),
            powers_mw=tuple(powers),
            cross_v=record.get("cross_voltage_v"),
            bar_v=record.get("bar_voltage_v"),
            cross_before_v=before.get(name, {}).get("cross"),
            bar_before_v=before.get(name, {}).get("straight"),
            fit=fit_mzi_response(xs, powers, "V²"),
            p_pi_mw=saved_fit.get("x_pi") if saved_fit.get("x_unit") == "mW" else None,
        ))
    if not results:
        raise CalibrationAnalysisError(f"{json_path} has no MZI with sweep data")
    results.sort(key=lambda item: [int(part) for part in item.name.lstrip("S").split(".")])
    return CalibrationRun(json_path.parent, meta.get("started_at", ""), meta.get("meter", ""),
                          step_v, tuple(results))


def _fmt(value, digits=2) -> str:
    return "" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def summary_rows(run: CalibrationRun) -> list[dict[str, str]]:
    rows = []
    for mzi in run.mzis:
        cross_error = mzi.phase_error_deg(mzi.cross_v, "cross")
        bar_error = mzi.phase_error_deg(mzi.bar_v, "bar")
        rows.append({
            "mzi": mzi.name,
            "input": str(mzi.input_port),
            "output": str(mzi.output_port),
            "bright_state": mzi.bright_state,
            "max_dbm": _fmt(mzi.max_dbm),
            "max_mw": _fmt(mzi.max_mw, 6),
            "max_at_v": _fmt(mzi.max_v, 2),
            "min_dbm": _fmt(mzi.min_dbm),
            "min_mw": _fmt(mzi.min_mw, 8),
            "min_at_v": _fmt(mzi.min_v, 2),
            "extinction_db": _fmt(mzi.extinction_db),
            "v_pi_v": _fmt(mzi.v_pi, 3),
            "v_pi_squared_v2": _fmt(mzi.fit.x_pi if mzi.fit else None, 3),
            "p_pi_mw": _fmt(mzi.p_pi_mw),
            "r_eff_ohm": _fmt(mzi.r_eff_ohm, 0),
            "phase_at_0v_deg": _fmt(mzi.phase_error_deg(0.0, "cross"), 1),
            "fit_rms_fraction": _fmt(mzi.fit_quality, 3),
            "cross_v": _fmt(mzi.cross_v, 2),
            "cross_phase_error_deg": _fmt(cross_error, 1),
            "cross_leak_db": _fmt(None if cross_error is None else leakage_db(math.radians(cross_error)), 1),
            "cross_exact_v": _fmt(mzi.exact_voltage("cross"), 3),
            "bar_v": _fmt(mzi.bar_v, 2),
            "bar_phase_error_deg": _fmt(bar_error, 1),
            "bar_leak_db": _fmt(None if bar_error is None else leakage_db(math.radians(bar_error)), 1),
            "bar_exact_v": _fmt(mzi.exact_voltage("bar"), 3),
            "cross_before_v": _fmt(mzi.cross_before_v, 2),
            "bar_before_v": _fmt(mzi.bar_before_v, 2),
            "flags": "; ".join(mzi.flags(run.step_v)),
        })
    return rows


def _write_markdown(path: Path, run: CalibrationRun, rows: Sequence[dict[str, str]]) -> None:
    lines = [
        f"# MZI calibration analysis: {run.folder.name}",
        "",
        f"Sweep started {run.started_at} with {run.meter or 'the detector'}, {run.step_v:g} V steps. "
        "Powers are at the detector, so they include laser power and path loss; "
        "extinction is the max/min ratio over the whole sweep.",
        "",
        "| MZI | Probe | Bright | Max dBm (V) | Min dBm (V) | Extinction dB | Vπ V | Pπ mW | "
        "Phase at 0 V | Cross V (err) | Bar V (err) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['mzi']} | In {row['input']} → Out {row['output']} | {row['bright_state']} | "
            f"{row['max_dbm']} ({row['max_at_v']}) | {row['min_dbm']} ({row['min_at_v']}) | "
            f"{row['extinction_db']} | {row['v_pi_v']} | {row['p_pi_mw']} | {row['phase_at_0v_deg']}° | "
            f"{row['cross_v']} ({row['cross_phase_error_deg']}°) | {row['bar_v']} ({row['bar_phase_error_deg']}°) |")
    flagged = [(row["mzi"], row["flags"]) for row in rows if row["flags"]]
    lines += ["", "Phase errors are from the cos² fit against V²; leaks assume ideal 50:50 couplers.", ""]
    if flagged:
        lines += ["## Flags", ""] + [f"- **{name}**: {flags}" for name, flags in flagged]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mzi_axis(ax, run: CalibrationRun):
    ax.set_xticks(range(len(run.mzis)), [mzi.name for mzi in run.mzis], rotation=45)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)


def render_sweeps(path: Path, run: CalibrationRun) -> None:
    """One panel per MZI: measured sweep, fit, extremes and saved setpoints."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    columns = 4
    rows = math.ceil(len(run.mzis) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(4.2 * columns, 3.1 * rows), squeeze=False,
                             constrained_layout=True)
    for ax, mzi in zip(axes.flat, run.mzis):
        ax.plot(mzi.voltages, [_dbm(p) for p in mzi.powers_mw], "o", color=DATA_COLOR, markersize=3,
                label="Measured")
        bottom = mzi.min_dbm - 6
        if mzi.fit is not None:
            dense = [i * mzi.voltages[-1] / 400 for i in range(401)]
            model = [mzi.fit.model(v * v) for v in dense]
            ax.plot(dense, [max(_dbm(p), bottom) if p > 0 else bottom for p in model],
                    color=DATA_COLOR, linewidth=1, alpha=0.6, label="cos² fit")
        for voltage, color, label in ((mzi.cross_v, CROSS_COLOR, "Cross"), (mzi.bar_v, BAR_COLOR, "Bar")):
            if voltage is not None:
                ax.axvline(voltage, color=color, linewidth=1.5, label=f"{label} {voltage:g} V")
        ax.plot([mzi.max_v], [mzi.max_dbm], "^", color="black", markersize=7)
        ax.plot([mzi.min_v], [mzi.min_dbm], "v", color="black", markersize=7)
        ax.set_ylim(bottom, mzi.max_dbm + 3)
        ax.set_title(f"{mzi.name}  In {mzi.input_port} → Out {mzi.output_port}  "
                     f"{mzi.extinction_db:.1f} dB", fontsize=10, loc="left")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7, loc="lower left")
    for ax in axes.flat[len(run.mzis):]:
        ax.set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("Voltage [V]")
    for ax in axes[:, 0]:
        ax.set_ylabel("Detected power [dBm]")
    fig.suptitle(f"MZI calibration sweeps ({run.folder.name}); ▲ max, ▼ min", fontweight="bold")
    fig.savefig(path, dpi=160)
    plt.close(fig)


def render_metrics(path: Path, run: CalibrationRun) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mzis = run.mzis
    positions = range(len(mzis))
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5), constrained_layout=True)

    ax = axes[0, 0]
    for i, mzi in enumerate(mzis):
        ax.plot([i, i], [mzi.min_dbm, mzi.max_dbm], color=DATA_COLOR, linewidth=2)
        ax.text(i + 0.12, (mzi.min_dbm + mzi.max_dbm) / 2, f"{mzi.extinction_db:.0f}", fontsize=8,
                color=FLAG_COLOR if mzi.extinction_db < LOW_EXTINCTION_DB else "black")
    ax.plot(positions, [m.max_dbm for m in mzis], "^", color="black", markersize=8, label="Max")
    ax.plot(positions, [m.min_dbm for m in mzis], "v", color="black", markersize=8, label="Min")
    ax.set_ylabel("Detected power [dBm]")
    ax.set_title("Max and min detected power; extinction [dB] beside each", loc="left", fontweight="bold")
    ax.legend(fontsize=8)
    _mzi_axis(ax, run)

    ax = axes[0, 1]
    if all(m.p_pi_mw for m in mzis):
        ax.bar(positions, [m.p_pi_mw for m in mzis], color=DATA_COLOR, width=0.6)
        for i, mzi in enumerate(mzis):
            ax.text(i, mzi.p_pi_mw, f"{mzi.v_pi:.2f} V", ha="center", va="bottom", fontsize=8)
        ax.set_ylabel("Pπ [mW]")
        ax.set_title("Heater power for π (Vπ above each bar)", loc="left", fontweight="bold")
    else:
        ax.bar(positions, [m.v_pi or 0 for m in mzis], color=DATA_COLOR, width=0.6)
        ax.set_ylabel("Vπ [V]")
        ax.set_title("Voltage for π", loc="left", fontweight="bold")
    _mzi_axis(ax, run)

    ax = axes[1, 0]
    errors = [m.phase_error_deg(0.0, "cross") or 0.0 for m in mzis]
    ax.bar(positions, errors, color=[FLAG_COLOR if abs(e) > PHASE_TOLERANCE_DEG else DATA_COLOR for e in errors],
           width=0.6)
    for i, error in enumerate(errors):
        ax.text(i, error, f"{leakage_db(math.radians(error)):.0f} dB", ha="center",
                va="bottom" if error >= 0 else "top", fontsize=8)
    ax.axhline(0, color="black", linewidth=0.8)
    for limit in (-PHASE_TOLERANCE_DEG, PHASE_TOLERANCE_DEG):
        ax.axhline(limit, color=FLAG_COLOR, linewidth=0.8, linestyle="--")
    ax.set_ylabel("Phase from cross at 0 V [°]")
    ax.set_title("Unbiased phase error; estimated leak if 0 V is used as cross", loc="left", fontweight="bold")
    _mzi_axis(ax, run)

    ax = axes[1, 1]
    width = 0.38
    for offset, state, color in ((-width / 2, "cross", CROSS_COLOR), (width / 2, "bar", BAR_COLOR)):
        leaks = []
        for mzi in mzis:
            error = mzi.phase_error_deg(getattr(mzi, f"{state}_v"), state)
            leaks.append(LEAKAGE_FLOOR_DB if error is None else leakage_db(math.radians(error)))
        ax.bar([i + offset for i in positions], [leak - LEAKAGE_FLOOR_DB for leak in leaks],
               bottom=LEAKAGE_FLOOR_DB, width=width, color=color, label=f"Saved {state}")
    ax.set_ylim(LEAKAGE_FLOOR_DB, 0)
    ax.set_ylabel("Estimated leak to the other port [dB]")
    ax.set_title(f"Phase error at the saved setpoints (±{run.step_v:g} V grid)", loc="left", fontweight="bold")
    ax.legend(fontsize=8)
    _mzi_axis(ax, run)

    fig.savefig(path, dpi=160)
    plt.close(fig)


def render_universal(path: Path, run: CalibrationRun) -> None:
    """All sweeps against their own fitted phase; a perfect MZI follows the cos² line."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5.5), constrained_layout=True)
    top = 0.0
    for mzi in run.mzis:
        if mzi.fit is None:
            continue
        phases = [mzi.fit.phase(v * v) / math.pi for v in mzi.voltages]
        normalized = [_dbm(p / mzi.max_mw) for p in mzi.powers_mw]
        low = mzi.extinction_db < LOW_EXTINCTION_DB
        ax.plot(phases, normalized, color=FLAG_COLOR if low else DATA_COLOR, linewidth=1,
                alpha=0.9 if low else 0.45)
        if low:
            ax.text(phases[-1], normalized[-1], f" {mzi.name}", color=FLAG_COLOR, fontsize=8, va="center")
        top = max(top, phases[-1])
    ideal = [i * top / 1000 for i in range(1001)]
    ax.plot(ideal, [max(_dbm((1 + math.cos(math.pi * p)) / 2), -80) for p in ideal], color=CROSS_COLOR,
            linewidth=2, label="Ideal cos²")
    ax.plot([], [], color=DATA_COLOR, label="Measured MZI")
    ax.plot([], [], color=FLAG_COLOR, label=f"Extinction < {LOW_EXTINCTION_DB:g} dB")
    lowest = min(m.min_dbm - m.max_dbm for m in run.mzis)
    ax.set_ylim(lowest - 5, 2)
    ax.set_xlabel("Fitted phase of the probed port φ/π (bright at even values)")
    ax.set_ylabel("Power relative to the MZI's max [dB]")
    ax.set_title("All sweeps on a common phase axis: null depth is each MZI's extinction limit",
                 loc="left", fontweight="bold")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="lower left")
    fig.savefig(path, dpi=160)
    plt.close(fig)


def render_paths(path: Path, run: CalibrationRun) -> None:
    """Peak power per probed input/output pair, and setpoint drift since the last calibration."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, (ax_map, ax_drift) = plt.subplots(1, 2, figsize=(13, 5.5), constrained_layout=True)
    peaks = np.full((6, 6), np.nan)
    names = defaultdict(list)
    for mzi in run.mzis:
        row, column = mzi.input_port - 1, mzi.output_port - 1
        peaks[row, column] = np.nanmax([peaks[row, column], mzi.max_dbm])
        names[row, column].append(mzi.name)
    image = ax_map.imshow(peaks, origin="upper", cmap="Blues", aspect="equal")
    for (row, column), labels in names.items():
        light = peaks[row, column] > np.nanmean(peaks)
        ax_map.text(column, row, f"{peaks[row, column]:.1f}\n" + "\n".join(labels), ha="center", va="center",
                    fontsize=7, color="white" if light else "black")
    ax_map.set_xticks(range(6), [f"Out {i}" for i in range(1, 7)])
    ax_map.set_yticks(range(6), [f"In {i}" for i in range(1, 7)])
    ax_map.set_title("Peak detected power per probe path [dBm]", loc="left", fontweight="bold")
    fig.colorbar(image, ax=ax_map, shrink=0.8, label="dBm")

    for offset, state, color in ((-0.15, "cross", CROSS_COLOR), (0.15, "bar", BAR_COLOR)):
        for i, mzi in enumerate(run.mzis):
            before, after = getattr(mzi, f"{state}_before_v"), getattr(mzi, f"{state}_v")
            if before is not None and after is not None:
                ax_drift.plot([i + offset] * 2, [before, after], color=color, linewidth=1.5)
            if before is not None:
                ax_drift.plot(i + offset, before, "o", markerfacecolor="white", markeredgecolor=color,
                              markersize=7)
            if after is not None:
                ax_drift.plot(i + offset, after, "o", color=color, markersize=7)
        ax_drift.plot([], [], "o", color=color, label=f"{state.capitalize()} now")
    ax_drift.plot([], [], "o", markerfacecolor="white", markeredgecolor=DATA_COLOR, label="Before this run")
    ax_drift.set_ylabel("Setpoint [V]")
    ax_drift.set_title("Saved setpoints before and after this calibration", loc="left", fontweight="bold")
    ax_drift.legend(fontsize=8)
    _mzi_axis(ax_drift, run)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_analysis_outputs(run: CalibrationRun, output_dir: Path | str) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows = summary_rows(run)
    paths = {
        "summary_csv": output / "calibration_summary.csv",
        "summary_markdown": output / "calibration_summary.md",
        "sweeps": output / "calibration_sweeps.png",
        "metrics": output / "calibration_metrics.png",
        "universal": output / "calibration_universal.png",
        "paths": output / "calibration_paths.png",
    }
    with paths["summary_csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _write_markdown(paths["summary_markdown"], run, rows)
    render_sweeps(paths["sweeps"], run)
    render_metrics(paths["metrics"], run)
    render_universal(paths["universal"], run)
    render_paths(paths["paths"], run)
    return paths


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize saved MZI calibration sweeps.")
    parser.add_argument("path", type=Path, help="Folder searched for mzi_calibration.json, or the file itself")
    parser.add_argument("--output", type=Path,
                        help="Output directory (default: <sweep folder>/Analysis)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    files = find_calibration_files(args.path)
    if not files:
        raise SystemExit(f"Analysis failed: no mzi_calibration.json under {args.path}")
    for json_path in files:
        if args.output is None:
            output = json_path.parent / "Analysis"
        else:
            output = args.output / json_path.parent.name if len(files) > 1 else args.output
        try:
            run = load_calibration(json_path)
            paths = write_analysis_outputs(run, output)
        except (OSError, CalibrationAnalysisError) as error:
            raise SystemExit(f"Analysis failed: {error}") from error
        print(f"{json_path.parent.name}: {len(run.mzis)} MZIs")
        for row in summary_rows(run):
            print(f"  {row['mzi']:5} max {row['max_dbm']:>7} dBm @ {row['max_at_v']} V  "
                  f"min {row['min_dbm']:>7} dBm @ {row['min_at_v']} V  {row['extinction_db']:>5} dB")
        print(f"  Summary: {paths['summary_markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
