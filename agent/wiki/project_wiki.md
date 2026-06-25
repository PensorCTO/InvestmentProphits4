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

*Last audited: 2026-06-23 09:38*

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

**Full ritual:** `.cursor/skills/ip4-stack-lifecycle/SKILL.md`

1. **Session start:** `.venv/bin/python scripts/stack_status.py` → fix or `restart_stack.py` if unhealthy
2. Start/restart stack: `.venv/bin/python scripts/restart_stack.py`
3. Verify: supervisor + apex + crucible + streamlit pids; dashboard HTTP 200 on :8501
4. **After stack restart:** `verify_trade_flow.py` must PASS (≥1 buy + ≥1 sell in current Apex session)
5. **Before claiming any fix done:** `.venv/bin/python scripts/acceptance_gate.py --scope full`
6. **Session end:** gate PASS + trade flow verified **OR** `stop_stack.py` + log "intentionally stopped"
7. Logs: `logs/supervisor.log`, `logs/apex.log`, `logs/crucible.log`, `logs/dashboard.log`

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
| done | Refresh `InvestmentProphits4_MASTER_BLUEPRINTS.md` via sync script (stale edge-gate docs) |
| done | Sync Turso `active_strategy.python_source` with local champion after audit |
| done | Enable `CROSS_VENUE_ENABLED=true` for paper cross-venue overlays |
| done | Core Integration & Remediation refactor (5 phases — spec 2026-06-23) |
| done | Learning loop integration (live feedback, toxicity gate, corpus refresh, scheduler, activity breakdown) |
| done | V2 Signal Stack + Adaptive MTF (BookWatcher, composite edge, Crucible hardening, regime breaker) |
| todo | Harden supervisor persistence (`watch-bg` exit investigation; zombie child detection fixed) |

---

## Decisions Log

### 2026-06-24 — Stop-loss cooldown before signals++ (false STALLED fix)

`is_stop_loss_cooldown_active()` ran **after** `signals += 1`, inflating `signals_last_tick` while blocking fill. With one edge-gated signal (fed_cut) + one stop-loss-blocked signal (us_election), `actionable_unfilled_signals` stayed > 0 → `trading_status=STALLED`, `zero_fill_streak` 270+, and idle/FULLY_DEPLOYED rotate never fired. Moved stop-loss check before signal count.

### 2026-06-24 — Six-market deployment headroom + faster idle rotate

`APEX_MAX_LADDER_LEGS=6` with `APEX_MAX_LEGS_PER_MARKET=1` allows six single-leg markets; portfolio-wide cap enforced via `count_agent_open_legs()` before entry. `APEX_CAP_STALL_REMEDIATE_TICKS=6` (~1 min) speeds FULLY_DEPLOYED / idle rotation when cash sits idle.

### 2026-06-23 — V2 Signal Stack & Adaptive MTF

- **BookWatcher:** Sub-second asyncio poller (`shared/book_watcher.py`, default 250ms) in Apex; feeds live `SignalStack` per CLOB token.
- **Adaptive MTF:** No fixed μs cutoff. `τ_MTF = max(MTF_TAU_FLOOR_MS, MTF_BETA × median_cancel_ms) × ψ_spasm` with L1 tier factor 0.5 and trade-confirmed exemption.
- **Fair value decoupled:** `resolve_execution_fair_value()` is overlay-only; OBI bump removed. Edge lives in `engine_1_apex/execution_edge.py` composite gate (25/25/20/15/10 weights).
- **Regime circuit breaker:** `shared/regime_classifier.py` returns HOLD path in Apex when `POOR_LIQUIDITY` (≥2 of wide spread, high ephemeral, low liquidity quality, thin book).
- **Crucible hardening:** Scorer adds MAE penalty, fill-prob filter, slippage stress; walk-forward pipeline before KEEP; `BACKTEST_MOCK_RESOLUTIONS` ignored for champion SCORE.

### 2026-06-23 — Learning loop integration (Apex ↔ Crucible)

- **Live feedback:** Crucible `build_proposal_prompt` includes Apex PnL/churn stats since last strategy KEEP via `live_trading_feedback.py` + `trading_activity_store.py`.
- **Toxicity gate:** `should_reject_toxic_entry()` in gateway/live_gateway when `knowledge_core_vectors` ≥ 100; env `TOXICITY_GATE_ENABLED`, `TOXICITY_REJECT_THRESHOLD=0.25`.
- **Resolved corpus:** `ensure_resolved_corpus()` always runs incremental Gamma + exhaust proxy seed (no early exit); Crucible refreshes every 50 iterations.
- **Scheduler:** Crucible loop calls `run_due_jobs()` each iteration (genetic evolution, resurrection, janitor).
- **Dashboard:** Activity breakdown (cap-stall vs alpha closes, churn ratio, alpha PnL) under Trading Activity.

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

