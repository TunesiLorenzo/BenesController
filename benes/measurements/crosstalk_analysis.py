"""Compact analysis of a saved crosstalk-matrix run.

Reads ``crosstalk_matrix.json`` and ``crosstalk_sweeps.csv`` from a ``*_matrix``
folder written by the Crosstalk tab and condenses the per-victim curves into a
few overview figures and tables:

* one row per measured aggressor/victim pair with κ, the phase shift at full
  aggressor drive, the power-law exponent of that shift against aggressor
  voltage and the equivalent shared series resistance;
* every victim curve on one shared-axis grid;
* all curves normalised against aggressor voltage and heater power, to show
  which of the two the coupling follows;
* per-switch totals and the worst-case phase drift at the calibrated operating
  points, with the extinction it limits the switch to.

A heater shares a return or supply resistance R with its neighbours when the
victim's shift is linear in the aggressor *voltage* (current) and negative.
The victim then loses ΔP = -2·V_bias·R_h/R_tv²·R·I_a of heater power, which
gives R from the measured slope.

Run with::

    python -m benes.measurements.crosstalk_analysis Datasets/Calibration1/<run>_matrix

The default output directory is ``<run>_matrix/Analysis``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SAME_STAGE = "same stage"
ADJACENT_STAGE = "adjacent stage"
RELATION_COLORS = {SAME_STAGE: "#2a78d6", ADJACENT_STAGE: "#eb6834"}
CURVE_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
MUTED = "#6b6a63"
# Pairs whose curve explains less than this share of variance under a straight
# line are too weak for a shape fit: no exponent or resistance is reported.
MIN_SHAPE_R2 = 0.8


class CrosstalkAnalysisError(ValueError):
    """Raised when a folder does not hold a crosstalk-matrix run."""


@dataclass(frozen=True)
class PairAnalysis:
    aggressor: str
    victim: str
    relation: str
    kappa: float
    bias_v: float
    voltages_v: tuple[float, ...]
    aggressor_mw: tuple[float, ...]
    phase_rad: tuple[float, ...]
    slope_rad_per_v: float
    r2_voltage: float
    r2_power: float
    exponent: float | None
    shared_resistance_ohm: float | None

    @property
    def label(self) -> str:
        return f"{self.aggressor} → {self.victim}"

    @property
    def max_phase_rad(self) -> float:
        return self.phase_rad[-1]

    @property
    def shape_fitted(self) -> bool:
        return self.exponent is not None


@dataclass(frozen=True)
class SwitchAnalysis:
    name: str
    received_rad: float
    emitted_rad: float
    operating_drift_rad: float | None
    extinction_limit_db: float | None


def stage_of(name: str) -> int:
    return int(name[1:].split(".")[0])


def relation_of(aggressor: str, victim: str) -> str:
    return SAME_STAGE if stage_of(aggressor) == stage_of(victim) else ADJACENT_STAGE


def heater_power_mw(voltage, heater_ohm, total_ohm):
    return np.asarray(voltage, dtype=float) ** 2 * heater_ohm / total_ohm ** 2 * 1e3


def through_origin_r2(x, y) -> float:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if not y.any():
        return 0.0
    slope = (x @ y) / (x @ x)
    residual = y - slope * x
    return float(1.0 - (residual @ residual) / (y @ y))


def power_law_exponent(x, y) -> float:
    """Least-squares n of y = a·x^n through the origin, by grid search over n."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    best = (math.inf, 1.0)
    for n in np.linspace(0.2, 3.0, 281):
        basis = x ** n
        a = (basis @ y) / (basis @ basis)
        residual = y - a * basis
        best = min(best, (float(residual @ residual), float(n)))
    return best[1]


