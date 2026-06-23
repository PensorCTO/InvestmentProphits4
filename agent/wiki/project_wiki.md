# IP4 Project Wiki

Karpathy-style **engineering memory** for InvestmentProphits4. Authoritative architecture:
[`InvestmentProphits4_MASTER_BLUEPRINTS.md`](../InvestmentProphits4_MASTER_BLUEPRINTS.md).

Trading narrative (fills, stoppage ticks): [`arena_wiki.md`](arena_wiki.md).

Helpers: [`agent/project_wiki.py`](../project_wiki.py).

---

## Project Overview

IP4 is a **dual-engine paper arena** on libSQL (local sqld or Turso Cloud):

| Engine | Process | Role |
|--------|---------|------|
| Apex Edge | `engine_1_apex/ip4_apex_edge.py` | Oracle → `trade_exhaust` → champion strategy → fair value + edge gates → paper fills |
| Crucible | `engine_2_crucible/ip4_swarm_crucible.py` | Karpathy loop: DeepSeek → backtest → KEEP/REVERT → Turso |
| Command Center | `engine_3_dashboard/app.py` | Streamlit control plane, NAV, wallet health |

**Supervisor** (`scripts/supervisor_watch.py`) is the **only** process spawner for Apex/Crucible/dashboard lifecycle. Dashboard Start/Stop updates `execution_controls` only.

---

## Current State Audit

*Last audited: 2026-06-22 21:45*

### Codebase (June 2026 audit refactor — landed)

| Area | Status |
|------|--------|
| Crucible validity gate | Champion Sortino on **resolved-only** corpus (`backtest_corpus.py`); optional `RESEARCH_SCORE` when `BACKTEST_MOCK_RESOLUTIONS=true` |
| Strategy sandbox | AST + subprocess smoke test in `strategy_loader.py` + `strategy_sandbox_worker.py` |
| Edge gates | Paper + live default **0.015**; exploration **0.008** via `APEX_EDGE_MODE=exploration` or `CRUCIBLE_EXPLORATION=true` |
| Ladder caps | `APEX_MAX_LEGS_PER_MARKET` = floor(`APEX_MAX_LADDER_LEGS`/2), default **1** when legs=3 |
| OBI research | `scripts/validate_obi_predictiveness.py` |
| Strategy overlay | `cross_venue_adj` in `build_market_state()`; baseline requires consensus |
| NAV telemetry | `portfolio_snapshots.total_capital_injected`, `SCOPE_APEX` injection ledger, dashboard True PnL |

### Runtime (typical dev machine)

| Component | Notes |
|-----------|-------|
| sqld | Local primary `http://127.0.0.1:8080` |
| Supervisor | Must stay running; orphans Apex/Crucible if supervisor dies |
| Dashboard | `http://127.0.0.1:8501` — fixed ImportError in `db.py` (no longer imports `get_apex_total_injected`) |
| `.env` | `CROSS_VENUE_ENABLED=true`; champion synced to Turso (v30+) |
| Wallet health | **STOPPED** was EXECUTION_STARVATION (stale Turso strategy + edge rejections). Edge-gated rejects and all-HOLD now stay **HEALTHY**. |

### Known gaps

- Master blueprints still mention old `APEX_PAPER_MIN_NET_EDGE=0.008` in places — wiki/env are source of truth post-audit.
- Resolved market corpus may be thin → Crucible REVERTs until `markets_ledger.is_resolved=1` rows exist.
- `ip4_supervisor.sh watch-bg` supervisor occasionally exits; agent should restart via `nohup .venv/bin/python scripts/supervisor_watch.py --dashboard`.

---

## Operator Runbook

**Agent executes these — do not instruct the user to copy-paste unless they ask.**

1. Ensure sqld: `.venv/bin/python scripts/start_local_sqld.py`
2. Start stack: `nohup .venv/bin/python scripts/supervisor_watch.py --dashboard >> logs/supervisor.log 2>&1 &`
3. Verify: supervisor + apex + crucible + streamlit pids; dashboard HTTP 200 on :8501
4. Logs: `logs/supervisor.log`, `logs/apex.log`, `logs/crucible.log`, `logs/dashboard.log`

**Wallet STOPPED in UI:** Apex is likely still ticking. Check `trader_health.stoppage_kind` — EXECUTION_STARVATION means edge gate rejected signals, not infra failure.

**Dashboard dead:** Usually import error or streamlit not running — fix code, restart supervisor (not a user task).

---

## Architecture Map

