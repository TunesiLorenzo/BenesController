"""Persistent Arduino detector reader built on ThorlabsControl's sampler."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import math
import threading
import time
from pathlib import Path
from typing import Callable

from benes.paths import app_dir, sibling_module_path


# Raw optical stream: top 7 A0 bits followed by a 110bbb0J status byte carrying
# the low 3 ADC bits and the jump flag. Older sketches always send bbb = 000.
# The Uno's 16 MHz clock divides exactly to both rates; match the sketch's Serial.begin.
RAW_BAUDS = (1_000_000, 2_000_000)
RAW_FULL_SCALE = 127
RAW_STATUS_BASE = 0xC0
RAW_STATUS_MASK = 0xE2
DEFAULT_JUMP_GUARD_MS = 5.0
# The detector flattens out near 80 counts, well below the 7-bit full scale.
DEFAULT_CEILING = 80.0
RANGE_TRACKING_OFF = "off"
RANGE_TRACKING_UP_FALLS = "range_up_falls"
RANGE_TRACKING_UP_RISES = "range_up_rises"
RANGE_TRACKING_MODES = (RANGE_TRACKING_OFF, RANGE_TRACKING_UP_FALLS, RANGE_TRACKING_UP_RISES)
RANGE_TRACKING_LABELS = {
    RANGE_TRACKING_OFF: "Off (no range correction)",
    RANGE_TRACKING_UP_FALLS: "Higher range: A0 falls",
    RANGE_TRACKING_UP_RISES: "Higher range: A0 rises",
}
# The 8152A XDCR OUT BNC is linear within a range, and adjacent ranges are
# 10 dB apart. The Arduino's jump flag is only a sudden-ADC-change heuristic,
# so a near-decade change in stable BNC levels is needed as well.
RANGE_STEP_TOLERANCE_DECADES = 0.20


@dataclass(frozen=True)
class PowerReading:
    """A scalar detector result suitable for a future calibration routine."""

    value: float
    unit: str
    raw_mean: float
    sample_count: int
    rejected_count: int = 0
    jump_count: int = 0
    sample_rate_hz: float | None = None
    range_steps: int = 0
    range_changes: int = 0


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
        # ``scale`` is calibrated at the range present on connection.  This
        # state carries later decade changes relative to that starting range.
        self._range_steps = 0
        self._range_previous_optical = None
        self._range_pending_optical = None
        self._range_guard_remaining = 0
        self._calibration_capture = False
        # Routing's live read and measurement workers share one serial stream.
        self._read_lock = threading.RLock()

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
            "jump_guard_ms": DEFAULT_JUMP_GUARD_MS,
            "range_tracking": RANGE_TRACKING_OFF,
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
        guard = float(settings.get("jump_guard_ms", DEFAULT_JUMP_GUARD_MS))
        if not math.isfinite(guard) or not 0 <= guard <= 1000:
            raise ValueError("Jump guard must be between 0 and 1000 ms")
        if settings.get("stream_format", "legacy") not in ("legacy", "dual", "raw"):
            raise ValueError("Arduino stream format must be raw, legacy, or dual")
        if settings.get("stream_format") == "raw" and int(settings["baud"]) not in RAW_BAUDS:
            raise ValueError("The raw optical sketch runs at 1000000 or 2000000 baud")
        if settings.get("range_tracking", RANGE_TRACKING_OFF) not in RANGE_TRACKING_MODES:
            raise ValueError("Select a valid Arduino autorange correction mode")

    def configure(self, port: object, baud: object, duration: object, scale: object, offset: object,
                  unit: object, stream_format: str = "legacy",
                  ceiling: object = DEFAULT_CEILING,
                  jump_guard_ms: object = DEFAULT_JUMP_GUARD_MS,
                  range_tracking: str = RANGE_TRACKING_OFF) -> None:
        settings = {
            "port": str(port).strip(),
            "baud": int(baud),
            "duration": float(duration),
            "scale": float(scale),
            "offset": float(offset),
            "unit": str(unit).strip() or "ADC counts",
            "stream_format": stream_format,
            "ceiling": float(ceiling),
            "jump_guard_ms": float(jump_guard_ms),
            "range_tracking": str(range_tracking),
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
        self._range_steps = 0
        self._range_previous_optical = None
        self._range_pending_optical = None
        self._range_guard_remaining = 0
        self._calibration_capture = False

    def disconnect(self) -> None:
        with self._read_lock:
            self._disconnect_unlocked()

    def _disconnect_unlocked(self) -> None:
        if self.serial is not None:
            try:
                if self._raw_running:
                    self.stop_stream()
            finally:
                self.sampler.close_arduino(self.serial)
                self.serial = None
                self._calibration_capture = False

    def begin_calibration_curve(self) -> None:
        """Retain range flags during voltage settling for one MZI sweep."""
        with self._read_lock:
            if self.settings["stream_format"] != "raw":
                return
            self._range_steps = 0
            self._range_previous_optical = None
            self._range_pending_optical = None
            self._range_guard_remaining = 0
            self.start_stream()
            self._calibration_capture = True

    def end_calibration_curve(self) -> None:
        with self._read_lock:
            if self._calibration_capture:
                self.stop_stream()
                self._calibration_capture = False

    def read_current_power(self, ceiling_override: float | None = None,
                           range_tracking_mode: str | None = None,
                           ignore_jumps: bool = False) -> PowerReading:
        with self._read_lock:
            return self._read_current_power_unlocked(ceiling_override, range_tracking_mode, ignore_jumps)

    def _read_current_power_unlocked(self, ceiling_override: float | None = None,
                                     range_tracking_mode: str | None = None,
                                     ignore_jumps: bool = False) -> PowerReading:
        if self.serial is None:
            raise RuntimeError("Connect the Arduino power meter first")
        ceiling = self.ceiling if ceiling_override is None else float(ceiling_override)
        if not math.isfinite(ceiling) or ceiling <= 0:
            raise ValueError("Arduino sample ceiling must be positive and finite")
        if self.settings["stream_format"] in ("dual", "raw"):
            try:
                samples, timing = self.read_timed_samples(
                    self.settings["duration"], reset_buffer=not self._calibration_capture)
            finally:
                if self.settings["stream_format"] == "raw" and not self._calibration_capture:
                    self.stop_stream()
            if self.settings["stream_format"] == "raw":
                period = timing.get("sample_period_s")
                guard_samples = (math.ceil(self.settings["jump_guard_ms"] / 1000 / period)
                                 if period and period > 0 else 0)
                raw_count = len(samples)
                history_rejected = history_jumps = history_changes = 0
                if self._calibration_capture and period and period > 0:
                    recent_count = max(1, math.ceil(self.settings["duration"] / period))
                    history, samples = samples[:-recent_count], samples[-recent_count:]
                    _, _, history_rejected, history_jumps, history_changes = self._process_raw_samples(
                        history, guard_samples, ceiling, mode=range_tracking_mode,
                        ignore_jumps=ignore_jumps)
                measurement_raw_count = len(samples)
                samples, corrected_samples, rejected, jumps, range_changes = self._process_raw_samples(
                    samples, guard_samples, ceiling, mode=range_tracking_mode,
                    ignore_jumps=ignore_jumps)
                rejected += history_rejected
                jumps += history_jumps
                range_changes += history_changes
                sample_rate = (measurement_raw_count / self.settings["duration"] if raw_count else None)
            else:
                samples = [optical for optical, _electrical in samples]
                corrected_samples = samples
                rejected = jumps = range_changes = 0
                sample_rate = None
        else:
            samples = self.sampler.burst_read_binary(self.serial, duration=self.settings["duration"])
            corrected_samples = samples
            rejected = jumps = range_changes = 0
            sample_rate = None
        if not samples:
            raise RuntimeError("The Arduino returned no usable optical samples; check the uploaded sketch, "
                               "jump guard, detector range, and ceiling")
        raw_mean = sum(samples) / len(samples)
        reading = PowerReading(
            value=(sum(corrected_samples) / len(corrected_samples) * self.settings["scale"]
                   + self.settings["offset"]),
            unit=self.settings["unit"],
            raw_mean=raw_mean,
            sample_count=len(samples),
            rejected_count=rejected,
            jump_count=jumps,
            sample_rate_hz=sample_rate,
            range_steps=self._range_steps,
            range_changes=range_changes,
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
    def _decode_optical_frames(data: bytes) -> tuple[list[tuple[int, bool]], bytes, int]:
        """Decode optical/status frames, preserving an unfinished A0 across reads.

        Optical values are 7-bit counts with the low 3 ADC bits as eighths, so
        ceilings and scale factors keep their meaning. Returns the frames, an
        unfinished trailing A0 byte, and how many bytes were skipped to
        resynchronize.
        """
        frames = []
        index = 0
        skipped = 0
        end = len(data) - 1
        while index < end:
            optical, status = data[index], data[index + 1]
            if optical < 0x80 and status & RAW_STATUS_MASK == RAW_STATUS_BASE:
                frames.append((optical + ((status >> 2) & 0b111) / 8, bool(status & 1)))
                index += 2
            else:
                skipped += 1
                index += 1
        tail = data[index:]
        if tail and tail[0] >= 0x80:
            # A lone status byte can never start a frame.
            skipped += 1
            tail = b""
        return frames, bytes(tail), skipped

    def _process_raw_samples(self, frames, guard_samples: int, ceiling: float, on_sample=None, mode=None,
                             ignore_jumps=False):
        """Reject transitions and put each stable sample on the starting range's scale.

        A jump-flagged sample is transitional, so compare the stable readings
        before and after its guard window, even when it crosses captures.
        A BNC-only reading cannot distinguish an unflagged range change from
        a real optical change between two voltage points, so do not rescale
        on that evidence alone.
        """
        clean = []
        corrected = []
        rejected = jumps = range_changes = 0
        previous = self._range_previous_optical
        pending = None if ignore_jumps else self._range_pending_optical
        remaining = 0 if ignore_jumps else self._range_guard_remaining
        mode = self.settings.get("range_tracking", RANGE_TRACKING_OFF) if mode is None else mode
        for optical, jump in frames:
            change = 0
            corrected_value = None
            if jump:
                jumps += 1
                if not ignore_jumps:
                    if pending is None:
                        pending = previous
                    remaining = max(remaining, guard_samples + 1)
            if remaining > 0 or optical >= ceiling:
                rejected += 1
            else:
                change = self._range_change(pending, optical, mode)
                if change:
                    self._range_steps += change
                    range_changes += 1
                pending = None
                previous = optical
                clean.append(optical)
                corrected_value = optical * 10 ** (0 if mode == RANGE_TRACKING_OFF else self._range_steps)
                corrected.append(corrected_value)
            if remaining > 0:
                remaining -= 1
            if on_sample is not None:
                on_sample(optical, corrected_value, jump, change, self._range_steps)
        self._range_previous_optical = previous
        self._range_pending_optical = pending
        self._range_guard_remaining = remaining
        return clean, corrected, rejected, jumps, range_changes

    @staticmethod
    def _range_change(previous, optical, mode: str) -> int:
        """Return one signed 10 dB range step from stable BNC samples."""
        if mode == RANGE_TRACKING_OFF or previous is None or previous <= 0 or optical <= 0:
            return 0
        decades = math.log10(optical / previous)
        if abs(abs(decades) - 1) > RANGE_STEP_TOLERANCE_DECADES:
            return 0
        raw_rose = decades > 0
        range_up = raw_rose if mode == RANGE_TRACKING_UP_RISES else not raw_rose
        return 1 if range_up else -1

    def read_timed_samples(self, duration: float, reset_buffer: bool = True):
        with self._read_lock:
            return self._read_timed_samples_unlocked(duration, reset_buffer)

    def _read_timed_samples_unlocked(self, duration: float, reset_buffer: bool = True):
        """Return optical/status frames (raw) or optical/electrical pairs (dual)."""
        if self.serial is None:
            raise RuntimeError("Connect the Arduino power meter first")
        if self.settings["stream_format"] == "raw":
            return self._read_raw_samples(duration, reset_buffer)
        if self.settings["stream_format"] != "dual":
            raise RuntimeError("Select the raw optical or dual A0/A1 Arduino stream")
        if reset_buffer:
            self._dual_buffer.clear()
        raw, timing = self.sampler.burst_read_binary(
            self.serial, duration=duration, reset_buffer=reset_buffer, return_timing=True,
        )
        self._dual_buffer.extend(raw)
        return self._decode_dual_frames(self._dual_buffer), timing

    def stop_stream(self) -> None:
        with self._read_lock:
            self._stop_stream_unlocked()

    def _stop_stream_unlocked(self) -> None:
        """Stop collecting; the Arduino keeps streaming and the PC discards it."""
        self._raw_running = False
        self._raw_tail = b""

    def start_stream(self) -> None:
        with self._read_lock:
            self._start_stream_unlocked()

    def _start_stream_unlocked(self) -> None:
        """Begin a capture at the newest bytes; status markers restore pairing."""
        if self.serial is None or self.settings["stream_format"] != "raw":
            raise RuntimeError("Connect the raw optical Arduino stream first")
        self.serial.reset_input_buffer()
        self._raw_tail = b""
        self._raw_index = 0
        self._raw_skipped = 0
        self.stream_start_time = time.perf_counter()
        self._raw_running = True
        self._raw_last_received = self.stream_start_time

    def _read_raw_samples(self, duration: float, restart: bool):
        """Decode optical/status frames; retain only an unfinished frame."""
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
        pairs, self._raw_tail, skipped = self._decode_optical_frames(data)
        self._raw_skipped += skipped
        first_index = self._raw_index
        self._raw_index += len(pairs)
        # The loop has no clock, so its frame rate is measured from arrival times.
        elapsed = self._raw_last_received - self.stream_start_time
        return pairs, {
            "start_time": self.stream_start_time,
            "end_time": self._raw_last_received,
            "first_index": first_index,
            "sample_period_s": elapsed / self._raw_index if self._raw_index and elapsed > 0 else None,
            "skipped_bytes": self._raw_skipped,
        }

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