def load_sweeps(path: Path) -> dict[tuple[str, str, int, int], list[tuple[float, float]]]:
    """Aggressor sweeps as (voltage, victim phase) keyed by (aggressor, victim, input, output)."""
    with path.open(encoding="utf-8") as handle:
        rows = csv.DictReader(line for line in handle if not line.startswith("#"))
        sweeps = defaultdict(list)
        for row in rows:
            if row["swept"] == row["victim"] or not row["victim_phase_shift_rad"]:
                continue
            key = (row["swept"], row["victim"], int(row["input"]), int(row["output"]))
            sweeps[key].append((float(row["voltage_v"]), float(row["victim_phase_shift_rad"])))
    return {key: sorted(points) for key, points in sweeps.items()}


def analyze_pairs(run: dict, sweeps: dict) -> list[PairAnalysis]:
    heater = run["heater_resistance_ohm"]
    total = run["total_resistance_ohm"]
    fits = {(item["mzi"], item["input"], item["output"]): item["fit"] for item in run["victims"]}
    pairs = []
    for pair in run["pairs"]:
        aggressor, victim = pair["aggressor"], pair["victim"]
        points = sweeps.get((aggressor, victim, pair["input"], pair["output"]))
        if not points or pair.get("kappa") is None:
            continue
        voltages = np.array([point[0] for point in points])
        phases = np.array([point[1] for point in points])
        power = heater_power_mw(voltages, heater[aggressor], total[aggressor])
        slope = float((voltages @ phases) / (voltages @ voltages))
        r2_voltage = through_origin_r2(voltages, phases)
        exponent = resistance = None
        if r2_voltage >= MIN_SHAPE_R2:
            exponent = power_law_exponent(voltages, phases)
            fit = fits.get((victim, pair["input"], pair["output"]))
            bias = pair["bias_voltage_v"]
            if fit and fit.get("x_unit") == "mW" and bias > 0:
                rad_per_ohm_volt = (math.pi / fit["x_pi"] * 2 * bias * heater[victim]
                                    / total[victim] ** 2 / total[aggressor] * 1e3)
                resistance = -slope / rad_per_ohm_volt
        pairs.append(PairAnalysis(
            aggressor=aggressor,
            victim=victim,
            relation=relation_of(aggressor, victim),
            kappa=pair["kappa"],
            bias_v=pair["bias_voltage_v"],
            voltages_v=tuple(voltages),
            aggressor_mw=tuple(power),
            phase_rad=tuple(phases),
            slope_rad_per_v=slope,
            r2_voltage=r2_voltage,
            r2_power=through_origin_r2(power, phases),
            exponent=exponent,
            shared_resistance_ohm=resistance,
        ))
    return pairs


def analyze_switches(run: dict, pairs: list[PairAnalysis]) -> list[SwitchAnalysis]:
    """Totals per switch and the worst-case drift at its calibrated operating points.

    The drift scales each measured slope from the victim's quadrature bias to its
    calibrated straight/cross voltage (the shift is proportional to the victim's
    own voltage for a shared resistance) and lets every aggressor toggle between
    its two calibrated voltages in the unfavourable direction.
    """
    calibration = run.get("calibration", {})
    names = sorted({name for pair in pairs for name in (pair.aggressor, pair.victim)} | set(calibration),
                   key=lambda name: (stage_of(name), name))
    switches = []
    for name in names:
        received = sum(abs(pair.max_phase_rad) for pair in pairs if pair.victim == name)
        emitted = sum(abs(pair.max_phase_rad) for pair in pairs if pair.aggressor == name)
        drift = extinction = None
        own = calibration.get(name)
        incoming = [pair for pair in pairs if pair.victim == name and pair.bias_v > 0]
        if own and incoming and all(pair.aggressor in calibration for pair in incoming):
            drift = 0.0
            for state in ("straight", "cross"):
                state_drift = 0.0
                for pair in incoming:
                    states = calibration[pair.aggressor]
                    swing = abs(states["straight"] - states["cross"])
                    state_drift += abs(pair.slope_rad_per_v) * own[state] / pair.bias_v * swing
                drift = max(drift, state_drift)
            leak = math.sin(drift / 2) ** 2
            extinction = -10 * math.log10(leak) if leak > 0 else None
        switches.append(SwitchAnalysis(name, received, emitted, drift, extinction))
    return switches


