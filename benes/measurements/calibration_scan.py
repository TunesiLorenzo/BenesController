"""Measure a switch's power response while sweeping its drive voltage."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from benes.hardware import osa


@dataclass(frozen=True)
class ScanPoint:
    voltage: float
    power: float


@dataclass(frozen=True)
class SpectrumScanPoint(ScanPoint):
    wavelength_nm: tuple[float, ...]
    spectrum_dbm: tuple[float, ...]
    reference_wavelength_nm: float | None = None
    half_window_nm: float | None = None
    mean_power_dbm: float = field(init=False)

    def __post_init__(self):
        if not self.spectrum_dbm:
            raise ValueError("The OSA returned an empty spectrum")
        values = self.spectrum_dbm
        if self.reference_wavelength_nm is not None:
            if self.half_window_nm is None or not math.isfinite(self.half_window_nm) or self.half_window_nm <= 0:
                raise ValueError("Enter a positive wavelength window for the OSA reference line")
            values = tuple(power for wave, power in zip(self.wavelength_nm, self.spectrum_dbm)
                           if abs(wave - self.reference_wavelength_nm) <= self.half_window_nm)
            if not values:
                raise ValueError(f"No OSA bins near {self.reference_wavelength_nm:g} nm")
        peak = max(values)
        relative_mean = sum(10 ** ((power - peak) / 10) for power in values) / len(values)
        object.__setattr__(self, "mean_power_dbm", peak + 10 * math.log10(relative_mean))


def spectrum_scan_point(voltage: float, spectrum, reference_wavelength_nm: float | None = None,
                        half_window_nm: float | None = None) -> SpectrumScanPoint:
    wavelength, power = osa.trim_spurious_tail(*spectrum)
    if not wavelength or len(wavelength) != len(power):
        raise ValueError("The OSA returned an empty or mismatched spectrum")
    if not all(math.isfinite(value) for value in wavelength) or not all(math.isfinite(value) for value in power):
        raise ValueError("The OSA returned non-finite spectrum values")
    if reference_wavelength_nm is not None:
        if not math.isfinite(reference_wavelength_nm):
            raise ValueError("The OSA reference wavelength must be finite")
        if half_window_nm is None or not math.isfinite(half_window_nm) or half_window_nm <= 0:
            raise ValueError("Enter a positive wavelength window for the OSA reference line")
        selected = [value for wave, value in zip(wavelength, power)
                    if abs(wave - reference_wavelength_nm) <= half_window_nm]
        if not selected:
            raise ValueError(f"No OSA bins near {reference_wavelength_nm:g} nm")
        peak = max(selected)
    else:
        peak = max(power)
    return SpectrumScanPoint(voltage, peak, tuple(wavelength), tuple(power),
                             reference_wavelength_nm, half_window_nm)


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
