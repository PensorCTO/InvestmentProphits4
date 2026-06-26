---
description: Karpathy LLM Wiki — read and update IP4 project progress memory
alwaysApply: true
---

# InvestmentProphits4 LLM Wiki Protocol

ContextStream MCP is **disabled**. Use the in-repo Karpathy-style wiki as persistent memory.

## Wiki files

| File | Purpose |
|------|---------|
| `agent/wiki/project_wiki.md` | **Engineering** — audits, decisions, lessons, backlog, operator runbook, sessions |
| `agent/wiki/arena_wiki.md` | **Trading** — Apex/Crucible narrative, fills, stoppage (not engineering) |
| `agent/project_wiki.py` | Helpers to read/update project wiki sections |

## Session startup (mandatory)

1. **Read** `agent/wiki/project_wiki.md` before exploring the codebase on a new task.
2. Check **Current State Audit**, **Operator Runbook**, and **Active Work**.
3. Skim **Decisions Log**, **Lessons Learned**, and **User Preferences** before changes.
4. **Stack check:** `.venv/bin/python scripts/stack_status.py` — see `.agents/skills/ip4-stack-lifecycle/SKILL.md`.
5. **After restart:** `.venv/bin/python scripts/verify_trade_flow.py` — must see buy + sell before handoff.

## During work

- **Decisions** → `## Decisions Log` or `append_decision()`
- **Lessons** → `## Lessons Learned` or `append_lesson()`
- **User said X about how to work** → `## User Preferences` or `append_user_preference()`
- **Backlog** → `## Active Work` table (`todo` / `wip` / `done`)
- **Runtime state changed** → refresh `## Current State Audit`
- **After Apex/Crucible/dashboard code changes** → restart per stack-lifecycle skill

## Session end (significant work)

Append to `## Session Log` via `append_session_log()` — what changed, validation, **Next** step.

**Before sign-off:** gate PASS + `stack_status.py --require-healthy` **OR** `stop_stack.py` (clean stop). See `.agents/skills/ip4-stack-lifecycle/SKILL.md` and `.agents/skills/ip4-definition-of-done/SKILL.md`. Exit 1 = not done. **Never hand off a crashed stack.**

## Operator actions

When the user asks to start/stop/fix the stack: **the agent runs commands** — do not dump shell recipes unless they explicitly want them.

Supervisor spawns Apex + Crucible (+ optional dashboard). Dashboard buttons only write `execution_controls`; they do not spawn processes without supervisor.

## Do NOT

- Call ContextStream / `mcp__contextstream__*` tools
- Store decisions only in chat — persist to `project_wiki.md`
- Confuse arena wiki (trading) with project wiki (engineering)
- Tell the user to run commands you can run yourself (see User Preferences)
