"""
Project LLM Wiki — Karpathy-style persistent memory for IP4 engineering progress.

Unlike ``arena_wiki.md`` (live trading narrative), this wiki tracks engineering
state: audits, decisions, lessons, backlog, operator notes, and session logs.
Cursor and other LLM agents read it at session start and update it when work lands.

Primary file: ``agent/wiki/project_wiki.md``
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIKI_PATH = PROJECT_ROOT / "agent" / "wiki" / "project_wiki.md"

SECTIONS = (
    "Project Overview",
    "Current State Audit",
    "Operator Runbook",
    "Architecture Map",
    "Active Work",
    "Decisions Log",
    "Lessons Learned",
    "User Preferences",
    "Session Log",
)

_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def read_wiki() -> str:
    """Return full wiki markdown."""
    WIKI_PATH.parent.mkdir(parents=True, exist_ok=True)
    if WIKI_PATH.exists():
        return WIKI_PATH.read_text(encoding="utf-8")
    return ""


def list_sections() -> List[str]:
    """Return section titles found in the wiki."""
    return _SECTION_RE.findall(read_wiki())


def read_section(title: str) -> str:
    """Return body text for a section (empty string if missing)."""
    text = read_wiki()
    pattern = re.compile(
        rf"^## {re.escape(title)}\n+(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _write_atomic(content: str) -> None:
    WIKI_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=WIKI_PATH.parent, prefix=".project_wiki.", suffix=".tmp"
    )
    try:
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        Path(tmp).replace(WIKI_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_section(title: str, body: str) -> None:
    """Replace a section body, appending the section if it does not exist."""
    text = read_wiki()
    body = body.strip()
    block = f"## {title}\n\n{body}\n"

    pattern = re.compile(
        rf"^## {re.escape(title)}\n+.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n{block}"

    _write_atomic(text)


def _append_to_section(title: str, entry: str) -> None:
    """Append a dated entry to a section."""
    existing = read_section(title)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = entry.strip()
    if not line.startswith("###"):
        line = f"### {stamp} — {line}"
    new_body = f"{existing}\n\n{line}".strip() if existing else line
    update_section(title, new_body)


def append_session_log(summary: str, *, next_step: str = "") -> None:
    """Record what happened this session."""
    body = summary.strip()
    if next_step:
        body += f"\n\n**Next:** {next_step.strip()}"
    _append_to_section("Session Log", body)


def append_decision(title: str, rationale: str) -> None:
    """Record an architectural or product decision."""
    entry = (
        f"### {datetime.now().strftime('%Y-%m-%d')} — {title.strip()}\n"
        f"{rationale.strip()}"
    )
    existing = read_section("Decisions Log")
    new_body = f"{existing}\n\n{entry}".strip() if existing else entry
    update_section("Decisions Log", new_body)


def append_lesson(
    title: str,
    *,
    trigger: str,
    impact: str,
    prevention: str,
    severity: str = "medium",
) -> None:
    """Record a lesson learned (mistake or correction)."""
    entry = (
        f"### {datetime.now().strftime('%Y-%m-%d')} — {title.strip()} "
        f"({severity})\n"
        f"- **Trigger:** {trigger.strip()}\n"
        f"- **Impact:** {impact.strip()}\n"
        f"- **Prevention:** {prevention.strip()}"
    )
    existing = read_section("Lessons Learned")
    new_body = f"{existing}\n\n{entry}".strip() if existing else entry
    update_section("Lessons Learned", new_body)


def append_user_preference(note: str) -> None:
    """Record an explicit user preference for agent behavior."""
    entry = f"- {datetime.now().strftime('%Y-%m-%d')}: {note.strip()}"
    existing = read_section("User Preferences")
    new_body = f"{existing}\n{entry}".strip() if existing else entry
    update_section("User Preferences", new_body)


def set_active_work(items: List[str]) -> None:
    """Replace the Active Work backlog list."""
    lines = ["| Status | Item |", "|--------|------|"]
    for item in items:
        status = "todo"
        text = item.strip()
        if text.startswith("[") and "]" in text:
            status, text = text[1:].split("]", 1)
            status = status.strip()
            text = text.strip()
        lines.append(f"| {status} | {text} |")
    update_section("Active Work", "\n".join(lines))


def touch_audit_timestamp() -> None:
    """Update the 'Last audited' line inside Current State Audit."""
    audit = read_section("Current State Audit")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    audit = re.sub(
        r"\*Last audited:.*\*",
        f"*Last audited: {stamp}*",
        audit,
    )
    if "*Last audited:" not in audit:
        audit = f"{audit}\n\n*Last audited: {stamp}*"
    update_section("Current State Audit", audit)


def wiki_context(max_chars: int = 12000) -> Dict[str, str]:
    """Compact bundle for LLM session grounding."""
    text = read_wiki()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n…(truncated — read agent/wiki/project_wiki.md for full wiki)"
    try:
        rel_path = str(WIKI_PATH.relative_to(PROJECT_ROOT))
    except ValueError:
        rel_path = str(WIKI_PATH)
    return {
        "path": rel_path,
        "sections": list_sections(),
        "content": text,
    }


if __name__ == "__main__":
    ctx = wiki_context()
    print(f"Wiki: {ctx['path']}")
    print(f"Sections: {', '.join(ctx['sections'])}")
