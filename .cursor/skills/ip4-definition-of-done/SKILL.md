---
name: ip4-definition-of-done
description: >-
  InvestmentProphits4 definition-of-done and acceptance gate. Use before claiming
  any IP4 fix is complete, after dashboard/trading/supervisor changes, when the
  user says QA failed, or when work touches Apex, Crucible, verify_stack, or
  Streamlit. Blocks sign-off until live tests pass — not pytest alone.
---

# IP4 Definition of Done

> **If you cannot test it live, you did not properly build it.**

Unit tests prove code compiles. They do **not** prove the trading stack works.
This skill is mandatory for IP4 engineering tasks.

## When to load this skill

- Before saying "done", "implemented", "verified", or "all tests pass"
- After any change to Apex, Crucible, dashboard, supervisor, stoppage, portfolio, verify scripts
- When the user reports QA failed or the system looks "dead" / "stalled"
- Before appending `## Session Log` in `agent/wiki/project_wiki.md`

## Forbidden sign-off (without gate PASS)

Do **not** tell the user the work is complete if any of these are true:

- Only `pytest` passed
- Only `verify_stack.py` (infra) passed while trading is STALLED **or** zero-fill streak ≥ 18 with signals
- Subagents reported success but **you** did not run the acceptance gate
- Apex/Crucible/dashboard were **not restarted** after code changes
- Dashboard was not loaded or queried after UI changes
- Trading fix claimed without `trader_health.trading_status != STALLED` (unless user scoped infra-only)

## Acceptance gate (required)

Run from project root:

```bash
# Before changes — capture baseline
.venv/bin/python scripts/acceptance_gate.py --scope full --baseline

# After changes — must PASS for your scope
.venv/bin/python scripts/acceptance_gate.py --scope full
```

| Scope | Use when |
|-------|----------|
| `--scope infra` | sqld/supervisor/processes only |
| `--scope trading` | Apex fills, stoppage, remediation, strategy |
| `--scope dashboard` | Streamlit UI / db.py display logic |
| `--scope full` | Default — any user-visible fix |

Trading scope fails when:

- `trading_status == STALLED`, or
- `zero_fill_streak >= 18` with signals and no fills (even if labeled IDLE), or
- `minutes_since_last_fill > 15` with active signals

Tune via `ACCEPTANCE_MAX_ZERO_FILL_STREAK` and `ACCEPTANCE_MAX_MINUTES_NO_FILL`.

**Exit code 1 = not done.** Fix and re-run. No exceptions.

## Mandatory workflow

Copy and track:

```
- [ ] 1. Read agent/wiki/project_wiki.md (Operator Runbook, Active Work)
- [ ] 2. Baseline: acceptance_gate.py --scope full --baseline
- [ ] 3. Implement + unit tests
- [ ] 4. Restart affected processes (agent runs — not the user)
- [ ] 5. acceptance_gate.py --scope full → PASS
- [ ] 6. Live evidence captured (see below)
- [ ] 7. Session log + honest summary to user
```

### 4. Restart affected processes

| Changed | Restart |
|---------|---------|
| `engine_1_apex/*` | Kill Apex pid; supervisor respawns. Confirm new pid + no Traceback in `logs/apex.log` |
| `engine_2_crucible/*` | Kill Crucible pid |
| `engine_3_dashboard/*` | Restart Streamlit (supervisor or kill streamlit pid) |
| `scripts/supervisor_watch.py` | Restart supervisor |

**Never** assume hot-reload picked up Python changes.

### 6. Live evidence (paste into session log)

At minimum:

```bash
.venv/bin/python scripts/verify_stack.py
.venv/bin/python scripts/verify_stack.py --trading
tail -5 logs/apex.log
.venv/bin/python -c "
from engine_3_dashboard.db import get_connection, fetch_trader_status, fetch_trader_health
c=get_connection()
print('status', fetch_trader_status(c))
print('health', {k: fetch_trader_health(c).get(k) for k in ['trading_status','zero_fill_streak','dominant_block_reason','filled_last_tick']})
c.close()
"
```

For dashboard UI changes: open `http://127.0.0.1:8501` and confirm **System Status** shows `INFRA ALIVE` separately from trading state.

For trading-behavior fixes: wait through remediation threshold (default 30 ticks ≈ 5 min) or run a targeted soak before sign-off.

## Lessons from past failures (do not repeat)

| Mistake | Why QA failed | Required check |
|---------|---------------|----------------|
| `fetch_portfolio_history` ORDER BY ASC | Chart showed $1099 NAV, metrics showed $95 | `fetch_portfolio` history tail NAV == live NAV |
| `@st.fragment` auto-refresh | Stale zero_fill_streak=0 in UI | Full `st.rerun()` or verify dashboard matches DB |
| `verify_stack` infra-only PASS | Declared victory while STALLED 110+ ticks | `--trading` must pass for trading fixes |
| Cap remediation shipped, Apex crash on preflight | `read_trader_health(conn, agent_id)` TypeError | `apex_log_clean` in acceptance gate |
| Subagents done = parent done | Parent never restarted Apex | Parent runs gate after integrating subagent output |
| Lifetime PnL -$104 on $95 wallet | Operator sees nonsense | Session PnL primary; lifetime in caption |

## Scope rules

| User goal | Gate scope | Pass requires |
|-----------|------------|---------------|
| Fix wallet panel / chart | `dashboard` + `full` | Dashboard HTTP + semantics + portfolio NAV match |
| Fix trading stall | `trading` + `full` | `trading_status != STALLED` OR user accepts known edge_gated market |
| Fix supervisor/infra | `infra` | All process checks OK |
| Stabilization / multi-phase | `full` | All checks; soak if activity changed |

If trading is **honestly** edge-gated (live CLOB, negative net edge), say so explicitly with apex log line — do not claim "trading fixed". Offer next step (strategy/Crucible), not false PASS.

## Reporting to the user

**Good:**

> Acceptance gate PASS (full). Infra alive. Trading IDLE, streak 2, last fill 3m ago. Apex pid 39549 restarted, no tracebacks.

**Bad:**

> 156 tests pass. verify_stack PASS. Implemented all phases.

## Related commands

```bash
.venv/bin/python scripts/restart_stack.py      # after stack-level changes
.venv/bin/python scripts/trader_health_audit.py --trading
.venv/bin/pytest tests/ -q
```

## Wiki

On significant pass/fail, append `agent/wiki/project_wiki.md` **Session Log** with gate output summary and **Next** step.
