"""Smoke test: dashboard app must import without runtime errors."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_app_py_parses_and_compiles():
    app_path = PROJECT_ROOT / "engine_3_dashboard" / "app.py"
    source = app_path.read_text(encoding="utf-8")
    ast.parse(source)
    compile(source, str(app_path), "exec")


def test_render_page_function_exists():
    app_path = PROJECT_ROOT / "engine_3_dashboard" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    ]
    assert "render_page" in names