def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.25)
    ax.set_axisbelow(True)


def render_pair_summary(path: Path, pairs: list[PairAnalysis], max_voltage: float) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    ordered = sorted(pairs, key=lambda pair: abs(pair.kappa))
    rows = np.arange(len(ordered))
    colors = [RELATION_COLORS[pair.relation] for pair in ordered]
    fig, axes = plt.subplots(1, 4, figsize=(14, 0.32 * len(ordered) + 1.8), sharey=True,
                             constrained_layout=True)

    ax = axes[0]
    ax.barh(rows, [100 * pair.kappa for pair in ordered], color=colors, height=0.7)
    ax.set_yticks(rows, [pair.label for pair in ordered])
    ax.set_xlabel("κ [%]")
    ax.set_title("Crosstalk ratio κ", loc="left", fontsize=10)

    ax = axes[1]
    ax.barh(rows, [pair.max_phase_rad for pair in ordered], color=colors, height=0.7)
    ax.set_xlabel("Victim phase shift [rad]")
    ax.set_title(f"Shift at aggressor {max_voltage:g} V", loc="left", fontsize=10)

    ax = axes[2]
    for row, pair in zip(rows, ordered):
        if pair.shape_fitted:
            ax.plot(pair.exponent, row, "o", color=RELATION_COLORS[pair.relation], markersize=7)
    ax.axvline(1, color=MUTED, linestyle="--", linewidth=1)
    ax.axvline(2, color=MUTED, linestyle=":", linewidth=1)
    ax.text(1, len(ordered) - 0.3, " ∝ V", color=MUTED, fontsize=8, va="bottom")
    ax.text(2, len(ordered) - 0.3, " ∝ P", color=MUTED, fontsize=8, va="bottom")
    ax.set_xlim(0, 2.6)
    ax.set_xlabel("n in Δφ ∝ V_aggressor^n")
    ax.set_title("Curve shape", loc="left", fontsize=10)

    ax = axes[3]
    for row, pair in zip(rows, ordered):
        if pair.shared_resistance_ohm is not None:
            ax.plot(pair.shared_resistance_ohm, row, "o", color=RELATION_COLORS[pair.relation], markersize=7)
    resistances = [pair.shared_resistance_ohm for pair in pairs
                   if pair.shared_resistance_ohm is not None and pair.relation == SAME_STAGE]
    if resistances:
        ax.axvline(float(np.median(resistances)), color=MUTED, linestyle="--", linewidth=1)
    ax.set_xlim(left=min(0.0, *(pair.shared_resistance_ohm or 0 for pair in pairs)))
    ax.set_xlabel("Equivalent shared resistance [Ω]")
    ax.set_title("Shared-resistance model", loc="left", fontsize=10)

    for ax in axes:
        _style(ax)
        ax.axvline(0, color="#999999", linewidth=0.8)
    ax.set_ylim(-0.7, len(ordered) - 0.1)
    handles = [Line2D([], [], color=color, marker="s", linestyle="", markersize=8, label=relation)
               for relation, color in RELATION_COLORS.items()]
    fig.legend(handles=handles, loc="outside lower center", ncol=2, frameon=False,
               title=f"No marker: curve too weak for a shape fit (R² < {MIN_SHAPE_R2:g})",
               title_fontsize=8)
    fig.suptitle("Measured crosstalk pairs, sorted by |κ|", x=0.01, ha="left", fontweight="bold")
    fig.savefig(path, dpi=160)
    plt.close(fig)


