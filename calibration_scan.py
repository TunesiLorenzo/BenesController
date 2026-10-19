"""Measure a switch's power response while sweeping its drive voltage."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from itertools import combinations

import osa


@dataclass(frozen=True)
class ScanPoint:
    voltage: float
    power: float


@dataclass(frozen=True)
class SpectrumScanPoint(ScanPoint):
    wavelength_nm: tuple[float, ...]
    spectrum_dbm: tuple[float, ...]
    mean_power_dbm: float = field(init=False)

    def __post_init__(self):
        if not self.spectrum_dbm:
            raise ValueError("The OSA returned an empty spectrum")
        peak = max(self.spectrum_dbm)
        relative_mean = sum(10 ** ((power - peak) / 10) for power in self.spectrum_dbm) / len(self.spectrum_dbm)
        object.__setattr__(self, "mean_power_dbm", peak + 10 * math.log10(relative_mean))


@dataclass(frozen=True)
class PathCalibration:
    """One MZI's response and the state voltages inferred from a zero-V path."""

    name: str
    points: tuple[ScanPoint, ...]
    cross: ScanPoint
    straight: ScanPoint


def zero_voltage_paths(network):
    """Return the physical paths when every MZI is held at its 0 V cross state."""
    states = dict.fromkeys(network.switches, True)
    return tuple(network.trace(source, states) for source in range(network.size))


def staged_reachable_switches(network, input_port: int, output_port: int):
    """Return MZIs reachable when known MZIs may steer and unknown ones stay cross."""
    known = set()
    changed = True
    while changed:
        changed = False
        for route in network.routes(input_port, output_port):
            states = dict(route.states)
            # A switch we have not calibrated cannot be driven bar yet, so it
            # must retain its 0 V cross state on the candidate route.
            if all(states[name] or name in known for name in states):
                added = {name for name, crossed in route.states if crossed and name not in known}
                if added:
                    known.update(added)
                    changed = True
    return frozenset(known)


def minimum_staged_calibration_pairs(network):
    """Find a smallest I/O-pair set when calibrated MZIs can steer later paths.

    Each candidate is `(input_row, output_row, switch_names)`, using zero-based
    port rows.  An input's matching zero-voltage output is used for the initial
    route, then already-calibrated MZIs may be set bar to reach its alternate
    subnetwork.  The exhaustive search is deliberately tiny (six paths here).
    """
    candidates = tuple(
        (source, route.rows[-1], tuple(sorted(staged_reachable_switches(network, source, route.rows[-1]))))
        for source, route in enumerate(zero_voltage_paths(network))
    )
    required = set(network.switches)
    for count in range(1, len(candidates) + 1):
        for selection in combinations(candidates, count):
            if set().union(*(set(names) for _, _, names in selection)) == required:
                return selection
    raise RuntimeError("No set of zero-voltage paths covers every MZI")


def minimum_default_cross_pairs(network):
    """Find a smallest I/O-pair set that covers MZIs without heated steering."""
    candidates = tuple(
        (source, route.rows[-1], tuple(name for name, _ in route.states))
        for source, route in enumerate(zero_voltage_paths(network))
    )
    required = set(network.switches)
    for count in range(1, len(candidates) + 1):
        for selection in combinations(candidates, count):
            if set().union(*(set(names) for _, _, names in selection)) == required:
                return selection
    raise RuntimeError("No set of default-cross paths covers every MZI")