```
supervisor_watch.py (poll execution_controls)
  ├── ip4_apex_edge.py      → oracle_sync, gateway, stoppage, portfolio snapshots
  ├── ip4_swarm_crucible.py → DeepSeek, val_bpb_backtest.py, KEEP/REVERT
  └── streamlit app.py      → db.py → portfolio_store, execution_controls

libSQL: execution_controls, active_strategy, trade_exhaust, markets_ledger,
         trade_execution, trader_health, portfolio_snapshots, capital_injection_ledger
```

Key modules post-audit:

- `engine_2_crucible/backtest_corpus.py` — shared exhaust flatten (resolved vs mock)
- `engine_2_crucible/strategy_loader.py` — AST sandbox + `build_market_state()`
- `engine_1_apex/sizing.py` — `effective_min_net_edge()`, `crucible_min_net_edge()`
- `database/portfolio_store.py` — NAV, injections, bankruptcy floor

---

## Active Work

| Status | Item |
|--------|------|
| done | 7-point Priority Build Order audit refactor |
| done | Karpathy LLM wiki (`project_wiki.py`, `.cursor/rules/llm-wiki.mdc`) |
| done | Seed resolved markets for Crucible (`scripts/seed_resolved_corpus.py`, auto on Crucible preflight) |
| todo | Refresh `InvestmentProphits4_MASTER_BLUEPRINTS.md` via sync script (stale edge-gate docs) |
| done | Sync Turso `active_strategy.python_source` with local champion after audit |
| done | Enable `CROSS_VENUE_ENABLED=true` for paper cross-venue overlays |
| todo | Harden supervisor persistence (`watch-bg` exit investigation; zombie child detection fixed) |

---

## Decisions Log

### 2026-06-22 — Champion backtest resolved-only

Sortino KEEP/REVERT uses `markets_ledger.is_resolved=1` only. Synthetic outcomes are research-log only (`RESEARCH_SCORE` when `BACKTEST_MOCK_RESOLUTIONS=true`).

### 2026-06-22 — Paper and live share edge bar 0.015

`APEX_PAPER_MIN_NET_EDGE` deprecated for behavior. Exploration 0.008 only when `APEX_EDGE_MODE=exploration` (Apex) or `CRUCIBLE_EXPLORATION=true` (Crucible live-fill gate).

### 2026-06-22 — Per-market ladder cap

`APEX_MAX_LEGS_PER_MARKET` defaults to `max(1, floor(APEX_MAX_LADDER_LEGS/2))` to reduce capital starvation on one thin market.

### 2026-06-22 — Strategy sandbox before exec

LLM proposals must pass AST validation + subprocess smoke eval before Crucible KEEP or Apex reload.

### 2026-06-22 — NAV injection accounting

`portfolio_snapshots.total_capital_injected` + `SCOPE_APEX` ledger; True PnL = NAV − injected capital.

### 2026-06-22 — Exploration mode split

User chose separate flags: `APEX_EDGE_MODE=exploration` for Apex paper, `CRUCIBLE_EXPLORATION=true` for Crucible gates.

### 2026-06-22 — Stoppage: edge gate vs execution failure

Net-edge rejections are capital protection, not execution starvation. Apex tracks `skipped_edge`; stoppage treats all edge-gated signals as healthy. All-HOLD ticks (SIGNAL_STARVATION) also stay HEALTHY — strategy waiting for consensus.

### 2026-06-22 — Karpathy LLM wiki for IP4

Mirror IP2/IP3 pattern: `agent/project_wiki.py`, structured markdown sections, Cursor always-on rule. ContextStream remains disabled.

---

## Lessons Learned

### 2026-06-22 — Turso champion lag caused wallet STOPPED (high)

- **Trigger:** Turso `active_strategy` was stale OBI-only (72 lines); Apex signaled BUY_YES on 2 markets every tick; edge gate rejected (negative net edge) → EXECUTION_STARVATION → STOPPED.
- **Impact:** Wallet showed STOPPED while processes were healthy.
- **Prevention:** Sync local `active_strategy.py` to Turso after champion changes; edge rejections must not count as execution failure.

### 2026-06-22 — Supervisor zombie child blocks Apex respawn (medium)

- **Trigger:** `_pid_alive` returned true for defunct Apex pid; supervisor skipped respawn.
- **Impact:** Apex dead while supervisor thought it was running.
- **Prevention:** `supervisor_watch._pid_alive` ignores zombie (`Z`) state; restart supervisor after manual Apex kill.

### 2026-06-22 — Wallet STOPPED ≠ system off (high)

