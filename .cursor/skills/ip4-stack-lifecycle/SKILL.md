---
name: ip4-stack-lifecycle
description: >-
  InvestmentProphits4 stack stop, restart, session handoff, and post-restart
  trade-flow verification. Use at session start, after restart_stack or Apex/Crucible
  changes, before sign-off, when QA reports stalled/degraded/crash, or when the
  user asks to start/stop/restart the application. After every stack restart the
  agent must confirm Apex and Crucible are running and wait for at least one
  successful buy and one successful sell before claiming the stack is ready.
---

# IP4 Stack Lifecycle

> **Never end a session on a crashed stack. Either leave it healthy (gate PASS + trade flow verified) or stop it cleanly (stop_stack PASS).**

Companion skills:
- [`ip4-definition-of-done`](../ip4-definition-of-done/SKILL.md) — acceptance gate before sign-off

## Canonical commands (agent runs these)

| Action | Command |
|--------|---------|
| Snapshot | `.venv/bin/python scripts/stack_status.py` |
| Snapshot (must be healthy) | `.venv/bin/python scripts/stack_status.py --require-healthy` |
| Stop cleanly | `.venv/bin/python scripts/stop_stack.py` |
| **Restart + full verify** | `.venv/bin/python scripts/restart_stack.py` |
| Restart (infra only) | `.venv/bin/python scripts/restart_stack.py --skip-trade-flow` |
| **Trade flow wait** | `.venv/bin/python scripts/verify_trade_flow.py` |
| Trade flow snapshot | `.venv/bin/python scripts/verify_trade_flow.py --snapshot` |
| Engines only | `.venv/bin/python scripts/verify_trade_flow.py --engines-only` |
| Full QA | `.venv/bin/python scripts/acceptance_gate.py --scope full` |

**Do not** tell the user to run these unless they explicitly ask for the recipe.

## Post-restart verification (mandatory)

After **every** `restart_stack.py`, Apex kill+respawn, or supervisor restart that affects trading:

```
1. restart_stack.py          # includes infra verify + trade flow by default
   OR manual sequence below
2. stack_status.py           # supervisor + apex + crucible + dashboard up
3. verify_trade_flow.py      # wait for ≥1 buy AND ≥1 sell
4. acceptance_gate.py --scope full   # before sign-off
```

### What “buy” and “sell” mean (live evidence)

| Event | Accepted proof |
|-------|----------------|
| **Buy** | `APEX FILL:` in `logs/apex.log` (current Apex session), **or** `filled≥1` on tick line, **or** new `trade_execution` row with `committed_at` after restart |
| **Sell** | `APEX CLOSE`, `APEX CAP STALL remediate`, tick with `closed_flip≥1` / `closed_rebalance≥1`, **or** `trade_execution.status LIKE 'CLOSED_%'` with `closed_at` after restart |

Current Apex session = log lines after the last `Initiating IP4 Apex Edge Engine`.

### Engine checks (required before/during trade flow)

| Engine | Must be |
|--------|---------|
| **Apex** | Process alive (`engine_1_apex/ip4_apex_edge.py` pid) |
| **Crucible** | Process alive (`engine_2_crucible/ip4_swarm_crucible.py` pid) |
| **Crucible activity** | `CRUCIBLE -` log line within `CRUCIBLE_VERIFY_MAX_IDLE_SECONDS` (default 600s) |

Supervisor and dashboard should also be up (`stack_status.py` → `infra_ok=true`).

### Timeouts

| Env var | Default | Purpose |
|---------|---------|---------|
| `TRADE_FLOW_VERIFY_TIMEOUT_SECONDS` | 1200 (20 min) | Max wait for buy+sell |
| `TRADE_FLOW_VERIFY_POLL_SECONDS` | 15 | Poll interval |
| `CRUCIBLE_VERIFY_MAX_IDLE_SECONDS` | 600 | Crucible log freshness |

If timeout hits: read `trader_health`, `cap_reasons`, and apex log — do **not** hand off. Fix (savepoint, cap stall, edge gate) and restart again.

**Cap-remediation timing:** After restart, if a pre-existing open leg triggers `CAP STALL remediate` (sell), Apex enforces `APEX_CAP_STALL_ENTRY_COOLDOWN_SECONDS` (default **600s**) before re-entering that market. Trade-flow wait must cover **~3 min cap-stall ramp + 600s cooldown + fill** → use **≥15–20 min** timeout (`--trade-flow-timeout 1200`) on `restart_stack.py`, not 8 min.

### Forbidden after restart

- Declaring “stack is up” after `verify_stack` alone
- Handing off with only infra PASS while zero buys/sells since restart
- Skipping trade flow without explicit user scope (`--skip-trade-flow` is infra-only)
- Restarting then immediately ending session without waiting

