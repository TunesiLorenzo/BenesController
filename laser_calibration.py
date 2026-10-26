"""Calibrate the Beneš MZIs with a laser on every input.

Each input carries its own WDM comb line (the plan in the WDM comb tab), so in
any switch configuration every MZI carries two distinguishable lines.  The OSA
reads one output at a time through the JDSU switch; the power of each line at
that output shows which way every MZI sent it.

* ``switch_probes`` finds the two outputs whose arriving line depends on one
  MZI, and which input arrives there in its cross and in its straight state.
* ``sweep_switch`` sweeps one MZI while the OSA watches one of those outputs:
  cross is where the cross-state line is strongest relative to the other.
* ``check_states`` reads every output and grades the state of each MZI.
* ``calibrate_switch`` and ``calibrate_all`` hold every other MZI at 0 V (its
  unbiased cross state) and save the extracted straight/cross voltages.
"""

from __future__ import annotations

import csv
import json
import math
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from osa import trim_spurious_tail
from osa_output_switch import fiber_number
from spectrum_export import write_spectra
from wdm_comb import input_fiber_number, line_power

CALIBRATION_SUBFOLDER = "MZI_calibration"
# A state check passes when the commanded line beats the other by this margin.
DEFAULT_PASS_DB = 10.0
# Sweep points this close to the best contrast are treated as equally good.
EXTREME_TIE_DB = 0.5


class CalibrationStopped(RuntimeError):
    """The user stopped a calibration between two measurements."""


@dataclass(frozen=True)
class SwitchProbe:
    """An output whose arriving line depends on one MZI; rows are 0-based."""

    name: str
    output: int
    cross_input: int
    straight_input: int

    def expected_input(self, crossed: bool) -> int:
        return self.cross_input if crossed else self.straight_input


@dataclass(frozen=True)
class ProbePoint:
    voltage: float
    cross_dbm: float | None
    straight_dbm: float | None

    @property
    def contrast_db(self) -> float | None:
        """Cross-state line minus straight-state line at the probed output.

        With only one of the two lines in the plan, its own power (negated for
        the straight line) stands in; only the extremes of a sweep are used.
        """
        if self.cross_dbm is None:
            return None if self.straight_dbm is None else -self.straight_dbm
        if self.straight_dbm is None:
            return self.cross_dbm
        return self.cross_dbm - self.straight_dbm


@dataclass(frozen=True)
class MZICalibration:
    probe: SwitchProbe
    points: tuple[ProbePoint, ...]
    cross: ProbePoint
    straight: ProbePoint

    @property
    def name(self) -> str:
        return self.probe.name

    def _both_lines(self) -> bool:
        return all(point.cross_dbm is not None and point.straight_dbm is not None
                   for point in (self.cross, self.straight))

    @property
    def cross_margin_db(self) -> float | None:
        """At the cross voltage, how far the cross line exceeds the other line."""
        return self.cross.contrast_db if self._both_lines() else None

    @property
    def straight_margin_db(self) -> float | None:
        return -self.straight.contrast_db if self._both_lines() else None


@dataclass(frozen=True)
class OutputReadout:
    output: int
    wave: tuple[float, ...]
    power: tuple[float, ...]
    # Peak dBm of each planned input's line at this output.
    lines: dict[int, float | None]


@dataclass(frozen=True)
class SwitchCheck:
    name: str
    crossed: bool
    # (output, expected input, other input, expected minus other [dB]) per probe output.
    margins: tuple[tuple[int, int, int, float | None], ...]

    @property
    def margin_db(self) -> float | None:
        values = [margin for *_, margin in self.margins if margin is not None]
        return min(values) if values else None

    def verdict(self, pass_db: float = DEFAULT_PASS_DB) -> str:
        margin = self.margin_db
        if margin is None:
            return "unknown"
        if margin >= pass_db:
            return "ok"
        return "inverted" if margin <= -pass_db else "partial"