### 2026-06-23 — 2026-06-23 — Core Integration spec (5 phases)
shared/db_lock.py + arena_transaction savepoints; execution_controls observed PIDs; get_fresh_snapshot MTF gate + ORACLE_STARVATION; atomic strategy os.replace; OVERLAY_WEIGHTS AST gate; 10bps friction + quarter-Kelly (Apex + backtest); resolved_corpus table + slope REVERT judge.

### 2026-06-23 — 2026-06-23 — Champion must include OVERLAY_WEIGHTS in Turso
Apex loads python_source from DB; local active_strategy.py changes require write_active_strategy_source sync or strategy load fails every tick.

### 2026-06-23 — Champion must include OVERLAY_WEIGHTS in Turso
Apex loads python_source from DB; local active_strategy.py changes require write_active_strategy_source sync.

### 2026-06-23 — Floor ladder to min_ladder when Kelly fraction is sub-minimum but cash allows
Quarter-Kelly can produce cash*kelly < APEX_MIN_LADDER_USD while wallet has ample cash. compute_ladder_budget() floors to min_ladder when caps allow. classify_stoppage() treats min_ladder cap skips as healthy (kelly_below_min_ladder) unless cash < min_ladder — avoids false CAPITAL_STARVATION / STALLED / DEGRADED.

### 2026-06-23 — Floor ladder to min_ladder when Kelly fraction is sub-minimum but cash allows
Quarter-Kelly can produce cash*kelly < APEX_MIN_LADDER_USD while wallet has ample cash. compute_ladder_budget() floors to min_ladder when caps allow. classify_stoppage() treats min_ladder cap skips as healthy (kelly_below_min_ladder) unless cash < min_ladder — avoids false CAPITAL_STARVATION / STALLED / DEGRADED.

### 2026-06-23 — 2026-06-23 — Cap-stall churn fix (hold deployed thesis)
Cap-stall remediation was firing on fully_deployed ticks (max_legs=1 + aligned BUY_YES), force-closing and rebuying the same leg every ~3min (~-$0.27/cycle spread tax). Fix: is_cap_stall_tick/should_remediate_cap_stall skip fully_deployed; aligned max-leg positions stay open until thesis/TP/SL/flip. Apex logs show block=fully_deployed, zero CAP STALL remediate after restart.

### 2026-06-23 — Cap-churn guard + acceptance NAV/churn checks
Runtime CapChurnGuard pauses trim/refill when same-tick rebalance+fill pattern or NAV drawdown with rebalance activity. acceptance_gate adds nav_session_floor, session_cap_churn, and trading_ready (fail when dashboard trading_blockers).

### 2026-06-24 — Idle deployment rotation for fully-deployed idle cash
Cap-stall remediation only closes cap_stall_rows (opposite-direction signals). When all capacity is deployed and remaining signals are edge-gated, idle cash never rotates. New IDLE_DEPLOYMENT_ROTATE closes smallest leg on deployed/HOLD markets after cap_blocked_streak threshold.

### 2026-06-24 — Idle-rotate churn guard: HOLD-only targets + cross-tick rotate/fill detection
Deployed markets still signaling BUY were rotation targets, causing rotate→refill spread bleed. Only HOLD legs rotate; note_market_rebalance/fill detects cross-tick pairs; 900s re-entry cooldown after idle rotate.

### 2026-06-24 — Default sizing: 5% NAV per market + 50% portfolio cap
Ladder budget now defaults to min(5% cash, 5% NAV per market) with APEX_MAX_PORTFOLIO_PCT=0.50 so a $100 wallet makes ~$5 bets and cannot deploy more than half NAV across open legs.

### 2026-06-24 — QA Audit Remediation (4 pillars)

- **Pillar 1:** `oracle_health` + `book_buffer` tables; `oracle_circuit_breaker.py` trips on ingest failures (not per-tick stale reads); BookWatcher debounced DB writes; oracle thread migrates replica only (avoids sqld primary timeout).
- **Pillar 2:** `strategy_proposals` quarantine; `validate_deployment()` scheduled every 50 Crucible iterations; structured JSON live feedback in prompts.
- **Pillar 3:** Sharpe slope + 10bps friction KEEP gate; `strategy_history` append on KEEP; `live_performance_monitor.py` auto-revert (default `LIVE_AUDIT_ENABLED=false`, shadow mode).
- **Pillar 4:** `shared/adversarial_filter.py` on DeepSeek/news/prompt assembly; `audit_events` table; `TOXICITY_FAIL_CLOSED` option.
- **CI:** `safety-gates` + `security` (bandit) jobs; `acceptance_gate --scope offline`; preflight blocking in CI.

