"""Redraw the Crosstalk tab's two-panel Victims figures from a saved matrix run.

Reads ``crosstalk_matrix.json`` (victim cos² fits, quadrature bias voltages,
κ, heater resistances) and ``crosstalk_sweeps.csv`` (victim and aggressor
sweeps) from a ``*_matrix`` folder, without hardware. For every victim the
left panel shows each probe's measured sweep, its fit, the quadrature bias and
the bright point used as path reference; the right panel shows the phase shift
each aggressor caused, as in the live view. One ``<victim>_victim.png`` is
saved per victim in the run folder.

The bias and bright points are chosen from the saved fit with the rules the
run used. Missing readings leave gaps; a victim without a fit is drawn without
its line, bias or bright point; a victim whose probe failed shows the reason.

Run with::

    python -m benes.measurements.crosstalk_victim_plots Results/Crosstalk_matrix/<run>_matrix
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from benes.measurements.crosstalk_results import PLOT_X_POWER, PLOT_X_VOLTAGE, plot_x_values
from benes.measurements.mzi_characterization import (EXTREME_TIE_FRACTION, MZIFit, SweepPoint, draw_phase_shifts,
                                                     fitted_extremes, mw_to_dbm, quadrature_point)

BRIGHT_SUFFIX = " (victim bright)"


class VictimPlotError(ValueError):
    """Raised when a run folder cannot be read."""


@dataclass
class VictimProbe:
    """One probe of a victim: its own sweep, fit and the operating points the run used."""

    victim: str
    input_port: int
    output_port: int
    bright_crossed: bool = True
    points: list[SweepPoint] = field(default_factory=list)
    fit: MZIFit | None = None
    bias_voltage: float | None = None


@dataclass
class PairCurve:
    """The fields ``draw_phase_shifts`` reads from a crosstalk sweep."""

    aggressor: str
    victim: str
    input_port: int
    output_port: int
    drive: list[float] = field(default_factory=list)
    phase_shift: list[float | None] = field(default_factory=list)
    x_unit: str = "mW"
    kappa: float | None = None
    contamination: str | None = None


@dataclass
class MatrixRun:
    folder: Path
    unit: str
    status: str
    heater_ohm: dict[str, float]
    total_ohm: dict[str, float]
    probes: dict[tuple[str, int, int], VictimProbe]
    pairs: dict[tuple[str, str, int, int], PairCurve]
    # victim -> {reason its probe failed: aggressors left unmeasured}
    failed: dict[str, dict[str, list[str]]]
    warnings: list[str]

    def victims(self) -> list[str]:
        names = {key[0] for key in self.probes} | {pair.victim for pair in self.pairs.values()} | set(self.failed)
        return sorted(names, key=lambda name: [int(part) for part in name.lstrip("S").split(".")])

    def heater_drive(self, name: str, voltage: float, x_unit: str) -> float:
        """Drive in the fit's own unit: heater power in mW when the resistances are known, else V²."""
        heater = self.heater_ohm.get(name)
        if x_unit == "mW" and heater:
            total = self.total_ohm.get(name) or heater
            return voltage * voltage * heater / (total * total) * 1000
        return voltage * voltage


def _float(text) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fit(record) -> MZIFit | None:
    if not record:
        return None
    values = [_float(record.get(key)) for key in ("offset", "amplitude", "x_pi", "phase0_rad")]
    if None in values or not values[2]:
        return None
    return MZIFit(*values, _float(record.get("rms")) or 0.0, record.get("x_unit", "mW"))