@dataclass(frozen=True)
class StateCheck:
    label: str
    states: dict[str, bool]
    expected: tuple[int, ...]
    readouts: tuple[OutputReadout, ...]
    switches: dict[str, SwitchCheck]

    @property
    def matrix(self) -> dict[int, dict[int, float | None]]:
        return {readout.output: readout.lines for readout in self.readouts}

    def strongest_output(self, source: int) -> int | None:
        """Output where an input's line was measured strongest."""
        powers = {output: lines.get(source) for output, lines in self.matrix.items()}
        powers = {output: power for output, power in powers.items() if power is not None}
        return max(powers, key=powers.get) if powers else None

    def routing_errors(self) -> list[tuple[int, int, int | None]]:
        """(input, expected output, strongest output) for each misrouted planned line."""
        errors = []
        for source, expected in enumerate(self.expected):
            measured = self.strongest_output(source)
            if any(source in lines for lines in self.matrix.values()) and measured != expected:
                errors.append((source, expected, measured))
        return errors


@dataclass
class CompleteCalibration:
    """Filled step by step, so a stopped run still holds what was measured."""

    alignment: StateCheck | None = None
    switches: dict[str, MZICalibration] = field(default_factory=dict)
    checks: list[StateCheck] = field(default_factory=list)


def strongest_input(lines: dict[int, float | None]) -> int | None:
    """The input whose line is strongest in one output's line powers."""
    powers = {source: power for source, power in lines.items() if power is not None}
    return max(powers, key=powers.get) if powers else None


def wavelengths_from_assignment(assignment: dict) -> dict[int, float]:
    """{0-based input: nm} for the inputs with a comb line in a WDM plan assignment."""
    return {int(port) - 1: float(wavelength) for port, wavelength in assignment.items() if wavelength is not None}


def reference_states(network) -> dict[str, bool]:
    """Every MZI at 0 V, which is the fabricated cross state."""
    return dict.fromkeys(network.switches, True)


def switch_probes(network, states, name) -> tuple[SwitchProbe, ...]:
    """The two outputs whose arriving input changes with ``name``; others keep ``states``."""
    crossed = network.mapping({**states, name: True})
    straight = network.mapping({**states, name: False})
    return tuple(SwitchProbe(name, output, crossed.index(output), straight.index(output))
                 for output in range(network.size) if crossed.index(output) != straight.index(output))


def _lit_lines(probe, wavelengths) -> int:
    return (probe.cross_input in wavelengths) + (probe.straight_input in wavelengths)


def calibration_plan(network, states, wavelengths, names=None) -> list[SwitchProbe]:
    """Choose one probe per MZI, grouped by output so the JDSU moves as little as possible.

    A probe that sees both of the MZI's lines is preferred. Within a group the
    output-side MZIs come first.
    """
    names = list(network.switches if names is None else names)
    options = {}
    for name in names:
        probes = switch_probes(network, states, name)
        best = max(_lit_lines(probe, wavelengths) for probe in probes)
        if not best:
            inputs = sorted({probe.cross_input + 1 for probe in probes})
            raise ValueError(f"No laser line reaches {name}; assign a comb wavelength to input "
                             f"{' or '.join(map(str, inputs))} in the WDM comb tab")
        options[name] = [probe for probe in probes if _lit_lines(probe, wavelengths) == best]
    plan, remaining = [], names
    while remaining:
        counts = Counter(probe.output for name in remaining for probe in options[name])
        output = min(counts, key=lambda candidate: (-counts[candidate], candidate))
        group = sorted((name for name in remaining if any(probe.output == output for probe in options[name])),
                       key=lambda name: (-network.switches[name][0], name))
        plan.extend(next(probe for probe in options[name] if probe.output == output) for name in group)
        remaining = [name for name in remaining if name not in group]
    return plan


def line_powers(wave, power, wavelengths, half_window_nm) -> dict[int, float | None]:
    """Peak dBm of each input's comb line in a trimmed OSA trace."""
    lines = {}
    for source, wavelength in wavelengths.items():
        line = line_power(wave, power, wavelength, half_window_nm)
        lines[source] = None if line is None else line.peak_dbm
    return lines