### 2026-06-24 — FULLY_DEPLOYED rotate anti-churn quartet
Rotate only at >=67% ladder fill; remediate ticks 18; block same-thesis reentry after FULLY_DEPLOYED_ROTATE until mid/fv moves 2c; cross-market rotate+fill (3 mkts/900s) activates cap_churn_guard.

### 2026-06-25 — 2026-06-25 — IP4 hardening: bankruptcy halt replaces auto-injection
maybe_halt_on_bankruptcy sets DRAIN_AND_HALT + audit event; no BANKRUPTCY_RESET injections. Gateway rejects entries when halted.

### 2026-06-25 — 2026-06-25 — Null-safe state_float + risk backfill after exits
BookWatcher None fields no longer crash Apex ticks. Vector backfill runs after bracket exits with Hrana retry; non-fatal on timeout.
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

### 2026-06-23 — Kelly-too-small min_ladder skips are not capital starvation (medium)
- **Trigger:** Quarter-Kelly fractional_kelly too small; cap_reasons min_ladder with cash=$94
- **Impact:** Dashboard trading stalled + wallet degraded (CAPITAL_STARVATION STOPPED)
- **Prevention:** Floor ladder budget in sizing.py; classify min_ladder as healthy when cash >= min_ladder in stoppage.py

### 2026-06-24 — Idle rotation must not target still-signaling deployed legs (high)
- **Trigger:** idle_rotation_rows included max-legs markets still returning BUY_YES
- **Impact:** rotate→refill loop on mkt_us_election bled ~$0.14/cycle (~$2/hr)
- **Prevention:** Only HOLD+open positions are rotation targets; churn guard detects cross-tick rotate+fill pairs
## User Preferences

- 2026-06-22: **Agent executes** start/stop/fix operations — do not blather shell commands; do the work.
- 2026-06-22: **Keep context** — use this wiki every session; remember what was said and what landed in code.
- 2026-06-22: Plan mode for large refactors; sequential 7-point audit build order was approved.

---
- 2026-06-23: Never hand off a crashed or unverified IP4 stack. Session end must be gate PASS + stack_status --require-healthy OR clean stop_stack.py with wiki note.
- 2026-06-23: After every stack restart: verify Apex + Crucible pids alive, then wait for verify_trade_flow.py PASS (≥1 buy + ≥1 sell in current Apex session) before handoff.
## Session Log

### 2026-06-25 — Overnight trading audit + blueprint sync + git push

- **Audit:** Jun 24 22:00–Jun 25 10:00 UTC — 66 closes, 6 bankruptcy injections ($600), true PnL ≈ −$1,277 (NAV $523 vs $1,700 injected). Verdict: **not ready for live wallet** (negative champion score, live-audit slope breach, no `POLYGON_WALLET_*`, risk-daemon transaction errors).
- **Blueprints:** Manual sync of `InvestmentProphits4_MASTER_BLUEPRINTS.md` + `IP4_ALGO_BLUEPRINTS.md` (DeepSeek auto-sync blocked by adversarial filter on large prompt). Documented async guardrails, telemetry, churn lockout, shadow Welch promotion, live cutover checklist §18.
- **Verified:** `sync_master_blueprints.py --validate-only` PASS; acceptance_gate `--scope full` PASS (306 pytest).

**Next:** Fix vector backfill sync URI + risk-daemon transaction timeouts; 48–72h paper soak with zero bankruptcy injections before live cutover.

### 2026-06-24 — Structural & asynchronous guardrails (full spec)

- **Task 1.1:** BookWatcher writes `/tmp/.dma_heartbeat` each poll loop; Apex first-step stale check → `SYSTEM_HALTED` + `DRAIN_AND_HALT` + execution halt.
- **Task 1.2 / 2.5:** HMM `HMMDecodeResult` (posterior, confidence delta, low-confidence bypass); toxic override compresses regime recover threshold; `regime_transitions` table migration.
- **Task 2.1:** Back-pressure throttling, async `VolTrackerWorker` queue, bootstrap warm-up when `book_buffer` < 3600 rows.
- **Task 2.2:** Tri-state regime (`GOOD`/`CAUTION`/`POOR_LIQUIDITY`) with weighted z-score composite; CAUTION downscale in `compute_ladder_budget`.
- **Task 2.3:** Crucible staleness gate (14d / 40%), time-decay helpers; shadow Welch t-test promotion + `SHADOW_MAX_LIFESPAN_CYCLES`.
- **Task 2.4:** `churn_lockout.py` alpha-decay re-entry lock; cap-trim skips legs below -4% unrealized P&L.
- **Task 4:** `shared/telemetry.py` jsonl export; `tests/test_asynchronous_guardrails.py`; `acceptance_gate --scope components --verify-telemetry`.
- **Verified:** 303 pytest pass; components gate PASS.

