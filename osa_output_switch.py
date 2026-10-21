"""Persistent controller for the JDSU SC optical switch that feeds the OSA.

Each Benes output fiber is patched to one JDSU switch channel; the mapping is
stored with the GPIB settings so selecting an output routes the right channel.
The JDSU must use its SC command set with CR+LF termination (see
``Jdsu_switch/switch.py``): ``CLOSE n`` selects a channel, bit 2 of ``CNB?``
reports that the mechanism has settled and ``CLOSE?`` confirms the channel.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from voltage_controller import app_dir

OUTPUTS = tuple(range(1, 7))
JDSU_CHANNELS = tuple(range(1, 17))


def fiber_number(output: int) -> int:
    """Benes output 1 leaves the chip on fiber 4, output 6 on fiber 9."""
    return output + 3


class OSAOutputSwitch:
    """Select one of the six Benes outputs before an OSA acquisition."""

    def __init__(self, config_path: Path | None = None,
                 resource_manager_factory: Callable[[], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.config_path = config_path or app_dir() / "benes_osa_output_switch_config.json"
        self._resource_manager_factory = resource_manager_factory
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self.manager = None
        self.instrument = None
        self._dry_run_connected = False
        # Output routed during this connection; unknown until the first selection.
        self.routed_output = None
        self.settings = self._load()

    @staticmethod
    def _defaults() -> dict:
        return {
            "resource": "GPIB0::25::INSTR",
            "dry_run": True,
            "settle_s": 0.25,
            "timeout_s": 5.0,
            "channel_map": {str(output): output for output in OUTPUTS},
            "selected_output": None,
        }

    def _load(self) -> dict:
        settings = self._defaults()
        if not self.config_path.exists():
            return settings
        try:
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            candidate = {**settings, **{name: stored[name] for name in settings if name in stored}}
            candidate["channel_map"] = self._normalized_map(candidate["channel_map"])
            self._validate(candidate)
            settings = candidate
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            # A bad optional-selector config must not prevent OSA measurements.
            pass
        return settings

    @staticmethod
    def _normalized_map(channel_map: dict) -> dict:
        """Return {"1".."6": channel or None}, accepting blank or "—" as unmapped."""
        normalized = {}
        for output in OUTPUTS:
            value = channel_map.get(str(output), channel_map.get(output))
            if value is None or str(value).strip() in ("", "—", "None"):
                normalized[str(output)] = None
            elif isinstance(value, bool):
                raise ValueError(f"JDSU channel for output {output} must be a number")
            else:
                normalized[str(output)] = int(value)
        return normalized

    @staticmethod
    def _validate(settings: dict) -> None:
        if not str(settings.get("resource", "")).strip():
            raise ValueError("Enter the JDSU VISA resource, for example GPIB0::25::INSTR")
        settle_s = float(settings.get("settle_s", 0))
        if not 0 <= settle_s <= 60:
            raise ValueError("Output-switch settling time must be between 0 and 60 seconds")
        timeout_s = float(settings.get("timeout_s", 0))
        if not 0 < timeout_s <= 60:
            raise ValueError("JDSU settle timeout must be between 0 and 60 seconds")
        used = {}
        for output, channel in settings["channel_map"].items():
            if channel is None:
                continue
            if channel not in JDSU_CHANNELS:
                raise ValueError(f"JDSU channel for output {output} must be from 1 to {JDSU_CHANNELS[-1]}")
            if channel in used:
                raise ValueError(f"Outputs {used[channel]} and {output} are both mapped to JDSU channel {channel}")
            used[channel] = output
        selected = settings.get("selected_output")
        if selected is not None and (isinstance(selected, bool) or int(selected) not in OUTPUTS):
            raise ValueError("Selected OSA output must be from 1 to 6")

    def save(self) -> None:
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)

    @property
    def connected(self) -> bool:
        return self.instrument is not None or self._dry_run_connected

    def configure(self, resource: object, dry_run: bool, settle_s: object, channel_map: dict) -> None:
        """Validate and persist settings, reconnecting if the resource or dry-run mode changed."""
        settings = {
            **self.settings,
            "resource": str(resource).strip(),
            "dry_run": bool(dry_run),
            "settle_s": float(settle_s),
            "channel_map": self._normalized_map(channel_map),
        }
        self._validate(settings)
        reconnect = self.connected and (settings["resource"], settings["dry_run"]) != (
            self.settings["resource"], self.settings["dry_run"])
        if reconnect:
            self.disconnect()
        self.settings = settings
        self.save()
        if reconnect:
            self.connect()

    def channel_for(self, output: object) -> int:
        output = int(output)
        if output not in OUTPUTS:
            raise ValueError("OSA output must be from 1 to 6")
        channel = self.settings["channel_map"][str(output)]
        if channel is None:
            raise ValueError(f"Output {output} (fiber {fiber_number(output)}) is not mapped to a JDSU channel; "
                             "set it in Selector setup")
        return channel

    def mapped_outputs(self) -> list[int]:
        return [output for output in OUTPUTS if self.settings["channel_map"][str(output)] is not None]

    def connect(self) -> None:
        if self.connected:
            return
        self.routed_output = None
        if self.settings["dry_run"]:
            self._dry_run_connected = True
            return
        factory = self._resource_manager_factory or self._load_resource_manager_factory()
        manager = factory()
        try:
            instrument = manager.open_resource(self.settings["resource"])
            instrument.timeout = 2000
            instrument.write_termination = "\r\n"
            instrument.read_termination = "\r\n"
        except Exception:
            manager.close()
            raise
        self.manager, self.instrument = manager, instrument

    @staticmethod
    def _load_resource_manager_factory():
        try:
            import pyvisa
        except ImportError as error:
            raise RuntimeError("Install PyVISA and NI-VISA to connect the JDSU switch: "
                               "python -m pip install pyvisa") from error
        return pyvisa.ResourceManager

    def list_resources(self) -> tuple[str, ...]:
        factory = self._resource_manager_factory or self._load_resource_manager_factory()
        manager = factory()
        try:
            return tuple(manager.list_resources())
        finally:
            manager.close()

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self.instrument is not None:
                    self.instrument.close()
            finally:
                try:
                    if self.manager is not None:
                        self.manager.close()
                finally:
                    self.instrument = self.manager = None
                    self._dry_run_connected = False
                    self.routed_output = None

    def _close_channel(self, channel: int) -> None:
        self.instrument.write(f"CLOSE {channel}")
        deadline = self._clock() + self.settings["timeout_s"]
        # Condition-register bit 2 means the switch mechanism has settled.
        while not int(self.instrument.query("CNB?").strip()) & 4:
            if self._clock() >= deadline:
                raise RuntimeError(f"JDSU switch did not settle within {self.settings['timeout_s']:g} s")
            self._sleep(0.1)
        actual = int(self.instrument.query("CLOSE?").strip())
        if actual != channel:
            raise RuntimeError(f"Requested JDSU channel {channel}, but the switch reports {actual}")

    def select_output(self, output: object) -> int:
        """Route the selected 1-based output to the OSA and wait for settling."""
        output = int(output)
        channel = self.channel_for(output)
        with self._lock:
            if not self.connected:
                raise RuntimeError("Connect the OSA output switch first")
            if not self.settings["dry_run"]:
                self._close_channel(channel)
            self.routed_output = output
            self.settings["selected_output"] = output
            self.save()
            if self.settings["settle_s"]:
                self._sleep(self.settings["settle_s"])
        return output