def render_curve_grid(path: Path, pairs: list[PairAnalysis]) -> None:
    import matplotlib.pyplot as plt

    by_victim = defaultdict(list)
    for pair in pairs:
        by_victim[pair.victim].append(pair)
    stages = sorted({stage_of(name) for name in by_victim})
    columns = {stage: sorted(name for name in by_victim if stage_of(name) == stage) for stage in stages}
    rows = max(len(names) for names in columns.values())
    phases = [value for pair in pairs for value in pair.phase_rad]
    label_spacing = 0.07 * (max(phases) - min(phases))
    fig, axes = plt.subplots(rows, len(stages), figsize=(3.0 * len(stages), 2.3 * rows + 0.6),
                             sharex=True, sharey=True, squeeze=False, constrained_layout=True)
    for column, stage in enumerate(stages):
        for row in range(rows):
            ax = axes[row][column]
            if row >= len(columns[stage]):
                ax.set_visible(False)
                continue
            victim = columns[stage][row]
            ax.axhline(0, color="#999999", linewidth=0.8)
            curves = sorted(by_victim[victim], key=lambda item: item.aggressor)
            for color, pair in zip(CURVE_COLORS, curves):
                ax.plot(pair.voltages_v, pair.phase_rad, color=color, linewidth=2,
                        marker="o", markersize=3, label=pair.aggressor)
            # End labels, pushed apart so close curve ends stay readable.
            label_y = {}
            for pair in sorted(curves, key=lambda item: item.phase_rad[-1]):
                below = max(label_y.values(), default=-math.inf)
                label_y[pair.aggressor] = max(pair.phase_rad[-1], below + label_spacing)
            for pair in curves:
                ax.annotate(pair.aggressor, (pair.voltages_v[-1], label_y[pair.aggressor]),
                            xytext=(4, 0), textcoords="offset points", va="center", fontsize=8)
            ax.set_title(f"Victim {victim}", loc="left", fontsize=10)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(alpha=0.25)
            if column == 0:
                ax.set_ylabel("Δφ [rad]")
            if row == len(columns[stage]) - 1:
                ax.set_xlabel("Aggressor voltage [V]")
                ax.xaxis.set_tick_params(labelbottom=True)
    voltages = [value for pair in pairs for value in pair.voltages_v]
    axes[0][0].set_xlim(min(voltages), max(voltages) * 1.22)
    fig.suptitle("Victim phase shift against each aggressor (shared axes)", x=0.01, ha="left",
                 fontweight="bold")
    fig.savefig(path, dpi=160)
    plt.close(fig)


def render_drive_dependence(path: Path, pairs: list[PairAnalysis]) -> None:
    import matplotlib.pyplot as plt

    strong = [pair for pair in pairs if pair.shape_fitted]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.4), sharey=True, constrained_layout=True)
    for ax, values_of, label, r2_of in (
        (axes[0], lambda pair: pair.voltages_v, "Aggressor voltage / max", lambda pair: pair.r2_voltage),
        (axes[1], lambda pair: pair.aggressor_mw, "Aggressor heater power / max", lambda pair: pair.r2_power),
    ):
        ax.plot([0, 1], [0, 1], color=MUTED, linestyle="--", linewidth=1)
        for pair in strong:
            x = np.asarray(values_of(pair))
            y = np.asarray(pair.phase_rad)
            ax.plot(x / x[-1], y / y[-1], color=RELATION_COLORS[pair.relation], linewidth=1.5, alpha=0.6)
        median_r2 = float(np.median([r2_of(pair) for pair in strong])) if strong else math.nan
        ax.set_title(f"Linear fit R² median {median_r2:.3f}", loc="left", fontsize=10)
        ax.set_xlabel(label)
        ax.set_xlim(0, 1.02)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(alpha=0.25)
    axes[0].set_ylabel("Victim phase shift / shift at max")
    fig.suptitle(f"Does the coupling follow voltage or power? ({len(strong)} pairs, normalised)",
                 x=0.01, ha="left", fontweight="bold")
    fig.savefig(path, dpi=160)
    plt.close(fig)