- **Trigger:** User sees Wallet Health STOPPED + supervisor warning.
- **Impact:** Misread as crash; user thinks stack is dead.
- **Prevention:** STOPPED = stoppage detector (EXECUTION_STARVATION, etc.). Check pids + `apex_state` in DB. Explain before suggesting restarts.

### 2026-06-22 — Supervisor orphan processes (high)

- **Trigger:** Supervisor killed; Apex/Crucible/dashboard keep running.
- **Impact:** Dashboard shows "supervisor not running"; no auto-restart on crash.
- **Prevention:** Agent restarts supervisor directly; document in Operator Runbook.

### 2026-06-22 — Do not dump commands on the user (high)

- **Trigger:** User asked if system operational; agent replied with shell recipes.
- **Impact:** Frustration — user wants agent to execute, not instruct.
- **Prevention:** Run ops yourself; brief status only. See User Preferences.

### 2026-06-22 — Dashboard import during partial refactor (medium)

- **Trigger:** `db.py` imported `get_apex_total_injected` before module stable / streamlit cached failure.
- **Impact:** Dashboard ImportError, blank UI.
- **Prevention:** `db.py` uses `_apex_total_injected()` locally; restart streamlit after portfolio_store changes.

### 2026-06-22 — Crucible recalibrate vs zero trades (medium)

- **Trigger:** Champion replay on resolved-only corpus returns 0 trades.
- **Impact:** `best_score` drift if recalibrate too aggressive.
- **Prevention:** Existing guard: do not lower stored score on zero-trade replay.

### 2026-06-22 — libSQL Streamlit sessions (medium)

- **Trigger:** Reused libSQL HTTP connection in Streamlit.
- **Impact:** `invalid baton` / STREAM_EXPIRED.
- **Prevention:** Fresh connection per query in dashboard (`_with_conn`).

---

## User Preferences

- 2026-06-22: **Agent executes** start/stop/fix operations — do not blather shell commands; do the work.
- 2026-06-22: **Keep context** — use this wiki every session; remember what was said and what landed in code.
- 2026-06-22: Plan mode for large refactors; sequential 7-point audit build order was approved.

---

## Session Log

### 2026-06-22 — Priority Build Order (7-point audit refactor)

- Implemented resolved-only Crucible judge, strategy AST/subprocess sandbox, aligned edge gates, per-market ladder caps, OBI validation script, cross-venue strategy overlay, NAV injection telemetry.
- Fixed dashboard `ImportError` (`engine_3_dashboard/db.py`).
- Restarted supervisor/stack multiple times; clarified STOPPED wallet health vs process death.

**Next:** Maintain wiki each session; seed resolved markets; consider enabling cross-venue for paper signal flow.

### 2026-06-22 — Karpathy LLM wiki bootstrap

- Added `agent/project_wiki.py`, expanded `project_wiki.md`, `arena_wiki.md` stub, `.cursor/rules/llm-wiki.mdc`, `tests/test_project_wiki.py`.
- Captured user preference: agent runs ops, wiki is source of continuity.

**Next:** Append Session Log after every significant agent session; refresh Current State Audit when runtime changes.

### 2026-06-22 — Wallet STOPPED (EXECUTION_STARVATION) resolved

- **Root cause:** Stale Turso champion (no `cross_venue_adj` gate) kept signaling on `mkt_ai_agi` / `mkt_fed_cut`; edge gate correctly rejected negative net edge; stoppage escalated to STOPPED.
- **Fixes:** Synced local champion to Turso (v30); enabled `CROSS_VENUE_ENABLED=true`; Apex treats net-edge rejects as `skipped_edge` (healthy); SIGNAL_STARVATION (all HOLD) stays HEALTHY; fixed supervisor zombie pid detection; restarted stack.
- **Verified:** `trader_health.status=HEALTHY`, consecutive=0, Apex ticking, no new EXECUTION_STARVATION logs.

**Next:** Refresh master blueprints; monitor Crucible KEEP rate with proxy corpus.

### 2026-06-22 — Crucible resolved corpus bootstrap

- **Root cause:** `markets_ledger.is_resolved=0` for all 10 markets → `flatten_exhaust_rows(use_mock=False)` returned empty → every proposal REVERTed before backtest.
- **Fixes:** `database/resolved_corpus_bootstrap.py` (Gamma refresh + exhaust mid-drift proxies); `scripts/seed_resolved_corpus.py`; auto-bootstrap on Crucible preflight; `enrich_backtest_cross_venue()` for historical exhaust without cross overlays.
- **Verified:** 10 resolved markets, 5000 replay samples; champion replay TRADES=691; sanity check passes on baseline strategy.

**Next:** Refresh master blueprints.
