"""Zero-trust adversarial text filter for LLM and overlay inputs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MAX_TEXT_LENGTH_DEFAULT = 32000


def _max_text_length() -> int:
    return int(os.getenv("ADVERSARIAL_MAX_TEXT_LENGTH", str(MAX_TEXT_LENGTH_DEFAULT)))

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(the\s+)?(system|prior|previous)", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"new\s+instructions?\s*:", re.I),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
    re.compile(r"```\s*python", re.I),
    re.compile(r"\bexec\s*\(", re.I),
    re.compile(r"\beval\s*\(", re.I),
    re.compile(r"\bos\.system\s*\(", re.I),
    re.compile(r"\bsubprocess\b", re.I),
    re.compile(r"\b__import__\s*\(", re.I),
]

_CIRCUIT_BREAKER_BYPASS = [
    re.compile(r"global_kill_switch\s*=\s*false", re.I),
    re.compile(r"APEX_MAX_PORTFOLIO_PCT\s*=\s*1", re.I),
    re.compile(r"ORACLE_CB_ENABLED\s*=\s*false", re.I),
    re.compile(r"DRAIN_AND_HALT", re.I),
    re.compile(r"disable\s+(the\s+)?circuit\s+breaker", re.I),
    re.compile(r"bypass\s+(the\s+)?(cap|drawdown|kill)", re.I),
    re.compile(r"override\s+(inventory|position|portfolio)\s+cap", re.I),
]

_FENCE_ESCAPE = re.compile(r"```\s*```", re.I)


@dataclass
class AuditResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    sanitized_text: str = ""
    hard_reject: bool = False


def audit_text(text: str, *, context: str = "generic") -> AuditResult:
    """Scan text for injection patterns; return sanitized copy when soft violations only."""
    if not text:
        return AuditResult(passed=True, sanitized_text="")

    violations: list[str] = []
    hard_reject = False

    max_len = _max_text_length()
    if len(text) > max_len:
        violations.append(f"exceeds_max_length={len(text)}>{max_len}")
        text = text[:max_len]

    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            violations.append(f"injection_pattern:{pat.pattern[:40]}")

    for pat in _CIRCUIT_BREAKER_BYPASS:
        if pat.search(text):
            violations.append(f"circuit_breaker_bypass:{pat.pattern[:40]}")
            hard_reject = True

    if _FENCE_ESCAPE.search(text):
        violations.append("delimiter_escape")

    sanitized = text.replace("\r\n", "\n").strip()
    passed = len(violations) == 0
    return AuditResult(
        passed=passed,
        violations=violations,
        sanitized_text=sanitized,
        hard_reject=hard_reject,
    )


def audit_messages(messages: list[dict[str, str]], *, context: str = "llm") -> AuditResult:
    """Audit all message parts in an LLM chat payload."""
    combined_violations: list[str] = []
    hard_reject = False
    sanitized_parts: list[str] = []

    for msg in messages:
        content = str(msg.get("content") or "")
        result = audit_text(content, context=f"{context}:{msg.get('role', 'unknown')}")
        combined_violations.extend(result.violations)
        hard_reject = hard_reject or result.hard_reject
        sanitized_parts.append(result.sanitized_text)

    passed = len(combined_violations) == 0
    return AuditResult(
        passed=passed,
        violations=combined_violations,
        sanitized_text="\n".join(sanitized_parts),
        hard_reject=hard_reject,
    )
