"""Re-exec CLI scripts with the project .venv interpreter when needed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


def ensure_project_python() -> None:
    """Use .venv/bin/python so libsql and other deps are available."""
    if not VENV_PYTHON.is_file():
        return
    try:
        same = Path(sys.executable).resolve() == VENV_PYTHON.resolve()
    except OSError:
        same = False
    if same:
        return
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])