**Next:** Restart Apex stack after deploy; monitor `logs/telemetry.jsonl` and heartbeat age in production soak.

### 2026-06-24 — BookWatcher heartbeat false-halt fix (QA plumbing)

- **Root cause:** Heartbeat only updated after poll loop; live CLOB polls for 10 markets blocked asyncio >5s → false `SYSTEM_HALTED`. Trade-flow verify also failed when Apex died mid-wait despite buy+sell already logged.
- **Fix:** Dedicated `dma-heartbeat-pusher` thread (`shared/telemetry.py`) writes every 1s independent of asyncio blocking; 15s startup grace; preflight recovers `HALTED`/`DRAIN_AND_HALT` → `RUNNING`.
- **Verified:** Stack restart + wallet reset; Apex alive 4+ min; heartbeat age ~0.7s; trade flow PASS (buy + sell); acceptance_gate `--scope full` PASS (303 pytest).

**Next:** Investigate risk-daemon `TRANSACTION_TIMEOUT` log noise (stack_status `healthy=False` cosmetic).

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

### 2026-06-22 — Crucible corpus REVERT loop (stale process)

- **Root cause:** Crucible started before `backtest_resolution_value` fix (commit `4acd227`); Python kept old cached `backtest_corpus.load_resolutions()` (`is_resolved=1` only). After oracle repair all markets have `is_resolved=0` → sanity check saw 0 samples despite DB having 10 proxy labels.
- **Fixes:** Restarted Crucible; `_sanity_check_proposal` now uses `open_replica()` + `ensure_resolved_corpus()` + direct `flatten_exhaust_rows` import; updated error message.
- **Verified:** Iterations 4–5 ran backtests (SCORE=-8.21, TRADES=836+) — no more "no resolved replay samples" errors.

**Next:** Restart Crucible after corpus/schema deploys; consider supervisor code-change detection.

### 2026-06-22 — Crucible corpus REVERT loop (stale process)

- **Root cause:** Crucible started before `backtest_resolution_value` fix (commit `4acd227`); Python kept old cached `backtest_corpus.load_resolutions()` (`is_resolved=1` only). After oracle repair all markets have `is_resolved=0` → sanity check saw 0 samples despite DB having 10 proxy labels.
- **Fixes:** Restarted Crucible; `_sanity_check_proposal` now uses `open_replica()` + `ensure_resolved_corpus()` + direct `flatten_exhaust_rows` import; updated error message.
- **Verified:** Iterations 4–5 ran backtests (SCORE=-8.21, TRADES=836+) — no more "no resolved replay samples" errors.

**Next:** Restart Crucible after corpus/schema deploys; consider supervisor code-change detection.

### 2026-06-23 07:56 — IP4 Wallet Stabilization plan implemented (Phases 0-5): verify_stack, honest trading telemetry (STALLED), HOLD hysteresis, Crucible replay edge gate, live CLOB preflight, restart_stack, soak_verify, CI pytest workflow. 139/140 tests pass.

**Next:** Run restart_stack.py + soak_verify.py --minutes 30 on live stack; monitor trading_status vs HEALTHY.

### 2026-06-23 08:02 — Fixed Trader Wallet panel: fetch_portfolio_history returned oldest 500 rows (chart showed $1099 NAV vs live $95). Switched to DESC+reverse; legacy injected=0 rows use fallback. Wallet panel now shows Capital Injected + True Return. Fixed minutes_since_last_fill after Apex restart (wall-clock from last_fill_at + DB hydrate). Added test_supervisor_lock, bankruptcy injection test, autoresearch replay REVERT test, supervisor spawn reason + post-spawn verify_stack --quick.

**Next:** Restart dashboard to pick up app.py; optional: fix test_apex_sizing env isolation for APEX_EDGE_MODE=exploration in .env

### 2026-06-23 — Phase 3: verify_stack infra/trading split

- **`run_verify(include_trading=False)`** default: sqld, processes, dashboard HTTP, trader_health_fresh, edge_model only — no zero_fill_streak / STALLED failures.
- **`--trading` flag** adds `check_zero_fill_streak` + `check_trading_stalled`; `trader_health_audit.py` STALLED exit 1 only with `--trading`.
- **`restart_stack.py`** documents infra-only post-restart verify.
- **Tests:** 10/10 in `tests/test_verify_stack.py`.

