"""Create a self-contained folder for a crosstalk scan."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from calibration_scan import SpectrumScanPoint
from spectrum_export import write_crosstalk_csv


POWER_COLOR = "#ffbe69"
OSA_COLOR = "#78b9ff"
ROUTE_COLOR = "#36b38f"


def scan_context(network, input_port: int, output_port: int, power_meter_port: int | None,
                 osa_port: int | None, controller, names, voltages, settle_ms, source, comment,
                 states=None):
    """Freeze route and voltage-board state before the scan changes any outputs."""
    states = dict(network.states if states is None else states)
    route = network.trace(input_port, states)
    return {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_port": input_port + 1,
        "selected_output_port": output_port + 1,
        "actual_output_port": route.rows[-1] + 1,
        "power_meter_output_port": None if power_meter_port is None else power_meter_port + 1,
        "osa_output_port": None if osa_port is None else osa_port + 1,
        "path_rows": [row + 1 for row in route.rows],
        "path_switches": [{"name": name, "state": "cross" if crossed else "bar"}
                          for name, crossed in route.states],
        "switch_states": {name: "cross" if crossed else "bar" for name, crossed in states.items()},
        "switch_pins": dict(controller.settings["mapping"]),
        "applied_voltages_before_scan": dict(controller.applied_voltages),
        "scanned_switches": list(names),
        "voltages_v": list(voltages),
        "settle_ms": settle_ms,
        "measurement_source": source,
        "comment": comment,
    }


def create_result_folder(project_dir: Path, context: dict) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"Port{context['input_port']}-Port{context['selected_output_port']}_{stamp}"
    folder = Path(project_dir) / "Results" / "Crosstalk_scan" / name
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def save_scan_artifacts(folder: Path, context: dict, data: dict, source: str,
                        response_unit: str, network, status: str = "complete") -> None:
    """Save dataset, route snapshot, and one figure per measured switch."""
    from matplotlib import colormaps
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
    from matplotlib.figure import Figure

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    summary = {**context, "status": status, "points_per_switch": {name: len(points) for name, points in data.items()}}
    (folder / "base_setting.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _save_path_figure(folder / "base_path.png", context, network, Figure)
    if data and any(data.values()):
        write_crosstalk_csv(folder / "crosstalk_dataset.csv", data, source, context["comment"], response_unit)
    for name, points in data.items():
        if not points:
            continue
        figure = Figure(figsize=(10, 5))
        response = figure.add_subplot(121 if source == "OSA" else 111)
        response.plot([point.voltage for point in points], [point.power for point in points],
                      marker=".", linewidth=1.4, color=ROUTE_COLOR,
                      label="Peak" if source == "OSA" else "Power")
        if source == "OSA":
            response.plot([point.voltage for point in points],
                          [point.mean_power_dbm for point in points],
                          marker=".", linewidth=1.4, color=POWER_COLOR, label="Mean")
            response.legend(loc="best")
        response.set(xlabel="Voltage [V]",
                     ylabel=f"Power [{response_unit}]",
                     title=f"{name}: response")
        response.grid(True)
        response.autoscale(enable=True, axis="y")
        if source == "OSA":
            spectra = figure.add_subplot(122)
            limits = context["voltages_v"]
            norm = Normalize(vmin=min(limits), vmax=max(limits))
            cmap = colormaps["viridis"]
            for point in points:
                if isinstance(point, SpectrumScanPoint):
                    spectra.plot(point.wavelength_nm, point.spectrum_dbm, linewidth=0.8,
                                 alpha=0.65, color=cmap(norm(point.voltage)))
            spectra.set(xlabel="Wavelength [nm]", ylabel="Power [dBm]", title=f"{name}: spectra")
            spectra.grid(True)
            spectra.autoscale(enable=True, axis="y")
            figure.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=spectra, label="Voltage [V]")
        figure.tight_layout()
        figure.savefig(folder / f"{name.replace('.', '_')}.png", dpi=160)


def _save_path_figure(path: Path, context: dict, network, Figure) -> None:
    figure = Figure(figsize=(9, 4))
    axes = figure.add_subplot(111)
    states = {name: value == "cross" for name, value in context["switch_states"].items()}
    for row in range(network.size):
        for stage in range(len(network.pairs)):
            next_row = network.step(stage, row, states.get(network.at.get((stage, row)), False))
            axes.plot((stage, stage + 1), (network.size - row, network.size - next_row),
                      color="#c6cbd2", linewidth=0.8, zorder=1)
    rows = [row - 1 for row in context["path_rows"]]
    axes.plot(range(len(rows)), [network.size - row for row in rows], color=ROUTE_COLOR,
              linewidth=3, marker="o", markersize=5, zorder=3, label="Actual path")
    for stage, row in enumerate(rows[:-1]):
        name = network.at.get((stage, row))
        if name:
            state = "X" if states[name] else "bar"
            y = network.size - (row + rows[stage + 1]) / 2
            axes.text(stage + 0.5, y + 0.16, f"{name} {state}", ha="center", va="bottom", fontsize=7)
    output_x = len(network.pairs)
    for port, color, label, size in (
        (context["selected_output_port"], ROUTE_COLOR, "Selected output", 170),
        (context["power_meter_output_port"], POWER_COLOR, "Power Meter", 260),
        (context["osa_output_port"], OSA_COLOR, "OSA", 370),
    ):
        if port is not None:
            axes.scatter([output_x], [network.size - port + 1], s=size, facecolors="none",
                         edgecolors=color, linewidths=2, zorder=4, label=f"{label}: port {port}")
    axes.set(xlim=(-0.35, output_x + 0.55), ylim=(0.5, network.size + 0.5),
             title=f"Base path: input {context['input_port']} to actual output {context['actual_output_port']}",
             ylabel="Port")
    axes.set_xticks(range(output_x + 1), ["Input", "1", "2", "3", "4", "Output"])
    axes.set_yticks(range(1, network.size + 1), list(range(network.size, 0, -1)))
    axes.grid(True, alpha=0.2)
    axes.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=4, fontsize=8)
    figure.tight_layout()
    figure.savefig(path, dpi=160, bbox_inches="tight")