def _check_stop(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise CalibrationStopped("Calibration stopped")


def _wait(seconds, stop_event, sleep) -> None:
    # Short steps keep Stop responsive during long settling delays.
    while seconds > 0:
        _check_stop(stop_event)
        step = min(0.1, seconds)
        sleep(step)
        seconds -= step


def _select(switch, output) -> None:
    if switch.routed_output != output + 1:
        switch.select_output(output + 1)


def _read(read_spectrum, output) -> tuple[list, list]:
    wave, power = trim_spurious_tail(*read_spectrum(output + 1))
    if not wave:
        raise ValueError("The OSA returned an empty spectrum")
    if not all(math.isfinite(value) for value in (*wave, *power)):
        raise ValueError("The OSA returned non-finite spectrum values")
    return wave, power


def extract_calibration(probe, points, tie_db=EXTREME_TIE_DB) -> MZICalibration:
    """Cross is the strongest cross-state contrast, straight the weakest.

    A sweep past 2π reaches a second maximum or null; any point within
    ``tie_db`` of an extreme counts as that extreme, and the lowest voltage wins.
    """
    measured = [point for point in points if point.contrast_db is not None]
    if not measured:
        raise ValueError(f"{probe.name}: no comb line was found at output {probe.output + 1}; "
                         "check the wavelength plan and line window")
    highest = max(point.contrast_db for point in measured)
    lowest = min(point.contrast_db for point in measured)
    cross = min((point for point in measured if point.contrast_db >= highest - tie_db),
                key=lambda point: (abs(point.voltage), -point.contrast_db))
    straight = min((point for point in measured if point.contrast_db <= lowest + tie_db),
                   key=lambda point: (abs(point.voltage), point.contrast_db))
    return MZICalibration(probe, tuple(points), cross, straight)


def sweep_switch(controller, switch, read_spectrum, probe, voltages, settle_s, wavelengths,
                 half_window_nm, on_point=None, stop_event=None, sleep=time.sleep) -> MZICalibration:
    """Sweep one MZI while the OSA watches ``probe.output``; the MZI stays at the last voltage.

    ``read_spectrum(output)`` takes a 1-based output and must return a trace
    that started after the last voltage change.
    """
    _select(switch, probe.output)
    points = []
    for voltage in voltages:
        _check_stop(stop_event)
        controller.drive_voltage(probe.name, voltage)
        _wait(settle_s, stop_event, sleep)
        lines = line_powers(*_read(read_spectrum, probe.output), wavelengths, half_window_nm)
        point = ProbePoint(voltage, lines.get(probe.cross_input), lines.get(probe.straight_input))
        points.append(point)
        if on_point is not None:
            on_point(probe, len(points), len(voltages), point)
    return extract_calibration(probe, points)


def evaluate_switches(network, states, matrix) -> dict[str, SwitchCheck]:
    """Grade each MZI from line powers {output: {input: dBm}}, assuming the others are in ``states``."""
    checks = {}
    for name in network.switches:
        margins = []
        for probe in switch_probes(network, states, name):
            expected, other = probe.expected_input(states[name]), probe.expected_input(not states[name])
            lines = matrix.get(probe.output, {})
            wanted, leaked = lines.get(expected), lines.get(other)
            margins.append((probe.output, expected, other,
                            None if wanted is None or leaked is None else wanted - leaked))
        checks[name] = SwitchCheck(name, states[name], tuple(margins))
    return checks


def check_states(network, states, switch, read_spectrum, wavelengths, half_window_nm, label="",
                 on_output=None, stop_event=None) -> StateCheck:
    """Read every output with the board already set to ``states`` and grade each MZI."""
    # Start at the output the JDSU already selects to save one move.
    outputs = sorted(range(network.size), key=lambda output: output + 1 != switch.routed_output)
    readouts = []
    for index, output in enumerate(outputs, start=1):
        _check_stop(stop_event)
        _select(switch, output)
        wave, power = _read(read_spectrum, output)
        readout = OutputReadout(output, tuple(wave), tuple(power),
                                line_powers(wave, power, wavelengths, half_window_nm))
        readouts.append(readout)
        if on_output is not None:
            on_output(readout, index, len(outputs))
    readouts.sort(key=lambda readout: readout.output)
    matrix = {readout.output: readout.lines for readout in readouts}
    return StateCheck(label, dict(states), network.mapping(states), tuple(readouts),
                      evaluate_switches(network, states, matrix))


def _require_ready(controller, network, voltages=None, settle_s=0.0) -> None:
    if not controller.connected:
        raise RuntimeError("Connect the voltage board before calibrating")
    for name in network.switches:
        if controller.settings["mapping"].get(name) is None:
            raise ValueError(f"Assign a voltage-board pin to {name} before calibrating")
    if voltages is not None and not voltages:
        raise ValueError("Choose at least one calibration voltage")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be a nonnegative number")


def reset_all(controller, network) -> None:
    """Send 0 V to every MZI, trying all of them even if one fails."""
    error = None
    for name in network.switches:
        try:
            controller.drive_voltage(name, 0.0)
        except Exception as exc:
            error = exc
    if error is not None:
        raise RuntimeError(f"Could not reset every MZI to 0 V: {error}") from error


def check_alignment(controller, switch, read_spectrum, network, wavelengths, half_window_nm,
                    settle_s=0.0, on_output=None, stop_event=None, sleep=time.sleep) -> StateCheck:
    """With every MZI at 0 V, check that each line leaves at its all-cross output."""
    _require_ready(controller, network, settle_s=settle_s)
    try:
        reset_all(controller, network)
        _wait(settle_s, stop_event, sleep)
        return check_states(network, reference_states(network), switch, read_spectrum, wavelengths,
                            half_window_nm, "Alignment (all 0 V)", on_output, stop_event)
    finally:
        reset_all(controller, network)


def check_calibrated_states(controller, switch, read_spectrum, network, states, wavelengths, half_window_nm,
                            label, settle_s=0.0, on_output=None, stop_event=None,
                            sleep=time.sleep) -> StateCheck:
    """Apply the saved voltages for ``states``, then read every output; the states stay applied."""
    _require_ready(controller, network, settle_s=settle_s)
    calibration = controller.settings["calibration"]
    missing = [name for name in network.switches
               if calibration[name]["cross" if states[name] else "straight"] is None]
    if missing:
        raise ValueError(f"Calibrate {', '.join(missing)} before checking these states")
    controller.apply_calibrated_states(states)
    _wait(settle_s, stop_event, sleep)
    return check_states(network, states, switch, read_spectrum, wavelengths, half_window_nm,
                        label, on_output, stop_event)


def calibrate_switch(controller, switch, read_spectrum, network, name, voltages, settle_s, wavelengths,
                     half_window_nm, output=None, save=True, on_probe=None, on_point=None,
                     stop_event=None, sleep=time.sleep) -> MZICalibration:
    """Sweep one MZI with every other MZI at 0 V, then return all of them to 0 V.

    ``output`` is the 0-based output to watch; by default the plan picks one
    that sees both of the MZI's lines.
    """
    _require_ready(controller, network, voltages, settle_s)
    states = reference_states(network)
    if output is None:
        probe = calibration_plan(network, states, wavelengths, [name])[0]
    else:
        probe = next((probe for probe in switch_probes(network, states, name) if probe.output == output), None)
        if probe is None:
            raise ValueError(f"{name} does not steer light to output {output + 1} while the others are at 0 V")
        if not _lit_lines(probe, wavelengths):
            raise ValueError(f"Neither input {probe.cross_input + 1} nor {probe.straight_input + 1} "
                             "has a comb wavelength in the WDM plan")
    try:
        reset_all(controller, network)
        if on_probe is not None:
            on_probe(probe)
        result = sweep_switch(controller, switch, read_spectrum, probe, voltages, settle_s, wavelengths,
                              half_window_nm, on_point, stop_event, sleep)
        if save:
            controller.set_calibration(name, result.straight.voltage, result.cross.voltage)
        return result
    finally:
        reset_all(controller, network)


def calibrate_all(controller, switch, read_spectrum, network, voltages, settle_s, wavelengths, half_window_nm,
                  verify=True, confirm_alignment=None, on_check=None, on_output=None, on_probe=None,
                  on_point=None, on_switch=None, stop_event=None, result=None,
                  sleep=time.sleep) -> CompleteCalibration:
    """Alignment check, one sweep per MZI, then optional calibrated all-cross/all-bar checks.

    Every sweep starts with all MZIs at 0 V, and every MZI is back at 0 V when
    this returns or fails. ``confirm_alignment(check)`` may stop the run after
    the alignment readout.
    """
    _require_ready(controller, network, voltages, settle_s)
    plan = calibration_plan(network, reference_states(network), wavelengths)
    result = CompleteCalibration() if result is None else result
    try:
        reset_all(controller, network)
        _wait(settle_s, stop_event, sleep)
        result.alignment = check_states(network, reference_states(network), switch, read_spectrum, wavelengths,
                                        half_window_nm, "Alignment (all 0 V)", on_output, stop_event)
        if on_check is not None:
            on_check(result.alignment)
        if confirm_alignment is not None and not confirm_alignment(result.alignment):
            raise CalibrationStopped("Calibration stopped after the alignment check")
        for probe in plan:
            if on_probe is not None:
                on_probe(probe)
            calibration = sweep_switch(controller, switch, read_spectrum, probe, voltages, settle_s,
                                       wavelengths, half_window_nm, on_point, stop_event, sleep)
            controller.drive_voltage(probe.name, 0.0)
            controller.set_calibration(probe.name, calibration.straight.voltage, calibration.cross.voltage)
            result.switches[probe.name] = calibration
            if on_switch is not None:
                on_switch(calibration)
        if verify:
            for label, crossed in (("All cross (calibrated)", True), ("All bar (calibrated)", False)):
                check = check_calibrated_states(controller, switch, read_spectrum, network,
                                                dict.fromkeys(network.switches, crossed), wavelengths,
                                                half_window_nm, label, settle_s, on_output, stop_event, sleep)
                result.checks.append(check)
                if on_check is not None:
                    on_check(check)
    finally:
        reset_all(controller, network)
    return result


class SimulatedChip:
    """Dry-run optics: cos² MZIs, cross at 0 V, whose heater phase grows with V².

    Each input's line is followed through the network separately, so the
    simulated OSA sees every MZI's finite extinction as leakage.
    """

    def __init__(self, network, voltage: Callable[[str], float], wavelengths, seed=7):
        rng = random.Random(seed)
        self.network = network
        self.voltage = voltage
        self.wavelengths = dict(wavelengths)
        self.v_pi = {name: rng.uniform(2.8, 4.3) for name in network.switches}
        self.phase = {name: rng.uniform(-0.3, 0.3) for name in network.switches}
        self.input_dbm = {source: rng.uniform(-11.0, -7.0) for source in range(network.size)}

    def cross_fraction(self, name) -> float:
        phase = math.pi * (self.voltage(name) / self.v_pi[name]) ** 2 + self.phase[name]
        return min(0.998, max(0.002, math.cos(phase / 2) ** 2))

    def output_mw(self) -> dict[int, dict[int, float]]:
        """Power of each input's line at each output."""
        result = {output: {} for output in range(self.network.size)}
        for source in range(self.network.size):
            rows = [0.0] * self.network.size
            rows[source] = 10 ** (self.input_dbm[source] / 10)
            for stage, pairs in enumerate(self.network.pairs):
                for index, (a, b) in enumerate(pairs):
                    cross = self.cross_fraction(f"S{stage + 1}.{index + 1}")
                    rows[a], rows[b] = ((1 - cross) * rows[a] + cross * rows[b],
                                        (1 - cross) * rows[b] + cross * rows[a])
                linked = [0.0] * self.network.size
                for row, power in enumerate(rows):
                    linked[self.network.links[stage][row]] = power
                rows = linked
            for output, power in enumerate(rows):
                result[output][source] = power
        return result

    def spectrum(self, output, points=4001) -> tuple[list, list]:
        """Trace at a 1-based output: every planned line over a -70 dBm floor."""
        powers = self.output_mw()[int(output) - 1]
        lines = [(wavelength, powers[source]) for source, wavelength in self.wavelengths.items()]
        centers = [wavelength for wavelength, _ in lines] or [1550.0]
        start, stop = min(centers) - 2, max(centers) + 2
        wave = [start + index * (stop - start) / (points - 1) for index in range(points)]
        power = [10 * math.log10(1e-7 + sum(peak / (1 + ((wavelength - center) / 0.02) ** 2)
                                            for center, peak in lines))
                 for wavelength in wave]
        return wave, power


def create_calibration_folder(project_dir: Path, kind: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder = Path(project_dir) / "Results" / CALIBRATION_SUBFOLDER / f"{stamp}_{kind}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _number(value, digits=3):
    return "" if value is None else f"{value:.{digits}f}"


def _check_record(check: StateCheck, pass_db: float) -> dict:
    return {
        "label": check.label,
        "switch_states": {name: "cross" if crossed else "bar" for name, crossed in check.states.items()},
        "expected_output_per_input": [output + 1 for output in check.expected],
        "routing_errors": [{"input": source + 1, "expected_output": expected + 1,
                            "strongest_output": None if measured is None else measured + 1}
                           for source, expected, measured in check.routing_errors()],
        "line_peak_dbm": {str(output + 1): {str(source + 1): power for source, power in lines.items()}
                          for output, lines in check.matrix.items()},
        "switches": {name: {"state": "cross" if item.crossed else "bar", "margin_db": item.margin_db,
                            "verdict": item.verdict(pass_db),
                            "outputs": [{"output": output + 1, "expected_input": expected + 1,
                                         "other_input": other + 1, "margin_db": margin}
                                        for output, expected, other, margin in item.margins]}
                     for name, item in check.switches.items()},
    }


def save_calibration_results(folder: Path, context: dict, calibrations: dict, checks: list,
                             pass_db: float = DEFAULT_PASS_DB) -> list[str]:
    """Write the run summary, MZI sweeps, output readouts and their spectra into ``folder``."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    comment = context.get("comment", "")
    header = [f"# Six-laser MZI calibration saved {datetime.now().isoformat(timespec='seconds')}\n"]
    header += [f"# Comment: {line}\n" for line in comment.splitlines() or [""]]
    files = ["mzi_calibration.json"]
    record = {
        **context,
        "pass_margin_db": pass_db,
        "mzi_calibrations": {
            name: {"watched_output": item.probe.output + 1,
                   "watched_output_fiber": fiber_number(item.probe.output + 1),
                   "cross_input": item.probe.cross_input + 1, "straight_input": item.probe.straight_input + 1,
                   "cross_voltage_v": item.cross.voltage, "straight_voltage_v": item.straight.voltage,
                   "cross_margin_db": item.cross_margin_db, "straight_margin_db": item.straight_margin_db}
            for name, item in calibrations.items()},
        "state_checks": [_check_record(check, pass_db) for check in checks],
    }
    (folder / "mzi_calibration.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if calibrations:
        files.append("mzi_sweeps.csv")
        with (folder / "mzi_sweeps.csv").open("w", encoding="utf-8", newline="") as file:
            file.writelines(header)
            file.write("# Peak power of the line that reaches the watched output in each MZI state\n")
            writer = csv.writer(file)
            writer.writerow(["switch", "watched_output", "cross_input", "straight_input", "voltage_v",
                             "cross_line_dbm", "straight_line_dbm", "contrast_db"])
            for name, item in calibrations.items():
                for point in item.points:
                    writer.writerow([name, item.probe.output + 1, item.probe.cross_input + 1,
                                     item.probe.straight_input + 1, point.voltage, _number(point.cross_dbm),
                                     _number(point.straight_dbm), _number(point.contrast_db)])
    if checks:
        files += ["output_readouts.csv", "readout_spectra.csv"]
        with (folder / "output_readouts.csv").open("w", encoding="utf-8", newline="") as file:
            file.writelines(header)
            writer = csv.writer(file)
            writer.writerow(["check", "output", "output_fiber", "input", "input_fiber", "expected_here",
                             "line_peak_dbm"])
            for check in checks:
                for readout in check.readouts:
                    for source, power in readout.lines.items():
                        writer.writerow([check.label, readout.output + 1, fiber_number(readout.output + 1),
                                         source + 1, input_fiber_number(source + 1),
                                         check.expected[source] == readout.output, _number(power)])
        write_spectra(folder / "readout_spectra.csv",
                      [(f"{check.label} · Out {readout.output + 1}", list(readout.wave), list(readout.power))
                       for check in checks for readout in check.readouts], comment)
    return files
