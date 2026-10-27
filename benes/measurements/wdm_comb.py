"""WDM comb plan and per-output OSA scan.

A WDM comb feeds the Benes inputs with one comb line per input.  The plan
records which wavelength enters which input; the active permutation then
determines the output where each line should appear.  The scan routes the JDSU
switch to each output in turn, waits for the switch and the OSA sweep to
settle, reads the spectrum and measures the power of every comb line in it,
so each output gives its routed signal and the leakage of all other lines.
"""

from __future__ import annotations

import csv
import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from benes.hardware.osa import trim_spurious_tail
from benes.hardware.osa_output_switch import fiber_number
from benes.measurements.spectrum_export import write_spectra
from benes.paths import app_dir

INPUTS = tuple(range(1, 7))
WDM_SUBFOLDER = "WDM_scan"


def input_fiber_number(input_port: int) -> int:
    """Benes input 1 enters the chip on fiber 15, input 6 on fiber 10."""
    return 16 - input_port


def _wavelength(value) -> float | None:
    if value is None or str(value).strip() in ("", "—", "None"):
        return None
    wavelength = float(value)
    if not 400 <= wavelength <= 2000:
        raise ValueError(f"Wavelength {wavelength:g} nm is outside 400–2000 nm")
    return wavelength


def normalized_assignment(assignment: dict) -> dict:
    """Return {"1".."6": wavelength_nm or None}; one comb line per input."""
    normalized = {}
    for port in INPUTS:
        try:
            normalized[str(port)] = _wavelength(assignment.get(str(port), assignment.get(port)))
        except (TypeError, ValueError) as error:
            raise ValueError(f"Input {port}: {error}") from error
    seen = {}
    for port, wavelength in normalized.items():
        if wavelength is None:
            continue
        if wavelength in seen:
            raise ValueError(f"Inputs {seen[wavelength]} and {port} are both assigned {wavelength:g} nm")
        seen[wavelength] = port
    return normalized


def parse_comb_lines(text) -> list[float]:
    """Parse comb wavelengths separated by commas, semicolons or whitespace."""
    values = str(text).replace(",", " ").replace(";", " ").split()
    try:
        return sorted({_wavelength(value) for value in values})
    except ValueError as error:
        raise ValueError(f"Comb lines: {error}") from error


def routed_wavelengths(mapping, assignment: dict) -> dict:
    """Map each output to (input, wavelength) for a 0-based ``network.mapping()``."""
    return {mapping[port - 1] + 1: (port, assignment[str(port)]) for port in INPUTS}


