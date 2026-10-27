"""Where settings, results and sibling-project helpers live."""

from __future__ import annotations

import sys
from pathlib import Path

# benes/paths.py -> the BenesController folder that holds the configs and Results/.
PROJECT_DIR = Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Folder for settings and results: beside the exe when frozen, else the project folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_DIR


def sibling_module_path(project: str, filename: str) -> Path:
    """Locate a helper from a sibling project, or the copy bundled into the exe."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / project / filename
    return PROJECT_DIR.parent / project / filename
