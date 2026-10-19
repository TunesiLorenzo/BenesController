"""Create a self-contained folder for a crosstalk scan."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from calibration_scan import ScanPoint, SpectrumScanPoint
from spectrum_export import write_crosstalk_csv


POWER_COLOR = "#ffbe69"
OSA_COLOR = "#78b9ff"
ROUTE_COLOR = "#36b38f"
# Path-calibration rescans, keyed by phase: (context key, legend label, colour).
RESCAN_STYLES = {
    "sensitive": ("sensitive_range_rescans", "Sensitive-range rescan", "#b48cff"),
    "wide": ("wide_range_rescans", "Wider-range rescan", "#ff7a59"),
}
CROSSTALK_SUBFOLDER = "Crosstalk_scan"
CALIBRATION_SUBFOLDER = "Path_calibration"
PLOT_X_VOLTAGE = "voltage"
PLOT_X_POWER = "power"


def plot_x_values(voltages, resistance_ohm, mode):
    """Return x values and axis label: commanded volts, or heater power V^2/R in mW.

    Falls back to volts when no resistance is known for the switch.
    """
    if mode == PLOT_X_POWER and resistance_ohm:
        return ([voltage * voltage / resistance_ohm * 1000 for voltage in voltages],
                f"Heater power [mW] (R = {resistance_ohm:g} Ω)")
    label = "Voltage [V]" if mode != PLOT_X_POWER else "Voltage [V] (no heater resistance set)"
    return list(voltages), label


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
        "heater_resistance_ohm": {name: controller.heater_resistance(name) for name in names}
        if hasattr(controller, "heater_resistance") else {},
        "voltages_v": list(voltages),
        "settle_ms": settle_ms,
        "measurement_source": source,
        "comment": comment,
    }


def create_result_folder(project_dir: Path, context: dict) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"Port{context['input_port']}-Port{context['selected_output_port']}_{stamp}"
    kind = CALIBRATION_SUBFOLDER if context.get("calibration_mode") else CROSSTALK_SUBFOLDER
    folder = Path(project_dir) / "Results" / kind / name
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def load_scan_folder(folder) -> dict:
    """Read a saved scan or calibration folder back into plottable points.

    Returns the saved context, ``data`` as {switch: [points]} in measured order,
    the measurement source, the response unit, and the dataset comment.
    """
    folder = Path(folder)
    settings_path = folder / "base_setting.json"
    if not settings_path.exists():
        raise ValueError(f"{folder.name} has no base_setting.json; choose a scan results folder")
    context = json.loads(settings_path.read_text(encoding="utf-8"))
    source = context.get("measurement_source", "Arduino")
    unit = "dBm" if source == "OSA" else ""
    comment = []
    data = {}
    dataset = folder / "crosstalk_dataset.csv"
    if dataset.exists():
        lines = dataset.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.startswith("# Response unit: "):
                unit = line.removeprefix("# Response unit: ").strip()
            elif line.startswith("# Description: "):
                comment.append(line.removeprefix("# Description: "))
        rows = csv.reader(line for line in lines if not line.startswith("#"))
        header = next(rows, [])
        power_column = header.index("peak_power_dbm" if "peak_power_dbm" in header else "response_power")
        spectrum = None  # (switch, voltage, peak, wavelengths, powers) being collected

        def flush():
            if spectrum is not None:
                name, voltage, peak, wavelengths, powers = spectrum
                data.setdefault(name, []).append(
                    SpectrumScanPoint(voltage, peak, tuple(wavelengths), tuple(powers)))

        for row in rows:
            if not row:
                continue
            record = dict(zip(header, row))
            name, voltage, power = row[0], float(record["voltage_v"]), float(row[power_column])
            if record.get("wavelength_nm", "") == "":
                flush()
                spectrum = None
                data.setdefault(name, []).append(ScanPoint(voltage, power))
                continue
            # OSA spectra take one row per wavelength bin; consecutive rows of one point share its key.
            if spectrum is None or spectrum[:3] != (name, voltage, power):
                flush()
                spectrum = (name, voltage, power, [], [])
            spectrum[3].append(float(record["wavelength_nm"]))
            spectrum[4].append(float(record["spectrum_power_dbm"]))
        flush()
    return {"context": context, "data": data, "source": source, "unit": unit,
            "comment": "\n".join(comment).strip() or context.get("comment", "")}


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
        resistance = context.get("heater_resistance_ohm", {}).get(name)
        mode = context.get("plot_x_axis", PLOT_X_VOLTAGE)
        xs, x_label = plot_x_values([point.voltage for point in points], resistance, mode)
        response.plot(xs, [point.power for point in points],
                      marker=".", linewidth=1.4, color=ROUTE_COLOR,
                      label="Peak" if source == "OSA" else "Power")
        if source == "OSA":
            response.plot(xs, [point.mean_power_dbm for point in points],
                          marker=".", linewidth=1.4, color=POWER_COLOR, label="Mean")
            response.legend(loc="best")
        response.set(xlabel=x_label,
                     ylabel=f"Power [{response_unit}]",
                     title=f"{name}: response")
        response.grid(True)
        response.autoscale(enable=True, axis="y")
        rescans = {phase: context.get(key, {}).get(name) for phase, (key, _, _) in RESCAN_STYLES.items()}
        if any(rescans.values()):
            # Rescans use another meter range, so they get their own axis.
            rescan_axis = response.twinx()
            rescan_axis.set_ylabel(f"Rescan power [{response_unit}] (changed range)")
            for phase, rows in rescans.items():
                if rows:
                    _, label, color = RESCAN_STYLES[phase]
                    rescan_axis.plot(plot_x_values([row["voltage_v"] for row in rows], resistance, mode)[0],
                                     [row["power"] for row in rows],
                                     marker=".", linestyle="--", linewidth=1.2, color=color, label=label)
            lines = response.get_lines() + rescan_axis.get_lines()
            response.legend(lines, [line.get_label() for line in lines], loc="best")
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
