#!/usr/bin/env python3
"""One-shot IP4 bootstrap: venv check, .env, sqld, schema, seed, preflight."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


def _base_python() -> str:
    for name in ("python3.11", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


def _has_libsql(python: Path) -> bool:
    r = subprocess.run(
        [str(python), "-c", "import libsql"],
        capture_output=True,
    )
    return r.returncode == 0


def resolve_python(*, force_venv: bool = False) -> Path:
    local_venv = PROJECT_ROOT / ".venv"
    local_py = local_venv / "bin" / "python"

    if force_venv and local_venv.exists():
        print(f"Removing existing .venv (--force-venv) ...")
        shutil.rmtree(local_venv)

    if local_py.exists() and _has_libsql(local_py):
        print(f"Using IP4 venv: {local_py}")
        return local_py

    base_python = _base_python()
    if not local_py.exists():
        print(f"Creating .venv with {base_python} ...")
        subprocess.run(
            [base_python, "-m", "venv", str(local_venv)],
            cwd=str(PROJECT_ROOT),
            check=True,
        )

    print("Installing requirements (needs libsql — may take a minute) ...")
    subprocess.run(
        [str(local_py), "-m", "pip", "install", "-U", "pip"],
        cwd=str(PROJECT_ROOT),
        check=True,
    )
    subprocess.run(
        [str(local_py), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=str(PROJECT_ROOT),
        check=True,
    )
    if not _has_libsql(local_py):
        raise SystemExit(
            "libsql install failed in IP4 .venv.\n"
            "Recovery:\n"
            "  cd InvestmentProphits4\n"
            "  rm -rf .venv && python3 scripts/ip4_bootstrap.py --force-venv"
        )
    return local_py


def _run(python: Path, script: str, *args: str) -> None:
    cmd = [str(python), str(PROJECT_ROOT / script), *args]
    print(f"\n>> {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def ensure_env_file() -> None:
    if ENV_FILE.exists():
        print(f".env exists at {ENV_FILE}")
        return
    if not ENV_EXAMPLE.exists():
        raise SystemExit("Missing .env.example — cannot create .env")
    shutil.copy(ENV_EXAMPLE, ENV_FILE)
    print(f"Created {ENV_FILE} from .env.example")
    print("Optional: set TURSO_* for cloud sync, DEEPSEEK_V4_API for live Crucible.")


def main() -> None:
    parser = argparse.ArgumentParser(description="IP4 one-shot bootstrap")
    parser.add_argument(
        "--force-venv",
        action="store_true",
        help="Recreate .venv from scratch before installing deps",
    )
    args = parser.parse_args()

    print("=== IP4 Bootstrap ===")
    python = resolve_python(force_venv=args.force_venv)
    ensure_env_file()
    _run(python, "scripts/start_local_sqld.py")
    _run(python, "database/migrate_schema.py")
    _run(python, "database/seed_arena.py")
    _run(python, "scripts/preflight.py")
    print("\n=== Bootstrap complete ===")
    print("Start both engines:  ./scripts/ip4_supervisor.sh")
    print("Or individually:")
    print(f"  {python} engine_1_apex/ip4_apex_edge.py")
    print(f"  {python} engine_2_crucible/ip4_swarm_crucible.py")


if __name__ == "__main__":
    main()