class WDMPlan:
    """Persistent input → wavelength plan, named presets and scan settings."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or app_dir() / "benes_wdm_plan_config.json"
        self.settings = self._load()

    @staticmethod
    def _defaults() -> dict:
        return {
            "assignment": {str(port): None for port in INPUTS},
            "presets": {},
            "active_preset": None,
            "comb_lines": [],
            "half_window_nm": 0.2,
            "settle_s": 2.0,
        }

    def _load(self) -> dict:
        settings = self._defaults()
        if not self.config_path.exists():
            return settings
        try:
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            candidate = {**settings, **{name: stored[name] for name in settings if name in stored}}
            candidate["assignment"] = normalized_assignment(candidate["assignment"])
            candidate["comb_lines"] = parse_comb_lines(" ".join(map(str, candidate["comb_lines"])))
            candidate["presets"] = {str(name): normalized_assignment(value)
                                    for name, value in dict(candidate["presets"]).items()}
            self._validate_scan(candidate["half_window_nm"], candidate["settle_s"])
            settings = candidate
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            # A damaged plan must not prevent the controller from starting.
            pass
        return settings

    @staticmethod
    def _validate_scan(half_window_nm, settle_s) -> tuple[float, float]:
        half_window_nm, settle_s = float(half_window_nm), float(settle_s)
        if not 0 < half_window_nm <= 10:
            raise ValueError("Line window must be between 0 and 10 nm")
        if not 0 <= settle_s <= 600:
            raise ValueError("Settling delay must be between 0 and 600 seconds")
        return half_window_nm, settle_s

    def save(self) -> None:
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)

    @property
    def assignment(self) -> dict:
        return self.settings["assignment"]

    def configure(self, assignment: dict, half_window_nm, settle_s, comb_lines="") -> None:
        assignment = normalized_assignment(assignment)
        half_window_nm, settle_s = self._validate_scan(half_window_nm, settle_s)
        self.settings.update(assignment=assignment, half_window_nm=half_window_nm, settle_s=settle_s,
                             comb_lines=parse_comb_lines(comb_lines))
        self.save()

    def save_preset(self, name: str) -> None:
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a name for the wavelength preset")
        self.settings["presets"][name] = dict(self.assignment)
        self.settings["active_preset"] = name
        self.save()

    def load_preset(self, name: str) -> dict:
        if name not in self.settings["presets"]:
            raise ValueError(f"No wavelength preset named {name!r}")
        self.settings["assignment"] = dict(self.settings["presets"][name])
        self.settings["active_preset"] = name
        self.save()
        return self.assignment

    def delete_preset(self, name: str) -> None:
        self.settings["presets"].pop(name, None)
        if self.settings["active_preset"] == name:
            self.settings["active_preset"] = None
        self.save()


@dataclass(frozen=True)
class LinePower:
    peak_dbm: float
    peak_nm: float
    mean_dbm: float


def line_power(wavelength, power_dbm, center_nm: float, half_window_nm: float) -> LinePower | None:
    """Peak and mean (of linear mW) power of the bins within ±half_window of a line.

    OSA bins are narrower than the resolution bandwidth, so a sum of bins would
    count the same power many times; the mean matches the crosstalk scan.
    """
    bins = [(wave, power) for wave, power in zip(wavelength, power_dbm)
            if abs(wave - center_nm) <= half_window_nm]
    if not bins:
        return None
    peak_nm, peak_dbm = max(bins, key=lambda item: item[1])
    mean_mw = sum(10 ** (power / 10) for _, power in bins) / len(bins)
    return LinePower(peak_dbm, peak_nm, 10 * math.log10(mean_mw))


@dataclass
class WDMOutputResult:
    output: int
    jdsu_channel: int
    routed_input: int
    wavelength_nm: float | None
    wave: list
    power: list
    # Power of every assigned comb line in this output's spectrum, keyed by input.
    lines: dict = field(default_factory=dict)

    @property
    def fiber(self) -> int:
        return fiber_number(self.output)

    @property
    def signal(self) -> LinePower | None:
        return self.lines.get(self.routed_input)

    @property
    def isolation_db(self) -> float | None:
        """Routed line peak minus the strongest other comb line at this output."""
        others = [line.peak_dbm for port, line in self.lines.items()
                  if port != self.routed_input and line is not None]
        if self.signal is None or not others:
            return None
        return self.signal.peak_dbm - max(others)


@dataclass(frozen=True)
class ConfigurationLineResult:
    """Six-port result for one input laser wavelength."""

    input_port: int
    wavelength_nm: float
    expected_output: int
    total_power_dbm: float | None
    transmission_dbm: float | None
    # Expected-port power divided by total power of this wavelength over all outputs.
    normalized_transmission: float | None
    # Output -> power relative to the expected-port transmission, in dBc.
    crosstalk_dbc: dict[int, float | None]
    # Output -> power divided by total power of this wavelength over all outputs.
    normalized_crosstalk: dict[int, float | None]


def evaluate_configuration(results: list[WDMOutputResult], mapping, assignment: dict) -> list[ConfigurationLineResult]:
    """Summarise a six-output WDM scan by wavelength/input.

    Total power is the linear sum of the measured line peak at every output.
    Crosstalk is reported relative to the line at its expected routed output.
    """
    by_output = {result.output: result for result in results}
    summary = []
    for input_port in INPUTS:
        wavelength_nm = assignment.get(str(input_port))
        if wavelength_nm is None:
            continue
        expected_output = int(mapping[input_port - 1]) + 1
        signal = by_output.get(expected_output)
        expected_line = None if signal is None else signal.lines.get(input_port)
        transmission_dbm = None if expected_line is None else expected_line.peak_dbm
        output_powers = {result.output: line.peak_dbm for result in results
                         if (line := result.lines.get(input_port)) is not None}
        total_mw = sum(10 ** (power / 10) for power in output_powers.values())
        total_power_dbm = 10 * math.log10(total_mw) if total_mw else None
        normalized_transmission = (10 ** (transmission_dbm / 10) / total_mw
                                   if transmission_dbm is not None and total_mw else None)
        crosstalk_dbc = {}
        normalized_crosstalk = {}
        for output, result in by_output.items():
            line = result.lines.get(input_port)
            crosstalk_dbc[output] = (None if output == expected_output or line is None or transmission_dbm is None
                                     else line.peak_dbm - transmission_dbm)
            normalized_crosstalk[output] = (None if output == expected_output or line is None or not total_mw
                                            else 10 ** (line.peak_dbm / 10) / total_mw)
        summary.append(ConfigurationLineResult(
            input_port, float(wavelength_nm), expected_output, total_power_dbm, transmission_dbm,
            normalized_transmission, crosstalk_dbc, normalized_crosstalk))
    return summary


def simulated_comb_spectrum(assignment: dict, routed_input: int, points: int = 4001) -> tuple[list, list]:
    """Dry-run trace: the routed line at -10 dBm, the other comb lines leaking at -45 dBm."""
    lines = [(wavelength, -10.0 if int(port) == routed_input else -45.0)
             for port, wavelength in assignment.items() if wavelength is not None]
    centers = [wavelength for wavelength, _ in lines] or [1550.0]
    start, stop = min(centers) - 2, max(centers) + 2
    wave = [start + index * (stop - start) / (points - 1) for index in range(points)]
    power = []
    for wavelength in wave:
        total_mw = 1e-7 + sum(10 ** (peak / 10) / (1 + ((wavelength - center) / 0.02) ** 2)
                              for center, peak in lines)
        power.append(10 * math.log10(total_mw))
    return wave, power


def scan_outputs(switch, read_spectrum: Callable[[int], tuple], outputs, mapping, assignment: dict,
                 half_window_nm: float, settle_s: float,
                 on_result: Callable[[WDMOutputResult, int, int], None] | None = None,
                 stop_event: threading.Event | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> list[WDMOutputResult]:
    """Route each output to the OSA, wait ``settle_s`` and measure every comb line."""
    routed = routed_wavelengths(mapping, assignment)
    results = []
    outputs = list(outputs)
    for index, output in enumerate(outputs, start=1):
        if stop_event is not None and stop_event.is_set():
            break
        switch.select_output(output)
        # Wait in short steps so Stop stays responsive during long OSA sweeps.
        remaining = settle_s
        while remaining > 0:
            if stop_event is not None and stop_event.is_set():
                return results
            step = min(0.1, remaining)
            sleep(step)
            remaining -= step
        wave, power = trim_spurious_tail(*read_spectrum(output))
        routed_input, wavelength = routed[output]
        lines = {port: line_power(wave, power, assignment[str(port)], half_window_nm)
                 for port in INPUTS if assignment[str(port)] is not None}
        result = WDMOutputResult(output, switch.channel_for(output), routed_input, wavelength,
                                 wave, power, lines)
        results.append(result)
        if on_result is not None:
            on_result(result, index, len(outputs))
    return results


def _fmt(value, digits=3):
    return "" if value is None else f"{value:.{digits}f}"


def save_wdm_scan(folder: Path, results: list[WDMOutputResult], context: dict, comment: str = "") -> list[str]:
    """Write summary, line-power matrix, spectra and context into ``folder``."""
    if not results:
        raise ValueError("The WDM scan has no results to save")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    assignment = context["assignment"]
    lined_inputs = [port for port in INPUTS if assignment[str(port)] is not None]
    header = [f"# WDM comb scan saved {datetime.now().isoformat(timespec='seconds')}\n",
              f"# Permutation (output per input 1-6): {' '.join(map(str, context['permutation']))}\n",
              f"# Line window: ±{context['half_window_nm']:g} nm; settling delay {context['settle_s']:g} s\n"]
    header += [f"# Comment: {line}\n" for line in comment.splitlines() or [""]]

    with (folder / "wdm_summary.csv").open("w", encoding="utf-8", newline="") as file:
        file.writelines(header)
        writer = csv.writer(file)
        writer.writerow(["output", "output_fiber", "jdsu_channel", "routed_input", "input_fiber",
                         "wavelength_nm", "peak_power_dbm", "peak_wavelength_nm", "window_mean_dbm",
                         "isolation_db"])
        for result in results:
            signal = result.signal
            writer.writerow([result.output, result.fiber, result.jdsu_channel, result.routed_input,
                             input_fiber_number(result.routed_input), _fmt(result.wavelength_nm, 4),
                             _fmt(signal and signal.peak_dbm), _fmt(signal and signal.peak_nm, 4),
                             _fmt(signal and signal.mean_dbm), _fmt(result.isolation_db)])

    with (folder / "wdm_line_matrix.csv").open("w", encoding="utf-8", newline="") as file:
        file.writelines(header)
        file.write("# Peak power [dBm] of each input's comb line (columns) at each output (rows)\n")
        writer = csv.writer(file)
        writer.writerow(["output", "routed_input"] +
                        [f"in{port}_{assignment[str(port)]:g}nm" for port in lined_inputs])
        for result in results:
            line_values = [result.lines.get(port) for port in lined_inputs]
            writer.writerow([result.output, result.routed_input] +
                            [_fmt(line and line.peak_dbm) for line in line_values])

    write_spectra(folder / "wdm_spectra.csv",
                  [(f"Out {result.output} (In {result.routed_input})", result.wave, result.power)
                   for result in results], comment)
    files = ["wdm_summary.csv", "wdm_line_matrix.csv", "wdm_spectra.csv", "wdm_scan.json"]
    record = {**context, "comment": comment, "outputs": [
        {"output": result.output, "jdsu_channel": result.jdsu_channel, "routed_input": result.routed_input,
         "wavelength_nm": result.wavelength_nm, "isolation_db": result.isolation_db,
         "lines": {str(port): None if line is None else vars(line) for port, line in result.lines.items()}}
        for result in results]}
    (folder / "wdm_scan.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return files


def create_wdm_folder(project_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder = Path(project_dir) / "Results" / WDM_SUBFOLDER / stamp
    folder.mkdir(parents=True, exist_ok=False)
    return folder