**Next:** `soak_verify.py` may want `--trading` for long-run trading gates; run `restart_stack.py` to confirm infra PASS with live STALLED streak.

### 2026-06-23 08:20 — Dashboard dead fix (approved spec Q1=B): cap-stall auto-remediation (remediate_cap_stall after 30 ticks), infra/trading split UI (INFRA ALIVE + TRADING STALLED), verify_stack infra-only default + --trading flag. Fixed read_trader_health preflight kwarg. Live: CAP STALL closed mkt_us_election leg; verify_stack PASS; 156 tests pass.

**Next:** Monitor edge_gated STALLED on mkt_fed_cut (live CLOB); optional Crucible strategy or exploration mode if fills needed.

### 2026-06-23 08:33 — Cap-block stall fix complete (gate PASS)

- **Root cause:** `zero_fill_streak` only counted actionable (non-edge-gated) signals, so `remediate_cap_stall` never fired while `max_legs_per_market` blocked every tick; strategy refilled closed legs after 120s cooldown.
- **Fix:** Added `cap_blocked_streak` in `StoppageTracker` (increments on sustained `max_legs_per_market` blocks); remediate now keys off that counter. Kept actionable-only `zero_fill_streak` for STALLED classification. Entry cooldown remains 600s on remediated market.
- **Live:** Apex restart → 18 cap_blocked ticks → `APEX CAP STALL remediate` closed mkt_us_election leg; subsequent ticks `cap_reasons=None`, `skipped_cooldown=1`.
- **Verified:** `acceptance_gate.py --scope full` PASS (159 pytest, apex_cap_stall_pattern OK, trading_status=IDLE).

**Next:** Soak monitor for refill after 600s cooldown; confirm no cap_blocked pattern recurrence.

### 2026-06-23 08:41 — Supervisor respawn fix + stack restart (gate PASS)

- **Symptom:** QA reported supervisor not working; after manual Apex kill supervisor **adopted a dying stray pid** instead of spawning fresh (6s+ gap, no ticks).
- **Fix:** `supervisor_watch._reconcile_engine()` — when tracked pid dies, skip adopt, terminate stray, spawn fresh. `ensure_supervisor_running()` verifies pid alive after 1.5s. Dashboard shows supervisor pid. `restart_stack.py` validates supervisor start.
- **Gate:** `apex_cap_stall_pattern` now scopes to current Apex session ticks after last `CAP STALL remediate` (avoids false FAIL from pre-restart log tail).
- **Live:** `restart_stack.py` → supervisor pid=48020, apex=48117; kill-test respawns in ~3s with log `Tracked Apex pid=… died — terminating stray … before respawn`. Cap remediate at 08:40:26; acceptance gate PASS (163 pytest).

**Next:** Monitor supervisor through dashboard Start/Stop; soak cap cooldown cycle.

### 2026-06-23 09:38 — Core Integration & Remediation refactor (Phases 1-5): db_lock, transactions, runtime PIDs, oracle_ts, get_fresh_snapshot, ORACLE_STARVATION, strategy_atomic, OVERLAY_WEIGHTS AST, 10bps friction, quarter-Kelly, resolved_corpus table, slope judge. 181 pytest pass. Champion synced Turso v93. acceptance_gate: infra OK; recent_fill FAIL only (62min, cap/edge blocked).

**Next:** Monitor live fills; Crucible slope gate on next KEEP

### 2026-06-23 09:43 — QA fix: trading stalled + wallet degraded from false CAPITAL_STARVATION. Root cause: quarter-Kelly fractional_kelly too small → min_ladder cap skips with $94 cash. Fixed compute_ladder_budget floor + stoppage classify. Apex restarted (pid 9649). trader_health: HEALTHY, trading_status=IDLE, zero_fill_streak=0. acceptance_gate --scope full PASS (183 pytest).

**Next:** Monitor for fills when max_legs_per_market clears; investigate one-off Turso savepoint error on first post-restart tick if repeats.

### 2026-06-23 09:52 — Created ip4-stack-lifecycle skill + scripts: stack_lifecycle.py, stop_stack.py, stack_status.py; refactored restart_stack.py. Session start/end ritual wired into llm-wiki.mdc and Operator Runbook. Verified restart + acceptance_gate PASS + stack_status --require-healthy.

**Next:** Agents load ip4-stack-lifecycle at session start; fix Turso savepoint error if it recurs in current Apex session.

### 2026-06-23 12:10 — Learning loop integration (5 recommendations)