### Reporting template (post-restart)

> Restart OK. Apex pid X, Crucible pid Y. Trade flow PASS in Ns — buy: [market/evidence], sell: [market/evidence]. acceptance_gate PASS.

## Session start ritual (mandatory)

Before exploring code or implementing:

```
1. Read agent/wiki/project_wiki.md (Operator Runbook, Active Work)
2. stack_status.py
3. If unhealthy → diagnose and fix OR restart_stack.py (full verify)
4. If restarted earlier in session → confirm verify_trade_flow already PASS for that restart
5. Only then begin implementation
```

### Unhealthy signals

| Signal | Meaning | First action |
|--------|---------|--------------|
| `supervisor DOWN` | No process spawner | `restart_stack.py` |
| `apex DOWN` / `crucible DOWN` | Engine dead | `restart_stack.py` |
| `DUPLICATE pids` | Orphan/stray processes | `stop_stack.py --force` then `restart_stack.py` |
| `stalled=true` | STALLED/DEGRADED/STOPPED health | Read `trader_health`; fix; restart |
| `apex_errors` | Traceback/SQL errors in apex session | Fix code; restart |
| Trade flow FAIL | No buy/sell since restart | Diagnose caps/edge/oracle; fix; restart |

## Mid-session restart (after code changes)

Python does **not** hot-reload. Restart after every change to runtime code.

| Changed path | Restart | Trade flow required? |
|--------------|---------|----------------------|
| `engine_1_apex/*` | Kill Apex or `restart_stack.py` | **Yes** (Apex changed) |
| `engine_2_crucible/*` | Kill Crucible or full restart | **Yes** if Apex trades depend on it |
| `engine_3_dashboard/*` | Restart Streamlit | No (unless trading logic) |
| `scripts/supervisor_watch.py` | `restart_stack.py` | **Yes** |
| `database/transaction.py`, gateway, trade_close | `restart_stack.py` | **Yes** |

**Targeted Apex respawn** (supervisor healthy, Apex-only change):

```bash
kill $(pgrep -f engine_1_apex/ip4_apex_edge.py)
sleep 8
tail -5 logs/apex.log   # new Initiating IP4 Apex, no Traceback
.venv/bin/python scripts/verify_trade_flow.py
```

**Full restart** (default):

```bash
.venv/bin/python scripts/restart_stack.py
```

## Session end ritual (mandatory before sign-off)

### A — Healthy running stack (preferred)

```
- [ ] Code changes + pytest pass
- [ ] Processes restarted with new code
- [ ] stack_status.py --require-healthy
- [ ] verify_trade_flow.py PASS (buy + sell since last restart)
- [ ] acceptance_gate.py --scope full → PASS
- [ ] Session log with pids + buy/sell evidence
```

### B — Clean stopped stack

```bash
.venv/bin/python scripts/stop_stack.py
.venv/bin/python scripts/stack_status.py   # all processes DOWN
```

Session log: **"Stack intentionally stopped — run restart_stack.py to resume"**

## Stop vs restart semantics

**`stop_stack.py`** — graceful stop, DB → HALTED, clear orphans. Supervisor exits too.

**After `stop_stack.py`:** run `restart_stack.py` — it sets DB → RUNNING and spawns engines. Dashboard **Stop** buttons also set HALTED; use **Resume Apex/Crucible** (or restart_stack) to start again — **Start** is disabled while supervisor runs.

**`restart_stack.py`** — stop → start supervisor → DB RUNNING → `verify_stack.py` → **`verify_trade_flow.py`** (unless `--skip-trade-flow`).

## Diagnosis quick reference

```bash
.venv/bin/python scripts/stack_status.py
.venv/bin/python scripts/verify_trade_flow.py --snapshot
.venv/bin/python scripts/verify_stack.py --trading
tail -30 logs/apex.log
tail -10 logs/crucible.log
```

**`cap_blocked` / `fully_deployed`** — wallet deployed at max legs; auto-remediation closes a leg after 18 ticks (sell), then may buy again after cooldown. Trade flow may take several minutes — keep waiting, do not hand off early.

**Wallet STOPPED ≠ process crash.** Check pids first.

## Strategy sync reminder

After editing `engine_2_crucible/active_strategy.py`, sync to DB and restart Apex before expecting fills.

## Related files

- `scripts/trade_flow_verify.py` — buy/sell/engine verification logic
- `scripts/verify_trade_flow.py` — CLI wait/snapshot
- `scripts/stack_lifecycle.py` — stop/status helpers
- `scripts/restart_stack.py` — orchestrated restart
- `.cursor/skills/ip4-definition-of-done/SKILL.md`
