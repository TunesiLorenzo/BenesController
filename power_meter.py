"""Persistent Arduino detector reader built on ThorlabsControl's sampler."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import math
from pathlib import Path
from typing import Callable

from voltage_controller import app_dir, sibling_module_path


@dataclass(frozen=True)
class PowerReading:
    """A scalar detector result suitable for a future calibration routine."""

    value: float
    unit: str
    raw_mean: float
    sample_count: int


class ArduinoPowerMeter:
    """Connect to the free-running Arduino ADC and average a timed sample burst."""

    def __init__(self, config_path: Path | None = None, sampler_loader: Callable[[], object] | None = None):
        self.config_path = config_path or app_dir() / "benes_power_meter_config.json"
        self._sampler_loader = sampler_loader
        self.sampler = None
        self.serial = None
        self.settings = self._load()
        self.last_reading: PowerReading | None = None

    def _defaults(self) -> dict:
        return {
            "port": "COM3",
            "baud": 230400,
            "duration": 0.10,
            "scale": 1.0,
            "offset": 0.0,
            "unit": "ADC counts",
        }

    def _load(self) -> dict:
        settings = self._defaults()
        if not self.config_path.exists():
            return settings
        try:
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            self._validate_settings(stored)
            settings.update({name: stored[name] for name in settings if name in stored})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A bad meter configuration must not stop the route explorer opening.
            pass
        return settings

    @staticmethod
    def _validate_settings(settings: dict) -> None:
        if not str(settings.get("port", "")).strip():
            raise ValueError("Enter an Arduino serial port, for example COM3")
        if int(settings.get("baud", 0)) <= 0:
            raise ValueError("Arduino baud rate must be positive")
        if float(settings.get("duration", 0)) <= 0:
            raise ValueError("Sample duration must be positive")
        for name in ("scale", "offset"):
            if not math.isfinite(float(settings.get(name, 0))):
                raise ValueError(f"Arduino {name} must be finite")

    def configure(self, port: object, baud: object, duration: object, scale: object, offset: object, unit: object) -> None:
        settings = {
            "port": str(port).strip(),
            "baud": int(baud),
            "duration": float(duration),
            "scale": float(scale),
            "offset": float(offset),
            "unit": str(unit).strip() or "ADC counts",
        }
        self._validate_settings(settings)
        self.settings = settings
        self.save()

    def save(self) -> None:
        temp_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.config_path)

    @property
    def connected(self) -> bool:
        return self.serial is not None

    def connect(self) -> None:
        if self.connected:
            return
        self.sampler = (self._sampler_loader or self._load_sampler)()
        self.serial = self.sampler.open_arduino(port=self.settings["port"], baud=self.settings["baud"])

    def disconnect(self) -> None:
        if self.serial is not None:
            self.sampler.close_arduino(self.serial)
            self.serial = None

    def read_current_power(self) -> PowerReading:
        if self.serial is None:
            raise RuntimeError("Connect the Arduino power meter first")
        samples = self.sampler.burst_read_binary(self.serial, duration=self.settings["duration"])
        if not samples:
            raise RuntimeError("The Arduino returned no samples")
        raw_mean = sum(samples) / len(samples)
        reading = PowerReading(
            value=raw_mean * self.settings["scale"] + self.settings["offset"],
            unit=self.settings["unit"],
            raw_mean=raw_mean,
            sample_count=len(samples),
        )
        self.last_reading = reading
        return reading

    def calibration_input(self) -> PowerReading:
        """Return the latest scalar reading for a future MZI calibration loop."""
        if self.last_reading is None:
            raise RuntimeError("Read the Arduino power meter before starting calibration")
        return self.last_reading

    @staticmethod
    def _load_sampler() -> object:
        sampler_path = sibling_module_path("ThorlabsControl", "ArduinoSampler.py")
        if not sampler_path.exists():
            raise RuntimeError(f"ThorlabsControl Arduino sampler was not found at {sampler_path}")
        spec = importlib.util.spec_from_file_location("benes_thorlabs_arduino_sampler", sampler_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load ThorlabsControl's Arduino sampler")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