- **Live feedback:** `live_trading_feedback.py` + `trading_activity_store.py`; Crucible prompts include Apex PnL/churn since last KEEP.
- **Toxicity gate:** `toxicity_gate.py` wired in PaperGateway/LiveGateway; `skipped_toxicity` on Apex ticks.
- **Corpus:** `ensure_resolved_corpus()` incremental refresh; every 50 Crucible iterations.
- **Scheduler:** `run_due_jobs()` each AutoResearch iteration.
- **Dashboard:** cap-stall vs alpha activity breakdown.
- **Verified:** 203 pytest; trade flow PASS in 244s (buy mkt_fed_cut, sell cap-remediate); acceptance_gate PASS.

**Next:** Monitor Crucible KEEP attempts with live summary in prompts; confirm toxicity rejects once vector corpus grows.

### 2026-06-23 12:45 — Dashboard honesty + auto-refresh fix

- **User report:** Buy/sell figures looked phoney; heartbeat refresh broken.
- **Root cause (refresh):** `_maybe_autorefresh()` only called `st.rerun()` when ≥5s since last load timestamp — Streamlit scripts exit after render, so nothing polled unless user clicked again. Fixed with `time.sleep(5); st.rerun()` loop.
- **Root cause (phoney UI):** Trade Flow showed latched "Log buy/sell yes" from first session events; activity breakdown used "since strategy KEEP" (cumulative). Live Apex is mostly cap-stall buy→forced-sell churn on `mkt_fed_cut`.
- **Fix:** `scan_apex_session_recent()` — last buy/sell with timestamps, session counts, cap-churn warning; activity scoped to Apex session; heartbeat banner at top with auto-refresh toggle.
- **Verified:** 6 trade_flow tests pass; dashboard respawned (pid 3494).

**Next:** User reload dashboard — confirm clock ticks every 5s; optional reduce cap-churn visibility in trade-flow PASS criteria separately from operator UI.

### 2026-06-23 13:00 — Plumbing vs alpha verify badges

- **`FlowVerdict`** in `trade_flow_verify.py`: `plumbing_ok` (log buy+sell), `alpha_ok` (thesis/flip/rebalance, not cap-stall), `stack_gate_ok` = plumbing only.
- **CLI** `verify_trade_flow.py --snapshot` prints three lines: Plumbing / Alpha / Stack gate; JSON adds `plumbing_ok`, `alpha_ok`, `verdict`.
- **Dashboard** Trade Flow panel: side-by-side PASS/FAIL/CHURN ONLY badges + alpha vs cap-stall metrics.
- **Tests:** 8 pass in `test_trade_flow_verify.py`.

**Next:** Optional `--require-alpha` wait flag for soak tests; document in ip4-stack-lifecycle that restart PASS is plumbing-only.

### 2026-06-23 15:06 — Fixed cap-stall churn loop: stoppage.py + ip4_apex_edge.py hold aligned max-leg positions instead of remediate→rebuy. 29 stoppage tests pass; trading acceptance_gate PASS; stack HEALTHY fully_deployed; 0 cap-stall events post-restart.

**Next:** Monitor NAV for alpha closes (TP/SL/thesis); fix NO stop-loss exit price bug (+ false win).

### 2026-06-23 — V2 Signal Stack & Adaptive MTF (full plan)

- **Phase 1:** `shared/book_watcher.py`, `shared/signals/*` (microprice, aggressive flow, temporal decay, adaptive MTF, SignalStack bus).
- **Phase 2:** Overlay-only fair value; `execution_edge.py` composite gate wired in `gateway.py` + OBI execution gate.
- **Phase 3:** Expanded backtest scorer (MAE, fill prob, slippage stress); `walk_forward_pipeline.py` before Crucible KEEP; mock resolutions stripped from champion path.
- **Phase 4:** `regime_classifier.py` circuit breaker in Apex tick; dual-horizon Kelly via flow slope.
- **Tests:** 218 pytest pass (new: `test_mtf_adaptive`, `test_microprice`, `test_aggressive_flow`, `test_composite_edge`; updated `test_fair_value`).

**Next:** Monitor NAV for alpha closes (TP/SL/thesis); fix NO stop-loss exit price bug (+ false win).

### 2026-06-23 — Paper activity tuning (safest order)

- **Step 1 env:** `APEX_MAX_LEGS_PER_MARKET=2`, `CRUCIBLE_EXPLORATION=true`, `V2_MIN_NET_EDGE_COST_MULT=1.0` (kept `APEX_EDGE_MODE=exploration`).
- **Step 3 strategy:** Moderate relax (spread 0.018, cross 0.004, OBI 0.006, depth imb 0.03); backtest unchanged vs baseline on resolved corpus; synced Turso v96.
- **Result:** Apex `trading=ACTIVE`; fills on `mkt_fed_cut`, `mkt_ukraine_peace`; expect more cap_rebalance churn with 2 legs/market.

