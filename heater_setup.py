"""Read GDS_heater device tables and derive per-heater current limits."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

# Current-density limits for the heater and routing metal.
SAFE_CURRENT_DENSITY = 50.0   # mA/µm²
BREAK_CURRENT_DENSITY = 70.0  # mA/µm²


@dataclass(frozen=True)
class HeaterDevice:
    name: str
    r_total_ohm: float
    r_heater_ohm: float
    # Peak current density in the heater per applied volt; J = V / R_total / area is
    # linear in V, so a single coefficient fixes the voltage limits.
    j_peak_per_v: float


def voltage_limit(j_peak_per_v: float | None, density: float) -> float | None:
    """Voltage at which the worst peak current density reaches ``density``."""
    if not j_peak_per_v:
        return None
    return density / j_peak_per_v


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _column(header: list[str], prefix: str) -> int | None:
    for index, title in enumerate(header):
        if title.strip().startswith(prefix):
            return index
    return None


def load_heater_csv(path) -> dict[str, HeaterDevice]:
    """Parse a GDS_heater ``mzi_results.csv`` into devices keyed by name.

    The second comment line holds the simulation voltage (``V [V],1.0``); the
    current densities in the table are for that voltage.
    """
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    reference_v = None
    for row in csv.reader(line.lstrip("#").strip() for line in lines if line.startswith("#")):
        for key, value in zip(row[::2], row[1::2]):
            if key.strip() == "V [V]":
                reference_v = _number(value)
    rows = list(csv.reader(line for line in lines if line.strip() and not line.startswith("#")))
    if not rows:
        raise ValueError(f"{Path(path).name} has no device table")
    header = rows[0]
    columns = {key: _column(header, prefix) for key, prefix in (
        ("name", "Device"), ("total", "R total"), ("heater", "R heater"), ("current", "I [mA]"),
        ("j_peak", "J peak"))}
    missing = [key for key in ("name", "total", "heater", "j_peak") if columns[key] is None]
    if missing:
        raise ValueError(f"{Path(path).name} is missing the Device, R total, R heater or J peak column")

    devices = {}
    for row in rows[1:]:
        cell = {key: row[index] if index is not None and index < len(row) else "" for key, index in columns.items()}
        name = cell["name"].strip()
        r_total, r_heater = _number(cell["total"]), _number(cell["heater"])
        if not name or not r_total or not r_heater or r_total <= 0 or r_heater <= 0:
            continue
        # Without the header voltage, recover it from the tabulated current.
        current = _number(cell["current"])
        voltage = reference_v or (current * r_total / 1000 if current else None)
        # The limits apply to the heater, so the routing peak density is not used.
        density = _number(cell["j_peak"])
        if not voltage or not density:
            continue
        devices[name] = HeaterDevice(name, r_total, r_heater, density / voltage)
    if not devices:
        raise ValueError(f"{Path(path).name} contains no complete device rows")
    return devices
