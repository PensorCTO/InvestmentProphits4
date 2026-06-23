"""Atomic strategy file swaps via staging + os.replace."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_strategy(path: Path | str, content: str) -> None:
    """Write strategy source atomically so Apex never reads a partial file."""
    target = Path(path)
    staging = target.with_suffix(target.suffix + ".staging")
    staging.write_text(content, encoding="utf-8")
    os.replace(staging, target)