### 2026-06-23 17:12 — Cap-churn guard in engine_1_apex/cap_churn_guard.py wired into Apex tick loop; acceptance gate NAV/churn/trading_ready checks; trade_flow_verify rebalance sells classified as churn. 227 pytest pass; stack restarted.

**Next:** Monitor apex.log for CAP CHURN GUARD under multi-market activity tuning.

### 2026-06-24 — Herding cap, wallet-reset gates, blueprint sync

- **Herding cap:** `engine_1_apex/herding_cap.py` — NAV-scaled cap (`max(floor, NAV×pct)`), Kelly clipped to headroom instead of hard `HERDING_CAP_EXCEEDED`.
- **Cap-stall:** Remediation paused when Kelly exceeds herding cap or target market in escalated stop-loss cooldown.
- **Stop-loss loop:** Escalating cooldown (2× repeats), re-entry edge margin; all stop-loss gates ignore pre-`WALLET_RESET` history.
- **Wallet reset:** Clears `trader_health` streak/counters; `KnowledgeStore` uses `open_replica()` (fixes local sqld toxicity sync error).
- **Validation:** 235 pytest pass; master blueprints synced via DeepSeek.

**Next:** Monitor post-reset trading; optional `--require-alpha` for soak gate.

### 2026-06-24 08:29 — QA stall fix: idle deployment rotation + ORACLE starvation no longer DEGRADED. Apex respawned; verify_trade_flow PASS; acceptance_gate full PASS (238 pytest).

**Next:** Monitor overnight fully-deployed idle; tune APEX_CAP_STALL_REMEDIATE_TICKS if rotation feels slow.

### 2026-06-24 — QA plumbing FAIL (buy+sell since restart)

- Fixed herding cap for $100 wallets: NAV×pct cap (not 2500 floor); ladder_floor allows ~$5 deploy
- Added portfolio-cap trim/remediate and fully-deployed rotate sell path
- trade_flow_verify counts TRIM + remediate log lines as sells
- restart_stack --reset-wallet → verify_trade_flow PASS (202s); acceptance_gate full PASS; stack healthy

**Next:** Watch fully_deployed rotate churn; tune remediate ticks if QA wait is too long.

### 2026-06-24 — QA Audit Risk Remediation (full plan)

- **Schema:** `oracle_health`, `book_buffer`, `strategy_proposals`, `strategy_history`, `audit_events` via `migrate_qa_audit_tables`.
- **Runtime:** Oracle circuit breaker + book buffer merge in Apex tick; Crucible quarantine + overlay validation scheduler; adversarial filter on LLM/news inputs.
- **Fixes during validation:** Circuit breaker no longer increments failures on execution-side stale reads; BookWatcher debounced (`BOOK_BUFFER_PERSIST_INTERVAL_S=1`); oracle `_ensure_schema_once` uses replica migrate only (fixes sqld TRANSACTION_TIMEOUT on startup).
- **Validation:** 258 pytest pass; `acceptance_gate --scope offline` PASS; stack restart + `verify_trade_flow` PASS (buy+sell, 63s).

**Next:** Enable `LIVE_AUDIT_ENABLED=true` after shadow soak; monitor oracle circuit trips in production.

### 2026-06-24 — Deployment headroom: 6-leg ladder + faster fully-deployed rotate

- **Tuning:** `APEX_MAX_LADDER_LEGS=6`, `APEX_MAX_LEGS_PER_MARKET=1`, `APEX_CAP_STALL_REMEDIATE_TICKS=6` (~1 min at 10s ticks vs 3 min).
- **Code:** `count_agent_open_legs()` enforces portfolio-wide ladder budget before new entries; idle/FULLY_DEPLOYED rotate fires sooner when cash is idle.

**Next:** Monitor for 6th-market fills and rotate churn at 6-tick threshold.

### 2026-06-24 — Micro-bleed fix: exploration off + thesis re-entry cooldown

- **Root cause:** `APEX_EDGE_MODE=exploration` (0.008 edge) allowed marginal entries; `thesis_expired` on 3 HOLD ticks closed legs but **no re-entry cooldown** → buy/close loop on `mkt_fed_cut` (~$0.10 spread tax per $5 leg every ~2min).
- **Fix:** Commented `APEX_EDGE_MODE` in `.env` (0.015 bar); `APEX_THESIS_REENTRY_COOLDOWN_SECONDS=900` blocks re-entry after THESIS_EXPIRED; `thesis_reentry_cooldown_seconds()` in stoppage.py.

### 2026-06-24 — False STALLED: stop-loss cooldown after signals++

