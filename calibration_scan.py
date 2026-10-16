"""Measure a switch's power response while sweeping its drive voltage."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanPoint:
    voltage: float
    power: float


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