def load_run(folder: Path | str) -> MatrixRun:
    folder = Path(folder)
    sweeps_path, json_path = folder / "crosstalk_sweeps.csv", folder / "crosstalk_matrix.json"
    if not sweeps_path.exists():
        raise VictimPlotError(f"{folder} has no crosstalk_sweeps.csv")
    warnings = []
    meta = {}
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except ValueError as error:
            warnings.append(f"crosstalk_matrix.json is unreadable ({error}); drawing measurements only")
    else:
        warnings.append("crosstalk_matrix.json is missing; drawing measurements only")

    unit = meta.get("unit", "mW")
    probes: dict[tuple[str, int, int], VictimProbe] = {}
    pairs: dict[tuple[str, str, int, int], PairCurve] = {}
    for record in meta.get("victims", []):
        key = record.get("mzi"), int(record.get("input", 0)), int(record.get("output", 0))
        probes[key] = VictimProbe(*key, bright_crossed=record.get("bright_state", "cross") == "cross",
                                  fit=_fit(record.get("fit")))
    for record in meta.get("pairs", []):
        key = record.get("aggressor"), record.get("victim"), int(record.get("input", 0)), int(record.get("output", 0))
        pairs[key] = PairCurve(*key, x_unit=record.get("x_unit", "mW"), kappa=_float(record.get("kappa")),
                               contamination=record.get("contamination"))
        probe = probes.get((key[1], key[2], key[3]))
        if probe is not None and probe.bias_voltage is None:
            probe.bias_voltage = _float(record.get("bias_voltage_v"))
    failed: dict[str, dict[str, list[str]]] = {}
    for record in meta.get("failed_pairs", []):
        reasons = failed.setdefault(record.get("victim"), {})
        reasons.setdefault(record.get("reason") or "no reason saved", []).append(record.get("aggressor"))

    lines = sweeps_path.read_text(encoding="utf-8-sig").splitlines()
    for row in csv.DictReader(line for line in lines if line.strip() and not line.startswith("#")):
        swept, victim = (row.get("swept") or "").strip(), (row.get("victim") or "").strip()
        voltage = _float(row.get("voltage_v"))
        if not swept or not victim or voltage is None:
            continue
        try:
            input_port, output_port = int(row["input"]), int(row["output"])
        except (KeyError, TypeError, ValueError):
            continue
        if swept.endswith(BRIGHT_SUFFIX):
            continue  # the path reference sweeps are already divided into the phase shift
        if swept == victim:
            power = _float(row.get(f"power_{unit}"))
            if power is None and unit == "mW":
                dbm = _float(row.get("power_dbm"))
                power = None if dbm is None else 10 ** (dbm / 10)
            probe = probes.setdefault((victim, input_port, output_port), VictimProbe(victim, input_port, output_port))
            probe.points.append(SweepPoint(voltage, power))
        else:
            pair = pairs.setdefault((swept, victim, input_port, output_port),
                                    PairCurve(swept, victim, input_port, output_port))
            drive = _float(row.get("aggressor_drive"))
            pair.drive.append(math.nan if drive is None else drive)
            pair.phase_shift.append(_float(row.get("victim_phase_shift_rad")))
            pair.x_unit = row.get("drive_unit") or pair.x_unit

    for key, probe in probes.items():
        probe.points.sort(key=lambda point: point.voltage)
        if not probe.points:
            warnings.append(f"{key[0]} In {key[1]} -> Out {key[2]}: no victim sweep saved")
    for key, pair in list(pairs.items()):
        if not pair.drive:
            warnings.append(f"{pair.aggressor} -> {pair.victim}: no aggressor sweep saved")
            del pairs[key]
    return MatrixRun(folder, unit, meta.get("status", "unknown"), meta.get("heater_resistance_ohm", {}),
                     meta.get("total_resistance_ohm", {}), probes, pairs, failed, warnings)


def operating_points(run: MatrixRun, probe: VictimProbe) -> tuple[SweepPoint | None, SweepPoint | None]:
    """The (quadrature bias, bright) points of a probe, by the rules the run used."""
    measured = [point for point in probe.points if point.power is not None]
    if not measured:
        return None, None
    fit = probe.fit
    xs = [run.heater_drive(probe.victim, point.voltage, fit.x_unit if fit else "V²") for point in probe.points]
    if probe.bias_voltage is not None:
        bias = min(measured, key=lambda point: abs(point.voltage - probe.bias_voltage))
    elif fit is not None:
        bias = quadrature_point(SimpleNamespace(name=probe.victim, fit=fit, points=probe.points), xs)
    else:
        bias = None
    if fit is not None and fit.amplitude > 0 and fit.rms < 0.05 * fit.amplitude:
        bright = fitted_extremes(fit, xs, probe.points)[0]
    else:
        high, low = max(p.power for p in measured), min(p.power for p in measured)
        close = [p for p in measured if high - p.power <= EXTREME_TIE_FRACTION * (high - low)]
        bright = min(close, key=lambda point: (abs(point.voltage), -point.power))
    return bias, bright


