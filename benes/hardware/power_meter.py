"""Persistent Arduino detector reader built on ThorlabsControl's sampler."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import math
import time
from pathlib import Path
from typing import Callable

from benes.hardware.voltage_controller import app_dir, sibling_module_path


# Raw stream: 7-bit A0 byte (bit 7 clear) followed by a 7-bit A1 byte (bit 7 set).
# The Uno's 16 MHz clock divides exactly to both rates; match the sketch's Serial.begin.
RAW_BAUDS = (1_000_000, 2_000_000)
RAW_FULL_SCALE = 127
RAW_CHANNEL_TAG = 0x80
# The detector flattens out near 80 counts, well below the 7-bit full scale.
DEFAULT_CEILING = 80.0


@dataclass(frozen=True)
class PowerReading:
    """A scalar detector result suitable for a future calibration routine."""

    value: float
    unit: str
    raw_mean: float
    sample_count: int


class ArduinoPowerMeter:
    """Read Arduino ADC streams and average optical samples for calibration."""

    def __init__(self, config_path: Path | None = None, sampler_loader: Callable[[], object] | None = None):
        self.config_path = config_path or app_dir() / "benes_power_meter_config.json"
        self._sampler_loader = sampler_loader
        self.sampler = None
        self.serial = None
        self.settings = self._load()
        self.last_reading: PowerReading | None = None
        self._dual_buffer = bytearray()
        self._raw_tail = b""
        self._raw_running = False
        self._raw_index = 0
        self.stream_start_time = None
        self._raw_last_received = 0.0
        self._raw_skipped = 0

    def _defaults(self) -> dict:
        return {
            "port": "COM3",
            "baud": RAW_BAUDS[-1],
            "duration": 0.10,
            "scale": 1.0,
            "offset": 0.0,
            "unit": "ADC counts",
            "stream_format": "raw",
            "ceiling": DEFAULT_CEILING,
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
        ceiling = float(settings.get("ceiling", DEFAULT_CEILING))
        if not math.isfinite(ceiling) or ceiling <= 0:
            raise ValueError("The power-meter ceiling must be a positive ADC count")
        if settings.get("stream_format", "legacy") not in ("legacy", "dual", "raw"):
            raise ValueError("Arduino stream format must be raw, legacy, or dual")
        if settings.get("stream_format") == "raw" and int(settings["baud"]) not in RAW_BAUDS:
            raise ValueError("The raw A0/A1 sketch runs at 1000000 or 2000000 baud")

    def configure(self, port: object, baud: object, duration: object, scale: object, offset: object,
                  unit: object, stream_format: str = "legacy",
                  ceiling: object = DEFAULT_CEILING) -> None:
        settings = {
            "port": str(port).strip(),
            "baud": int(baud),
            "duration": float(duration),
            "scale": float(scale),
            "offset": float(offset),
            "unit": str(unit).strip() or "ADC counts",
            "stream_format": stream_format,
            "ceiling": float(ceiling),
        }
        self._validate_settings(settings)
        self.settings = settings
        self.save()

    def save(self) -> None:
        temp_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.config_path)

    @property
    def full_scale(self) -> int:
        """Largest raw ADC value the selected stream can report."""
        return RAW_FULL_SCALE if self.settings["stream_format"] == "raw" else 255

    @property
    def ceiling(self) -> float:
        """Raw ADC level at which the detector is treated as saturated."""
        return min(self.settings["ceiling"], self.full_scale)

    @property
    def connected(self) -> bool:
        return self.serial is not None

    def connect(self) -> None:
        if self.connected:
            return
        self.sampler = (self._sampler_loader or self._load_sampler)()
        self.serial = self.sampler.open_arduino(port=self.settings["port"], baud=self.settings["baud"])
        self._dual_buffer.clear()
        self._raw_tail = b""
        self._raw_running = False

    def disconnect(self) -> None:
        if self.serial is not None:
            try:
                if self._raw_running:
                    self.stop_stream()
            finally:
                self.sampler.close_arduino(self.serial)
                self.serial = None

    def read_current_power(self) -> PowerReading:
        if self.serial is None:
            raise RuntimeError("Connect the Arduino power meter first")
        if self.settings["stream_format"] in ("dual", "raw"):
            try:
                samples, _ = self.read_timed_samples(self.settings["duration"])
            finally:
                if self.settings["stream_format"] == "raw":
                    self.stop_stream()
            samples = [optical for optical, _electrical in samples]
        else:
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

    @staticmethod
    def _decode_dual_frames(buffer: bytearray) -> list[tuple[int, int]]:
        pairs = []
        index = 0
        while len(buffer) - index >= 6:
            if (buffer[index] == 0xA5 and buffer[index + 1] == 0x5A
                    and buffer[index + 5] == (buffer[index + 2] ^ buffer[index + 3]
                                             ^ buffer[index + 4] ^ 0xFF)):
                pairs.append((buffer[index + 2], buffer[index + 3]))
                index += 6
            else:
                index += 1
        del buffer[:index]
        return pairs

    @staticmethod
    def _decode_tagged_pairs(data: bytes) -> tuple[list[tuple[int, int]], bytes, int]:
        """Pair each untagged A0 byte with a following tagged A1 byte.

        Returns the pairs, an unfinished trailing A0 byte, and how many bytes
        were skipped to resynchronize.
        """
        pairs = []
        index = 0
        skipped = 0
        end = len(data) - 1
        while index < end:
            optical, electrical = data[index], data[index + 1]
            if not optical & RAW_CHANNEL_TAG and electrical & RAW_CHANNEL_TAG:
                pairs.append((optical, electrical & ~RAW_CHANNEL_TAG))
                index += 2
            else:
                skipped += 1
                index += 1
        tail = data[index:]
        if tail and tail[0] & RAW_CHANNEL_TAG:
            # A lone A1 byte can never start a pair.
            skipped += 1
            tail = b""
        return pairs, bytes(tail), skipped

    def read_timed_samples(self, duration: float, reset_buffer: bool = True):
        """Return optical/electrical pairs with measured-rate or host chunk timing."""
        if self.serial is None:
            raise RuntimeError("Connect the Arduino power meter first")
        if self.settings["stream_format"] == "raw":
            return self._read_raw_samples(duration, reset_buffer)
        if self.settings["stream_format"] != "dual":
            raise RuntimeError("Select the dual A0/A1 Arduino stream for impulse tests")
        if reset_buffer:
            self._dual_buffer.clear()
        raw, timing = self.sampler.burst_read_binary(
            self.serial, duration=duration, reset_buffer=reset_buffer, return_timing=True,
        )
        self._dual_buffer.extend(raw)
        return self._decode_dual_frames(self._dual_buffer), timing

    def stop_stream(self) -> None:
        """Stop collecting; the Arduino keeps streaming and the PC discards it."""
        self._raw_running = False
        self._raw_tail = b""

    def start_stream(self) -> None:
        """Begin a capture at the newest bytes; channel tags restore the pairing."""
        if self.serial is None or self.settings["stream_format"] != "raw":
            raise RuntimeError("Connect the raw A0/A1 Arduino stream first")
        self.serial.reset_input_buffer()
        self._raw_tail = b""
        self._raw_index = 0
        self._raw_skipped = 0
        self.stream_start_time = time.perf_counter()
        self._raw_running = True
        self._raw_last_received = self.stream_start_time

    def _read_raw_samples(self, duration: float, restart: bool):
        """Decode tagged pairs; retain only an unfinished pair across reads."""
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Sample duration must be positive and finite")
        if restart or not self._raw_running:
            self.start_stream()
        deadline = time.perf_counter() + duration
        data = bytearray(self._raw_tail)
        while time.perf_counter() < deadline:
            waiting = self.serial.in_waiting
            if waiting:
                data.extend(self.serial.read(waiting))
                self._raw_last_received = time.perf_counter()
            else:
                time.sleep(0.0005)
        if time.perf_counter() - self._raw_last_received > 0.5:
            raise RuntimeError("Raw Arduino stream stopped; check the connection, baud rate, and sketch")
        pairs, self._raw_tail, skipped = self._decode_tagged_pairs(data)
        self._raw_skipped += skipped
        first_index = self._raw_index
        self._raw_index += len(pairs)
        # The loop has no clock, so its pair rate is measured from arrival times.
        elapsed = self._raw_last_received - self.stream_start_time
        return pairs, {
            "start_time": self.stream_start_time,
            "end_time": self._raw_last_received,
            "first_index": first_index,
            "sample_period_s": elapsed / self._raw_index if self._raw_index and elapsed > 0 else None,
            "skipped_bytes": self._raw_skipped,
        }

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