def render_switch_summary(path: Path, switches: list[SwitchAnalysis], max_voltage: float) -> None:
    import matplotlib.pyplot as plt

    rows = np.arange(len(switches))[::-1]
    stage_colors = [CURVE_COLORS[0] if stage_of(item.name) % 2 else "#86b6ef" for item in switches]
    fig, axes = plt.subplots(1, 3, figsize=(12, 0.34 * len(switches) + 1.6), sharey=True,
                             constrained_layout=True)
    panels = (
        (axes[0], [item.received_rad for item in switches], "Σ|Δφ| [rad]",
         f"Received (all aggressors at {max_voltage:g} V)"),
        (axes[1], [item.emitted_rad for item in switches], "Σ|Δφ| [rad]",
         f"Caused in neighbours (at {max_voltage:g} V)"),
        (axes[2], [item.extinction_limit_db or 0 for item in switches], "Extinction limit [dB]",
         "Worst case at calibrated states (lower is worse)"),
    )
    for ax, values, xlabel, title in panels:
        ax.barh(rows, values, color=stage_colors, height=0.7)
        ax.set_xlabel(xlabel)
        ax.set_title(title, loc="left", fontsize=10)
        _style(ax)
    for row, item in zip(rows, switches):
        if item.extinction_limit_db is not None:
            axes[2].annotate(f"{item.extinction_limit_db:.1f} dB  ({item.operating_drift_rad:.2f} rad)",
                             (item.extinction_limit_db, row), xytext=(4, 0), textcoords="offset points",
                             va="center", fontsize=8)
        else:
            axes[2].annotate("no measured aggressor", (0, row), xytext=(4, 0), textcoords="offset points",
                             va="center", fontsize=8, color=MUTED)
    finite = [item.extinction_limit_db for item in switches if item.extinction_limit_db is not None]
    axes[2].set_xlim(0, 1.45 * max(finite, default=1))
    axes[0].set_yticks(rows, [item.name for item in switches])
    fig.suptitle("Crosstalk per switch (shading alternates by stage)", x=0.01, ha="left", fontweight="bold")
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _number(value, digits=4):
    return "" if value is None else f"{value:.{digits}g}"