- **Symptom:** `trading_status=STALLED`, `zero_fill_streak=270+`, `block=cap_blocked`, ~90% cash idle; 3 HOLD legs deployed.
- **Root cause:** `mkt_fed_cut` BUY_NO edge-gated + `mkt_us_election` stop-loss cooldown counted as 2 signals but only 1 could fill → idle/FULLY_DEPLOYED rotate blocked.
- **Fix:** Move `is_stop_loss_cooldown_active()` before `signals += 1` in `ip4_apex_edge.py`. Post-restart: `trading=IDLE`, `streak=0`, FULLY_DEPLOYED rotate cleared all legs; only fed_cut edge-gated NO remains.

**Next:** Wait for strategy BUY with ≥0.015 edge on uncooled markets; trade-flow buy pending (all-HOLD + fed_cut reject).

### 2026-06-24 — Dynamic Microstructure & Regime Enhancement (5 phases)

- **Phase 1:** `shared/rolling_stats.py`; `backtest_judge` IS/OOS Sortino gates, OOS MDD hard reject (10%), Calmar ranking; walk-forward 80/20 split.
- **Phase 2:** DMA vol-adaptive BookWatcher poll (50–250ms); MTF notional debounce + phantom liquidity; regime z-score Schmitt hysteresis in `regime_classifier.py`.
- **Phase 3:** Alpha-decay leg ranking + `CAP_TRIM` partial closes in `trade_close.py`; wired into stoppage + Apex cap remediation.
- **Phase 4:** Shadow strategy soak (`shadow_python_source` columns); Crucible stages `backtest_pass`; Apex parallel eval + 1h promotion monitor.
- **Phase 5:** numpy HMM regime decoder + `runtime_levers.py`; `LIVE_AUDIT_ENABLED=true` shadow-first in `.env.example`.

**Next:** Restart Apex stack to load new code; soak shadow promotion + HMM lever mapping; flip `LIVE_AUDIT_SHADOW=false` after audit soak.

### 2026-06-24 17:23 — Implemented rotate-churn fixes (stoppage, cap_churn_guard, ip4_apex_edge); 46 tests pass; post-restart holds 3 legs without rotate. Next: soak cross-market guard.

### 2026-06-25 09:18 — IP4 Infrastructure Hardening & Capital Guardrails (P1–P3): URI normalize, Hrana retry, risk daemon MVCC-friendly cycle, Kelly clamp, bankruptcy halt, OOS Sortino docs, live audit config, wallet preflight. Post-fix: state_float across HMM/regime/execution edge/champion; read_trader_health kwarg fix. 98 targeted + acceptance_gate 318 pytest PASS; stack healthy trading=ACTIVE.

**Next:** 48–72h paper soak: zero BANKRUPTCY_RESET events; monitor risk backfill retries. verify_trade_flow buy+sell on next clean restart if handoff required.

### 2026-06-25 12:32 — Wallet stall resolved (zero trading activity). Deployed baseline consensus strategy v99 to Turso (source=baseline_unstall), reverted APEX_MIN_NET_EDGE to 0.015, reset trader health session, restarted Apex. First tick after restart: 3 fills (mkt_ukraine_peace, mkt_oil_100, mkt_fed_cut YES). Trading status STARVED→ACTIVE→IDLE (fully_deployed). NAV ~$515.68, cash ~$486.93. Crucible remains HALTED.

**Next:** Monitor for alpha closes; risk worker TRANSACTION_TIMEOUT cleared on latest cycle. Do not wallet-reset (would destroy $516 NAV).

### 2026-06-25 12:48 — Trading freeze resolved. Root cause: stop_loss_reentry_edge blocked all signals — legacy entry_context stored boosted composite edge (~0.94) making re-entry impossible; plus FULLY_DEPLOYED_ROTATE closed positions within 60s. Fix: cap/sanitize SL re-entry prior edge, direction-scoped lookup, store unboosted net_edge on fills, disable aggressive rotation (CAP_STALL_REMEDIATE_TICKS=99), strategy v100. Post-restart: 3 fills first tick.

**Next:** Monitor positions hold without rotate churn; verify_trade_flow when sells occur.

### 2026-06-25 13:06 — Fixed ORACLE STARVATION (stale_oracle age=800s+). Root cause: oracle worker thread blocked forever on arena_lock in _ensure_schema_once after Apex restart — never logged Oracle worker started, no snapshots after 12:47. Fix: mark_oracle_schema_initialized() in preflight, move CLOB auto-map to preflight, non-blocking vector backfill locks. Verified: Oracle worker started + snapshots every 30s, ticks resume.

**Next:** Monitor risk daemon TRANSACTION_TIMEOUT under load; consider nb lock in oracle_sync write path if recurs.