def _read_averaged_power(power_meter, averages: int) -> float:
    values = [power_meter.read_current_power().value for _ in range(averages)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("The power meter returned a non-finite reading")
    return sum(values) / len(values)


def calibrate_default_cross_path(controller, power_meter, network, input_port: int, output_port: int,
                                 voltages: list[float], settle_s: float, averages: int = 1,
                                 on_point=None, on_switch=None):
    """Calibrate one all-cross I/O path, from its output-side MZI backwards.

    No other switch is ever deliberately heated.  Every MZI is first driven to
    0 V; the target alone is swept, then reset to 0 V before the next target.
    """
    if not controller.connected or controller.settings["dry_run"]:
        raise RuntimeError("Connect the physical voltage board before calibrating")
    if not power_meter.connected:
        raise RuntimeError("Connect the Arduino power meter before calibrating")
    if not 0 <= input_port < network.size or not 0 <= output_port < network.size:
        raise ValueError("Input and output ports must be between 1 and 6")
    if not voltages:
        raise ValueError("Choose at least one calibration voltage")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be a nonnegative number")
    if not isinstance(averages, int) or not 1 <= averages <= 100:
        raise ValueError("Averages must be an integer from 1 to 100")

    route = zero_voltage_paths(network)[input_port]
    if route.rows[-1] != output_port:
        raise ValueError(
            f"Input {input_port + 1} reaches output {route.rows[-1] + 1} when every MZI is at 0 V; "
            "select that output for default-cross calibration"
        )
    names = tuple(name for name, _ in reversed(route.states))
    for name in network.switches:
        if controller.settings["mapping"].get(name) is None:
            raise ValueError(f"Assign a voltage-board pin to {name} before path calibration")

    results = {}
    reset_error = None
    try:
        for name in network.switches:
            controller.drive_voltage(name, 0.0)
        for name in names:
            points = []
            try:
                for voltage in voltages:
                    controller.drive_voltage(name, voltage)
                    time.sleep(settle_s)
                    point = ScanPoint(voltage, _read_averaged_power(power_meter, averages))
                    points.append(point)
                    if on_point is not None:
                        on_point(name, len(points), len(voltages), point)
                cross = max(points, key=lambda point: (point.power, -abs(point.voltage)))
                straight = min(points, key=lambda point: (point.power, abs(point.voltage)))
                controller.set_calibration(name, straight.voltage, cross.voltage)
                result = PathCalibration(name, tuple(points), cross, straight)
                results[name] = result
                if on_switch is not None:
                    on_switch(result)
            finally:
                controller.drive_voltage(name, 0.0)
    finally:
        for name in network.switches:
            try:
                controller.drive_voltage(name, 0.0)
            except Exception as error:
                reset_error = error
        if reset_error is not None:
            raise RuntimeError(f"Could not reset every MZI to 0 V: {reset_error}") from reset_error
    return results


def _next_staged_calibration_route(network, input_port, output_port, calibrated):
    """Choose the next cross-state MZI, favouring the one nearest the output."""
    candidates = []
    for route in network.routes(input_port, output_port):
        states = dict(route.states)
        if not all(states[name] or name in calibrated for name in states):
            continue
        unknown = [(name, index) for index, (name, crossed) in enumerate(route.states)
                   if crossed and name not in calibrated]
        if not unknown:
            continue
        # Route states are ordered input to output; the greatest index is the
        # output-side candidate.  Prefer a route needing fewer heated steering
        # MZIs when stages tie.
        target, stage = max(unknown, key=lambda item: item[1])
        steering = tuple(name for name, crossed in route.states if name in calibrated and not crossed)
        candidates.append((stage, -len(steering), target, route, steering))
    if not candidates:
        return None
    _, _, target, route, steering = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    return target, route, steering


def calibrate_zero_voltage_path(controller, power_meter, network, input_port: int, output_port: int,
                                voltages: list[float], settle_s: float, on_point=None, on_switch=None):
    """Calibrate a fixed I/O pair, using calibrated bars to reach alternate paths.

    Before the first read and after every completed/failing run, every mapped
    MZI is sent to 0 V. The initial route is all-cross. Later routes can set
    only already-calibrated MZIs bar, so unneeded switches remain unbiased.
    The maximum at the selected output is stored as ``cross`` and the minimum
    as ``straight``. The routine never leaves a scanned MZI heated.
    """
    if not controller.connected or controller.settings["dry_run"]:
        raise RuntimeError("Connect the physical voltage board before calibrating")
    if not power_meter.connected:
        raise RuntimeError("Connect the Arduino power meter before calibrating")
    if not 0 <= input_port < network.size or not 0 <= output_port < network.size:
        raise ValueError("Input and output ports must be between 1 and 6")
    if not voltages:
        raise ValueError("Choose at least one calibration voltage")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be a nonnegative number")

    route = zero_voltage_paths(network)[input_port]
    if route.rows[-1] != output_port:
        raise ValueError(
            f"Input {input_port + 1} reaches output {route.rows[-1] + 1} when every MZI is at 0 V; "
            "select that output for a zero-crosstalk calibration path"
        )
    for name in network.switches:
        if controller.settings["mapping"].get(name) is None:
            raise ValueError(f"Assign a voltage-board pin to {name} before path calibration")

    results = {}
    reset_error = None
    try:
        calibrated = set()
        while True:
            next_route = _next_staged_calibration_route(network, input_port, output_port, calibrated)
            if next_route is None:
                break
            name, route, steering = next_route
            # Start every individual sweep thermally quiet. Only the known
            # bars necessary to route light to this target are enabled.
            for switch in network.switches:
                controller.drive_voltage(switch, 0.0)
            for switch in steering:
                voltage = controller.settings["calibration"][switch]["straight"]
                if voltage is None:
                    raise RuntimeError(f"{switch} has no saved straight voltage for path steering")
                controller.drive_voltage(switch, voltage)
            points = []
            for voltage in voltages:
                controller.drive_voltage(name, voltage)
                time.sleep(settle_s)
                reading = power_meter.read_current_power()
                if not math.isfinite(reading.value):
                    raise ValueError("The power meter returned a non-finite reading")
                point = ScanPoint(voltage, reading.value)
                points.append(point)
                if on_point is not None:
                    on_point(name, len(points), len(voltages), point)
            # At the selected 0 V path, the cross state is the high-throughput
            # state; prefer the voltage closest to zero when samples tie.
            cross = max(points, key=lambda point: (point.power, -abs(point.voltage)))
            straight = min(points, key=lambda point: (point.power, abs(point.voltage)))
            controller.set_calibration(name, straight.voltage, cross.voltage)
            result = PathCalibration(name, tuple(points), cross, straight)
            results[name] = result
            calibrated.add(name)
            if on_switch is not None:
                on_switch(result)
            controller.drive_voltage(name, 0.0)
        expected = staged_reachable_switches(network, input_port, output_port)
        if set(results) != expected:
            missing = ", ".join(sorted(expected - set(results)))
            raise RuntimeError(f"Could not reach every staged-path MZI: {missing}")
    finally:
        for name in network.switches:
            try:
                controller.drive_voltage(name, 0.0)
            except Exception as error:
                reset_error = error
        if reset_error is not None:
            raise RuntimeError(f"Could not reset every MZI to 0 V: {reset_error}") from reset_error
    return results


def spectrum_scan_point(voltage: float, spectrum) -> SpectrumScanPoint:
    wavelength, power = osa.trim_spurious_tail(*spectrum)
    if not wavelength or len(wavelength) != len(power):
        raise ValueError("The OSA returned an empty or mismatched spectrum")
    if not all(math.isfinite(value) for value in wavelength) or not all(math.isfinite(value) for value in power):
        raise ValueError("The OSA returned non-finite spectrum values")
    return SpectrumScanPoint(voltage, max(power), tuple(wavelength), tuple(power))


def scan_voltages(start: float, stop: float, step: float) -> list[float]:
    values = (float(start), float(stop), float(step))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Scan voltages must be finite")
    start, stop, step = values
    if not -10 <= start < stop <= 10:
        raise ValueError("Enter a range within -10 to +10 V, with start below stop")
    if step <= 0:
        raise ValueError("Step must be positive")
    count = math.ceil((stop - start) / step)
    if count > 200:
        raise ValueError("Choose a larger step (at most 201 scan points)")
    voltages = [round(start + index * step, 6) for index in range(count)]
    if not voltages or voltages[-1] != stop:
        voltages.append(stop)
    return voltages


def scan_switch(controller, power_meter, name: str, voltages: list[float], settle_s: float,
                on_point=None) -> list[ScanPoint]:
    """Sweep one mapped switch and restore its previous voltage on exit."""
    if not controller.connected or controller.settings["dry_run"]:
        raise RuntimeError("Connect the physical voltage board before scanning")
    if not power_meter.connected:
        raise RuntimeError("Connect the Arduino power meter before scanning")
    if controller.settings["mapping"][name] is None:
        raise ValueError(f"Assign a pin to {name} before scanning")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be a nonnegative number")
    previous = controller.applied_voltages[name]
    points = []
    try:
        for voltage in voltages:
            controller.drive_voltage(name, voltage)
            time.sleep(settle_s)
            reading = power_meter.read_current_power()
            if not math.isfinite(reading.value):
                raise ValueError("The power meter returned a non-finite reading")
            point = ScanPoint(voltage, reading.value)
            points.append(point)
            if on_point is not None:
                on_point(len(points), len(voltages), point)
    finally:
        controller.drive_voltage(name, previous)
    return points


def scan_switches(controller, power_meter, names: list[str], voltages: list[float],
                  settle_s: float, on_point=None) -> dict[str, list[ScanPoint]]:
    """Scan selected switches with Arduino burst averages."""
    if not power_meter.connected:
        raise RuntimeError("Connect the Arduino power meter before scanning")

    def read_point(voltage):
        reading = power_meter.read_current_power()
        if not math.isfinite(reading.value):
            raise ValueError("The power meter returned a non-finite reading")
        return ScanPoint(voltage, reading.value)

    return scan_switches_with_reader(controller, names, voltages, settle_s, read_point, on_point)


def scan_switches_with_reader(controller, names: list[str], voltages: list[float],
                              settle_s: float, read_point, on_point=None):
    """Scan switches in order with a measurement reader; reset all to 0 V."""
    if not names:
        raise ValueError("Select at least one switch")
    if not controller.connected or controller.settings["dry_run"]:
        raise RuntimeError("Connect the physical voltage board before scanning")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("Settling time must be a nonnegative number")
    for name in names:
        if name not in controller.settings["mapping"]:
            raise ValueError(f"Unknown switch: {name}")
        if controller.settings["mapping"][name] is None:
            raise ValueError(f"Assign a pin to {name} before scanning")

    results = {}
    reset_names = set()
    try:
        for name in names:
            points = []
            try:
                for voltage in voltages:
                    controller.drive_voltage(name, voltage)
                    time.sleep(settle_s)
                    point = read_point(voltage)
                    points.append(point)
                    if on_point is not None:
                        on_point(name, len(points), len(voltages), point)
            finally:
                controller.drive_voltage(name, 0.0)
                reset_names.add(name)
            results[name] = points
    finally:
        reset_error = None
        for name in names:
            if name not in reset_names:
                try:
                    controller.drive_voltage(name, 0.0)
                except Exception as error:
                    reset_error = error
        if reset_error is not None:
            raise RuntimeError(f"Could not reset all selected switches to 0 V: {reset_error}") from reset_error
    return results