def write_tables(output: Path, pairs: list[PairAnalysis], switches: list[SwitchAnalysis]) -> None:
    with (output / "crosstalk_pairs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["aggressor", "victim", "relation", "kappa", "victim_bias_v", "max_phase_rad",
                         "slope_rad_per_v", "r2_linear_voltage", "r2_linear_power", "exponent_in_voltage",
                         "shared_resistance_ohm"])
        for pair in pairs:
            writer.writerow([pair.aggressor, pair.victim, pair.relation, _number(pair.kappa), pair.bias_v,
                             _number(pair.max_phase_rad), _number(pair.slope_rad_per_v),
                             _number(pair.r2_voltage), _number(pair.r2_power), _number(pair.exponent, 3),
                             _number(pair.shared_resistance_ohm, 3)])
    with (output / "crosstalk_switches.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["switch", "received_rad", "emitted_rad", "operating_drift_rad", "extinction_limit_db"])
        for item in switches:
            writer.writerow([item.name, _number(item.received_rad), _number(item.emitted_rad),
                             _number(item.operating_drift_rad), _number(item.extinction_limit_db, 3)])


def write_summary(path: Path, folder: Path, pairs: list[PairAnalysis], switches: list[SwitchAnalysis]) -> None:
    strong = [pair for pair in pairs if pair.shape_fitted]
    same = [pair.shared_resistance_ohm for pair in strong
            if pair.relation == SAME_STAGE and pair.shared_resistance_ohm is not None]
    negative = sum(pair.kappa < 0 for pair in strong)
    lines = [
        f"# Crosstalk analysis — {folder.name}",
        "",
        f"- Measured pairs: {len(pairs)} ({sum(p.relation == SAME_STAGE for p in pairs)} same stage, "
        f"{sum(p.relation == ADJACENT_STAGE for p in pairs)} adjacent stage); "
        f"{len(strong)} strong enough for a shape fit (R² ≥ {MIN_SHAPE_R2:g}).",
    ]
    if strong:
        lines += [
            f"- Shape: median exponent n = {np.median([p.exponent for p in strong]):.2f} in Δφ ∝ V^n; "
            f"median R² linear in voltage {np.median([p.r2_voltage for p in strong]):.3f} "
            f"against {np.median([p.r2_power for p in strong]):.3f} linear in heater power.",
            f"- Sign: {negative} of {len(strong)} strong pairs have κ < 0 "
            "(the aggressor acts like *less* victim heating).",
        ]
    if same:
        lines.append(f"- Equivalent shared resistance, same-stage pairs: median {np.median(same):.1f} Ω "
                     f"(range {min(same):.1f}–{max(same):.1f} Ω).")
    lines += [
        "",
        "A shift that is negative and linear in aggressor voltage points to a shared return or supply",
        "resistance rather than heat, which would be positive and linear in power. With that coupling",
        "κ is not a constant: it scales with the victim's own bias voltage, so the κ matrix holds only",
        "at the bias each victim was measured at.",
        "",
        "| Switch | Worst-case drift [rad] | Extinction limit [dB] |",
        "|---|---|---|",
    ]
    for item in switches:
        if item.operating_drift_rad is not None:
            lines.append(f"| {item.name} | {item.operating_drift_rad:.3f} | {item.extinction_limit_db:.1f} |")
    lines += [
        "",
        "Worst case: every measured aggressor toggles between its calibrated straight and cross voltage",
        "while the victim sits at whichever of its calibrated voltages couples more.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def analyze_folder(folder: Path | str, output: Path | str | None = None) -> dict[str, Path]:
    import matplotlib
    matplotlib.use("Agg")

    folder = Path(folder)
    matrix_path = folder / "crosstalk_matrix.json"
    sweeps_path = folder / "crosstalk_sweeps.csv"
    if not matrix_path.is_file() or not sweeps_path.is_file():
        raise CrosstalkAnalysisError(f"{folder} has no crosstalk_matrix.json and crosstalk_sweeps.csv")
    run = json.loads(matrix_path.read_text(encoding="utf-8"))
    pairs = analyze_pairs(run, load_sweeps(sweeps_path))
    if not pairs:
        raise CrosstalkAnalysisError(f"{folder} has no measured crosstalk pairs")
    switches = analyze_switches(run, pairs)
    output = Path(output) if output else folder / "Analysis"
    output.mkdir(parents=True, exist_ok=True)
    max_voltage = max(run.get("voltages_v") or [pair.voltages_v[-1] for pair in pairs])

    paths = {
        "pair_summary": output / "01_pair_summary.png",
        "curve_grid": output / "02_curves_by_victim.png",
        "drive_dependence": output / "03_voltage_vs_power.png",
        "switch_summary": output / "04_switch_summary.png",
        "pairs_csv": output / "crosstalk_pairs.csv",
        "switches_csv": output / "crosstalk_switches.csv",
        "summary": output / "summary.md",
    }
    render_pair_summary(paths["pair_summary"], pairs, max_voltage)
    render_curve_grid(paths["curve_grid"], pairs)
    render_drive_dependence(paths["drive_dependence"], pairs)
    render_switch_summary(paths["switch_summary"], switches, max_voltage)
    write_tables(output, pairs, switches)
    write_summary(paths["summary"], folder, pairs, switches)
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", help="crosstalk-matrix run folder (holds crosstalk_matrix.json)")
    parser.add_argument("--output", help="output directory (default: <folder>/Analysis)")
    arguments = parser.parse_args(argv)
    for path in analyze_folder(arguments.folder, arguments.output).values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
