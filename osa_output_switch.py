"""Persistent, protocol-neutral controller for the OSA output selector.

The optical selector's command protocol is intentionally configurable: until its
vendor/interface is known, use dry-run mode.  Later, set a serial command
template such as ``OUT {port}`` without changing the measurement workflow.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from voltage_controller import app_dir


class OSAOutputSwitch:
    """Select one of the six Benes outputs before an OSA acquisition."""

    def __init__(self, config_path: Path | None = None, serial_factory: Callable[..., object] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config_path = config_path or app_dir() / "benes_osa_output_switch_config.json"
        self._serial_factory = serial_factory
        self._sleep = sleep
        self.serial = None
        self._dry_run_connected = False
        self.settings = self._load()

    @staticmethod
    def _defaults() -> dict:
        return {
            "port": "COM5",
            "baud": 115200,
            "dry_run": True,
            "command_template": "",
            "settle_s": 0.25,
            "selected_output": None,
        }

    def _load(self) -> dict:
        settings = self._defaults()
        if not self.config_path.exists():
            return settings
        try:
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            self._validate(stored)
            settings.update({name: stored[name] for name in settings if name in stored})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A bad optional-selector config must not prevent OSA measurements.
            pass
        return settings

    @staticmethod
    def _validate(settings: dict) -> None:
        if not str(settings.get("port", "")).strip():
            raise ValueError("Enter an output-switch serial port, for example COM5")
        if int(settings.get("baud", 0)) <= 0:
            raise ValueError("Output-switch baud rate must be positive")
        template = str(settings.get("command_template", ""))
        if template and "{port}" not in template:
            raise ValueError("Output-switch command must contain {port}, for example OUT {port}")
        settle_s = float(settings.get("settle_s", 0))
        if not 0 <= settle_s <= 60:
            raise ValueError("Output-switch settling time must be between 0 and 60 seconds")
        selected = settings.get("selected_output")
        if selected is not None and (isinstance(selected, bool) or not 1 <= int(selected) <= 6):
            raise ValueError("Selected OSA output must be from 1 to 6")

    def save(self) -> None:
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)

    @property
    def connected(self) -> bool:
        return self.serial is not None or self._dry_run_connected

    def configure(self, port: object, baud: object, dry_run: bool, command_template: object,
                  settle_s: object) -> None:
        settings = {
            **self.settings,
            "port": str(port).strip(),
            "baud": int(baud),
            "dry_run": bool(dry_run),
            "command_template": str(command_template).strip(),
            "settle_s": float(settle_s),
        }
        self._validate(settings)
        self.settings = settings
        self.save()

    def connect(self) -> None:
        if self.connected:
            return
        if self.settings["dry_run"]:
            self._dry_run_connected = True
            return
        if not self.settings["command_template"]:
            raise RuntimeError("Set the output-switch command template before connecting physical hardware")
        factory = self._serial_factory or self._load_serial_factory()
        self.serial = factory(self.settings["port"], self.settings["baud"], timeout=1)

    @staticmethod
    def _load_serial_factory():
        try:
            import serial
        except ImportError as error:
            raise RuntimeError("Install pyserial to connect the OSA output switch") from error
        return serial.Serial

    def disconnect(self) -> None:
        if self.serial is not None:
            try:
                self.serial.close()
            finally:
                self.serial = None
        self._dry_run_connected = False

    def select_output(self, output: object) -> int:
        """Route the selected 1-based output to the OSA and wait for settling."""
        output = int(output)
        if not 1 <= output <= 6:
            raise ValueError("OSA output must be from 1 to 6")
        if not self.connected:
            raise RuntimeError("Connect the OSA output switch first")
        if not self.settings["dry_run"]:
            command = self.settings["command_template"].format(port=output) + "\n"
            written = self.serial.write(command.encode("ascii"))
            if written != len(command):
                raise OSError("Incomplete output-switch command")
            flush = getattr(self.serial, "flush", None)
            if flush is not None:
                flush()
        self.settings["selected_output"] = output
        self.save()
        if self.settings["settle_s"]:
            self._sleep(self.settings["settle_s"])
        return output