def draw_victim(figure, run: MatrixRun, name: str, x_mode: str = PLOT_X_VOLTAGE) -> None:
    """The live Victims figure: sweeps, fits and operating points left, phase shifts right."""
    from matplotlib import colormaps

    response, shifts = figure.subplots(1, 2, gridspec_kw={"width_ratios": (1, 1.25)})
    decibels = run.unit == "mW"

    def convert(power):
        value = None if power is None else (mw_to_dbm(power) if decibels else power)
        return float("nan") if value is None else value

    def plot_x(voltages):
        return plot_x_values(voltages, run.heater_ohm.get(name), x_mode, run.total_ohm.get(name))

    colors = colormaps["tab10"].colors
    probes = [probe for key, probe in run.probes.items() if key[0] == name and probe.points]
    for index, probe in enumerate(probes):
        color = colors[index % len(colors)]
        voltages = [point.voltage for point in probe.points]
        response.plot(plot_x(voltages)[0], [convert(point.power) for point in probe.points], ".", color=color,
                      markersize=4, label=f"In {probe.input_port} → Out {probe.output_port}")
        if probe.fit is not None:
            dense = [voltages[0] + step * (voltages[-1] - voltages[0]) / 200 for step in range(201)]
            drive = [run.heater_drive(name, voltage, probe.fit.x_unit) for voltage in dense]
            response.plot(plot_x(dense)[0], [convert(max(probe.fit.model(x), 1e-12)) for x in drive],
                          "-", color=color, linewidth=1, alpha=0.7)
        bias, bright = operating_points(run, probe)
        if bias is not None:
            response.plot(plot_x([bias.voltage])[0], [convert(bias.power)], "o", color=color,
                          markersize=8, markerfacecolor="none", markeredgewidth=1.6)
        if bright is not None:
            response.plot(plot_x([bright.voltage])[0], [convert(bright.power)], "^", color=color,
                          markersize=7, markerfacecolor="none", markeredgewidth=1.4)
    if probes:
        response.plot([], [], "o", color="gray", markerfacecolor="none", label="Quadrature bias")
        response.plot([], [], "^", color="gray", markerfacecolor="none", label="Bright (path reference)")
        response.legend(fontsize=7)
    else:
        response.text(0.5, 0.5, "No victim sweep saved", ha="center", va="center", transform=response.transAxes,
                      color="#777777")
    label = plot_x([])[1]
    response.set(xlabel=f"Victim {label[0].lower()}{label[1:]}",
                 ylabel=f"Power [{'dBm' if decibels else run.unit}]",
                 title=f"{name}: victim sweep, line = cos² fit")
    response.grid(True, alpha=0.4)

    items = [pair for pair in run.pairs.values() if pair.victim == name]
    draw_phase_shifts(shifts, name, items)
    notes = ([] if items else ["No aggressor sweep saved"]) + [
        f"Victim probe failed: {reason}\n(aggressors {', '.join(aggressors)} not measured)"
        for reason, aggressors in run.failed.get(name, {}).items()]
    if notes:
        shifts.text(0.5, 0.5, "\n".join(notes), ha="center", va="center", transform=shifts.transAxes,
                    fontsize=8, color="#777777", wrap=True)
    figure.tight_layout()


def save_victim_figures(run: MatrixRun, x_mode: str = PLOT_X_VOLTAGE) -> list[Path]:
    from matplotlib.figure import Figure

    paths = []
    for name in run.victims():
        figure = Figure(figsize=(9, 4.5))
        draw_victim(figure, run, name, x_mode)
        path = run.folder / f"{name.replace('.', '_')}_victim.png"
        figure.savefig(path, dpi=150, bbox_inches="tight")
        paths.append(path)
    return paths


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redraw the Victims figures of a saved crosstalk matrix run.")
    parser.add_argument("folder", type=Path, help="A *_matrix folder with crosstalk_sweeps.csv")
    parser.add_argument("--x", choices=(PLOT_X_VOLTAGE, PLOT_X_POWER), default=PLOT_X_VOLTAGE,
                        help="Victim sweep x axis: voltage (default) or heater power")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        run = load_run(args.folder)
        paths = save_victim_figures(run, args.x)
    except (OSError, VictimPlotError) as error:
        raise SystemExit(f"Could not redraw victims: {error}") from error
    if run.status not in ("complete", "unknown"):
        print(f"Run status is {run.status!r}; some victims may be incomplete.")
    for warning in run.warnings:
        print(f"Note: {warning}")
    print(f"Saved {len(paths)} victim figure(s) in {run.folder}")
    for path in paths:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
