# InvestmentProphits4 — Master Blueprints

**Version:** June 2026 checkpoint (QA audit remediation, oracle circuit breaker, deployment headroom tuning, dual-engine Turso/libSQL arena, Karpathy AutoResearch Crucible, Apex Edge execution, Streamlit Command Center, trader health stoppage detection, live Polymarket CLOB when `EDGE_MODEL_MOCKED=false`)  
**Scope:** Paper-first Polymarket-inspired binary prediction market trading with optional LIVE execution scaffold. No real capital unless explicitly switched to LIVE mode with wallet keys.

This document describes **what IP4 is**, **how it works end-to-end**, and **exactly what the trading strategy is** — from oracle snapshots through Crucible research, Apex paper fills, dashboard control plane, and observability. It reflects the codebase as implemented at this checkpoint, not aspirational backlog.

**Maintained by:** `scripts/sync_master_blueprints.py` (DeepSeek `deepseek-v4-flash` on workflow dispatch) + validation on every push via GitHub Actions.

---

## Executive Summary

InvestmentProphits4 (IP4) is a **dual-engine paper arena**:

| Engine | Process | Role |
|--------|---------|------|
| **Engine 1 — Apex Edge** | `engine_1_apex/ip4_apex_edge.py` | Oracle sync → `trade_exhaust` snapshots → read `active_strategy` from Turso → fair value + net edge gates → paper (or LIVE) fills |
| **Engine 2 — Crucible** | `engine_2_crucible/ip4_swarm_crucible.py` | Karpathy loop: DeepSeek proposes full `active_strategy.py` → `val_bpb_backtest.py` Sortino judge → keep/revert → push winners to Turso |
| **Engine 3 — Command Center** | `engine_3_dashboard/app.py` (Streamlit) | DB-driven controls, wallet NAV, engine process status, logs |

State lives in **libSQL** — local `turso dev` sqld primary (`data/ip4_sqld_primary.db`) or Turso Cloud when credentials are set. A **supervisor** (`scripts/supervisor_watch.py`) spawns/stops Apex and Crucible based on the `execution_controls` singleton row. **Dashboard Start/Stop buttons only update that row**; they do not spawn OS processes without the supervisor running.

---

## 0. The Boondoggle — Honest Operator's Guide

IP4 is simpler than IP3 (no 24-agent quadrant swarm), but the **control plane is easy to misunderstand**. If Apex "won't start" after clicking Start in the dashboard, the DB flag is RUNNING but **no supervisor is watching**.

### Common failure modes (June 2026)

| Symptom | Root cause | Fix |
|---------|------------|-----|
| Dashboard `ERR_CONNECTION_REFUSED` | Streamlit not running | `./scripts/ip4_supervisor.sh watch --dashboard` or `./scripts/ip4_supervisor.sh dashboard` |
| Dashboard crash `NameError: col1` | Broken Streamlit layout (fixed checkpoint) | Pull latest; run `pytest tests/test_dashboard_app.py` |
| Dashboard DB errors `invalid baton` | Cached libSQL HTTP session | Fixed: fresh connection per query in `engine_3_dashboard/app.py` |
| Apex/Crucible won't start from UI | No supervisor process | `./scripts/ip4_supervisor.sh watch --dashboard` |
| `filled=0` for hours | Edge gates, ladder caps, `max_legs`, strategy HOLD — **not** wall-clock market hours; IP4 has no session clock | See stoppage kinds; check `dominant_block_reason` |
| `trading=STALLED` with edge-gated signals | Stop-loss cooldown counted after `signals++` inflated actionable streak (fixed) | Pull latest; restart Apex |
| Cash ≈ $0, `skipped_cap` | Over-laddering on 1–2 markets | `APEX_MAX_LADDER_LEGS`, `count_agent_open_legs()`, cap rebalance in `trade_close.py` |

### Minimum sane operator workflow

```bash
cd InvestmentProphits4
python3 scripts/ip4_bootstrap.py          # once
./scripts/ip4_supervisor.sh watch --dashboard
# Dashboard: http://127.0.0.1:8501
tail -f logs/apex.log logs/crucible.log logs/supervisor.log
```

**One supervisor instance.** Do not run `both --dashboard` and `watch --dashboard` concurrently.

---

## 1. System Purpose

InvestmentProphits4 exists to:

1. **Ingest live Polymarket market state** (Gamma + CLOB) into rolling `trade_exhaust` snapshots for backtest replay and Apex ticks.
2. **Research strategy code** via Crucible AutoResearch — LLM edits `evaluate_market()` in `active_strategy.py`, backtest judge scores Sortino on replay rows.
3. **Execute paper trades** on a single Apex agent (`APEX_EDGE`) using the champion strategy stored in `active_strategy.python_source`.
4. **Gate fills** with fair value (mid + OBI bump), synthetic spread/slippage (`shared/poly_costs.py`), min net edge, position caps, max ladder legs, and cooldown rules.
5. **Expose a control plane** — kill switch, execution mode (PAPER/LIVE), Apex/Crucible run states, portfolio chart, wallet health stoppage telemetry.
6. **Detect wallet stoppages** inline (signal/execution/capital starvation) and auto-remediate capital lock-up where possible.

Lineage: conceptual descendant of IP2/IP3 agent arenas, but **standalone codebase** — no shared venv or DB with IP3.

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  scripts/ip4_supervisor.sh watch --dashboard                              │
│    └── scripts/supervisor_watch.py (poll execution_controls every 3s)    │
│          ├── spawn/stop engine_1_apex/ip4_apex_edge.py                   │
│          ├── spawn/stop engine_2_crucible/ip4_swarm_crucible.py            │
│          └── scripts/dashboard_service.py (HTTP health → Streamlit)      │
└──────────────────────────────────────────────────────────────────────────┘
         │                              │                         │
         ▼                              ▼                         ▼
   Oracle + Apex tick              AutoResearch loop          Streamlit UI
   (10s exec interval)            (DeepSeek → backtest)      (reads/writes DB)
         │                              │
         └──────────────┬───────────────┘
                        ▼
              libSQL (local sqld or Turso Cloud)
              execution_controls, active_strategy, trade_exhaust,
              agent_archetypes, open trades, trader_health, portfolio_history
```

### Apex tick loop (simplified)

1. Oracle worker writes `trade_exhaust` payload (per-market CLOB mid, spread, depth, overlays). BookWatcher debounces writes to `book_buffer`; oracle circuit breaker can halt ingest on sustained failures.
2. Load `active_strategy.python_source` from Turso → `evaluate_market(state)` per market.
3. Entry gates **before** signal count: entry cooldown, stop-loss cooldown, per-market max legs, portfolio-wide `APEX_MAX_LADDER_LEGS` via `count_agent_open_legs()`.
4. Fair value + net edge vs `APEX_MIN_NET_EDGE` (default 0.015; exploration 0.008 only when `APEX_EDGE_MODE=exploration`).
5. `PaperGateway` simulates fills; updates cash/NAV; ladder + cap checks.
6. Stoppage detector → `trader_health` row (`dominant_block_reason` includes `fully_deployed`, `edge_gated`, `cap_blocked`, `all_hold`).
7. **Cap-stall remediation** (`remediate_cap_stall`) only when `should_remediate_cap_stall()` — genuine `cap_blocked`, not aligned max-leg holds.
8. **Idle / fully-deployed rotation** closes smallest HOLD leg when cash is idle and no actionable fills (`IDLE_DEPLOYMENT_ROTATE`, `FULLY_DEPLOYED_ROTATE`) after `APEX_CAP_STALL_REMEDIATE_TICKS`.
9. Optional capital-starvation rebalance trim (`remediate_stoppage`). Cap-churn guard pauses rotate/refill loops.

### Crucible loop (simplified)

1. Read champion from `active_strategy.py.bak`.
2. DeepSeek proposes full replacement `active_strategy.py`.
3. Run `val_bpb_backtest.py` → `TRADES:N` / `SCORE:x.xxxx`.
4. If score > `best_score` and live fill-eligible gate passes → KEEP (write Turso, bump version).
5. Else REVERT to backup.

---

## 3. The Core Strategy (What We Are Actually Betting On)

The **live strategy** is whatever Python source Crucible last KEEP'd into `active_strategy.python_source`. Apex imports it dynamically each tick.

At checkpoint, the strategy pattern is **OBI + depth gate**:

- Require `bid_depth` / `ask_depth` in market state (snapshot contract enforced by `tests/test_snapshot_contract.py`).
- Compute order-book imbalance; map to `BUY_YES`, `BUY_NO`, or `HOLD`.
- Longshot and spread filters vary by Crucible evolution.

**Human mandate only:** `engine_2_crucible/strategy_instructions.md`  
**LLM-editable:** `engine_2_crucible/active_strategy.py`  
**Never LLM-editable:** `engine_2_crucible/val_bpb_backtest.py`

---

## 4. Signal Pipeline (Oracle → Market State)

| Step | Module | Output |
|------|--------|--------|
| Gamma map | `data/gamma_market_map.json` | condition_id → CLOB token IDs |
| CLOB fetch | `shared/polymarket_clob.py` | mid, spread, depth |
| Overlays | oracle sync in Apex | RSS/trend overlays when live |
| Snapshot | `database/market_state_store.py` | `trade_exhaust` JSON blob incl. `bid_depth`, `ask_depth` |
| Strategy input | `engine_2_crucible/strategy_loader.py` | `build_market_state()` dict |

When `EDGE_MODEL_MOCKED=true`, synthetic depth/OBI cycles apply for offline dev. Production checkpoint uses **`EDGE_MODEL_MOCKED=false`** for live CLOB with paper execution.

---

## 5. Agent Arena (Execution Loop)

IP4 runs a **single execution agent** for paper/LIVE:

| Field | Value |
|-------|-------|
| Agent ID | `APEX_EDGE` (env `APEX_AGENT_ID`) |
| Initial capital | `APEX_INITIAL_CAPITAL` (default $100; wallet may differ after trading) |
| Sizing | Fractional Kelly + `APEX_MAX_POSITION_PCT` + portfolio-wide `APEX_MAX_LADDER_LEGS` + per-market `APEX_MAX_LEGS_PER_MARKET` |
| Gateway | `engine_1_apex/gateway.py` (paper), `live_gateway.py` (LIVE scaffold) |

Legacy seed data includes 24 quadrant archetypes in `agent_archetypes` for schema compatibility; **Apex tick loop trades only `APEX_EDGE`**.

---

## 6. Knowledge Layer

IP4 inherits embedding config (`OLLAMA_*`, `EMBEDDING_MODEL`) for future vector features. **Checkpoint execution does not block fills on vector RAG** the way IP3 does. Risk daemon may attempt vector backfill; failures are logged non-fatally.

---

## 7. Risk Daemon (Fast Loop)

`engine_1_apex/risk_daemon.py` runs on interval (`ARENA_RISK_INTERVAL`, default 45s):

- Bracket / thesis exits on open positions.
- Optional committee toxicity (disabled by default: `COMMITTEE_ENABLED=false`).

---

## 8. Transaction Cost Model

`shared/poly_costs.py` — tier-aware synthetic spread and slippage. Net edge subtracts costs before fill approval. Paper mode can use stronger OBI fair bump via `engine_1_apex/fair_value.py` (`is_paper_execution()`).

Key env thresholds:

| Variable | Default | Meaning |
|----------|---------|---------|
| `APEX_MIN_NET_EDGE` | 0.015 | Live/minimum edge bar |
| `APEX_PAPER_MIN_NET_EDGE` | 0.015 | Paper execution edge bar (deprecated; uses `APEX_MIN_NET_EDGE` unless `APEX_EDGE_MODE=exploration`) |
| `APEX_EXPLORATION_MIN_NET_EDGE` | 0.008 | Exploration mode edge bar |
| `APEX_MIN_LADDER_USD` | 5.0 | Minimum notional per ladder leg |
| `APEX_MAX_LADDER_LEGS` | 6 (dev `.env.example`) | Max concurrent open legs portfolio-wide |
| `APEX_MAX_LEGS_PER_MARKET` | 1 (override) | Per-market leg cap; default `max(1, floor(APEX_MAX_LADDER_LEGS/2))` |
| `APEX_CAP_STALL_REMEDIATE_TICKS` | 6 | Ticks before idle/cap-stall rotate (~1 min at 10s ticks) |
| `APEX_THESIS_REENTRY_COOLDOWN_SECONDS` | 900 | Block re-entry after `THESIS_EXPIRED` close |
| `APEX_MAX_PORTFOLIO_PCT` | 0.50 | Max NAV fraction deployed across open legs |

---

## 9. Prime Apex Meta-Agent

**Not implemented in IP4.** IP3 Prime lanes are out of scope. IP4 focuses on single-agent Apex + Crucible research.

---

## 10. Genetic Evolution

**Not implemented as IP3 intra-quadrant cull-and-breed.** IP4 strategy evolution is entirely via **Crucible keep/revert** on Sortino score.

---

## 11. Data Layer (Turso + libSQL)

| Mode | Connection | Notes |
|------|------------|-------|
| Local paper | `turso dev` sqld @ `LOCAL_SQLD_URL` | Default; `scripts/start_local_sqld.py` |
| Cloud | `TURSO_DATABASE_URL` + token | Embedded replica sync via `database/replica_store.py` |

Critical tables:

| Table | Purpose |
|-------|---------|
| `execution_controls` | Singleton kill switch, apex/crucible state, PAPER/LIVE mode |
| `active_strategy` | Champion JSON + `python_source` + `best_score` |
| `trade_exhaust` | Rolling oracle snapshots for backtest + analytics |
| `agent_archetypes` | Agent cash/NAV (`APEX_EDGE`) |
| `trades` / positions | Open paper legs |
| `trader_health` | Stoppage status per tick |
| `portfolio_history` | Dashboard NAV chart |
| `oracle_health` | Oracle ingest failure streak / circuit state |
| `book_buffer` | Debounced BookWatcher snapshots for Apex merge |
| `strategy_proposals` | Quarantined Crucible LLM output before Turso KEEP |
| `strategy_history` | Champion lineage on KEEP |
| `audit_events` | Adversarial filter / policy audit log |

Migrations: `database/migrate_schema.py` (includes `trader_health`, `migrate_qa_audit_tables()`).

---

## 12. Concurrency and Locking

| Lock | Path | Holder |
|------|------|--------|
| Arena DB | `.arena_db.lock` | Crucible iterations, some writes |
| Crucible | crucible lock file | AutoResearch loop |

Dashboard uses **fresh libSQL connections per query** — no Streamlit session caching of HTTP batons.

Supervisor opens a **new DB connection each poll tick** to avoid stale Hrana sessions.

---

## 13. Observability

| Surface | Location |
|---------|----------|
| Apex log | `logs/apex.log` |
| Crucible log | `logs/crucible.log` |
| Supervisor | `logs/supervisor.log` |
| Dashboard | `logs/dashboard.log`, watchdog `logs/dashboard_watchdog.log` |
| Streamlit UI | http://127.0.0.1:8501 — Trader Status, Wallet Health, engine PIDs |
| Trader health audit | `scripts/trader_health_audit.py` (supervisor periodic) |

Stoppage kinds: `SIGNAL_STARVATION`, `EXECUTION_STARVATION`, `CAPITAL_STARVATION`.

Dominant block reasons (dashboard / `trader_health`): `fully_deployed`, `cap_blocked`, `edge_gated`, `all_hold`, `activity`.

| Script | Purpose |
|--------|---------|
| `scripts/stack_status.py` | Snapshot supervisor + engine PIDs + wallet health |
| `scripts/restart_stack.py` | Stop/start stack + optional trade-flow verify |
| `scripts/stop_stack.py` | Clean shutdown |
| `scripts/verify_trade_flow.py` | Post-restart buy+sell gate |
| `scripts/acceptance_gate.py` | Definition-of-done QA |

---

## 14. LLM / Agent Infrastructure

| Use | Model | Module |
|-----|-------|--------|
| Crucible strategy proposals | DeepSeek `deepseek-v4-flash` | `shared/deepseek.py` + `shared/adversarial_filter.py` |
| Blueprint sync (optional) | DeepSeek `deepseek-v4-flash` | `scripts/sync_master_blueprints.py` |
| Embeddings (future / risk) | Ollama Qwen3-Embedding-0.6B | env `EMBEDDING_MODEL` |

Crucible env:

| Variable | Purpose |
|----------|---------|
| `DEEPSEEK_V4_API` | API key |
| `AUTORESEARCH_DRY_RUN` | Skip LLM; backtest-only loop |
| `AUTORESEARCH_MIN_LIVE_FILL_ELIGIBLE` | Require live edge gate before KEEP |
| `LIVE_AUDIT_ENABLED` | Shadow/live auto-revert on champion drift (default false) |
| `TOXICITY_FAIL_CLOSED` | Adversarial filter blocks on high toxicity |

### QA audit remediation (June 2026)

| Pillar | Module / table | Role |
|--------|----------------|------|
| Oracle resilience | `engine_1_apex/oracle_circuit_breaker.py`, `oracle_health` | Trip on ingest failures; decouple execution stale reads |
| Book buffer | `shared/book_watcher.py`, `book_buffer` | Debounced sub-second snapshots merged in Apex |
| Proposal quarantine | `strategy_proposals`, Crucible scheduler | LLM output staged before Turso KEEP |
| Audit judge | `backtest_judge.py` Sharpe slope + friction | KEEP gate hardening |
| Live revert | `live_performance_monitor.py`, `strategy_history` | Optional shadow revert to prior champion |
| Adversarial filter | `shared/adversarial_filter.py`, `audit_events` | Scan DeepSeek/news/prompt payloads |

---

## 15. Configuration Reference

See `.env.example` for full list. Checkpoint highlights:

```
EDGE_MODEL_MOCKED=false          # live CLOB oracle
EXECUTION_MODE=paper
APEX_MAX_LADDER_LEGS=6           # portfolio-wide open-leg budget (default code: 3)
APEX_MAX_LEGS_PER_MARKET=1       # six markets × one leg
APEX_CAP_STALL_REMEDIATE_TICKS=6 # idle / cap-stall rotate (~1 min)
APEX_THESIS_REENTRY_COOLDOWN_SECONDS=900
APEX_IDLE_ROTATE_REENTRY_COOLDOWN_SECONDS=900
APEX_CAP_STALL_ENTRY_COOLDOWN_SECONDS=120
APEX_STOPPAGE_TICKS=6
APEX_TRADING_STALL_TICKS=18
LIVE_AUDIT_ENABLED=false         # shadow live-performance auto-revert
ORACLE_CB_ENABLED=true
BACKTEST_MOCK_RESOLUTIONS=true   # backtest judge (independent of live mock)
IP4_DASHBOARD_PORT=8501
SUPERVISOR_POLL_SECONDS=3
```

---

## 16. Operational Runbook

### Bootstrap (once)

```bash
python3 scripts/ip4_bootstrap.py
cp .env.example .env   # edit keys
```

### Full stack

```bash
./scripts/ip4_supervisor.sh watch --dashboard
```

### Dashboard only (no Apex/Crucible auto-spawn)

```bash
./scripts/ip4_supervisor.sh dashboard
```

### Validate blueprint on disk

```bash
.venv/bin/python scripts/sync_master_blueprints.py --validate-only
```

### Regenerate blueprint (needs DeepSeek key)

```bash
.venv/bin/python scripts/sync_master_blueprints.py
```

### Stack lifecycle (agent-operated)

```bash
.venv/bin/python scripts/stack_status.py --require-healthy
.venv/bin/python scripts/restart_stack.py
.venv/bin/python scripts/verify_trade_flow.py
.venv/bin/python scripts/acceptance_gate.py --scope full
.venv/bin/python scripts/stop_stack.py
```

See `.cursor/skills/ip4-stack-lifecycle/SKILL.md` and `agent/wiki/project_wiki.md` Operator Runbook.

### Tests

```bash
.venv/bin/pytest tests/ -q
```

---

## 17. Test Infrastructure

| Test area | Files |
|-----------|-------|
| Stoppage classification | `tests/test_stoppage.py` |
| Snapshot CLOB contract | `tests/test_snapshot_contract.py` |
| Dashboard smoke | `tests/test_dashboard_app.py` |
| Trade close / trim | `tests/test_trade_close.py` |
| Blueprint validation | `tests/test_sync_master_blueprints.py` |
| Backtest mock resolutions | `tests/test_backtest_mock.py` |
| Resolved corpus bootstrap | `tests/test_resolved_corpus_bootstrap.py` |
| Acceptance gate | `tests/test_acceptance_gate.py` |
| Apex sizing | `tests/test_apex_sizing.py` |
| AutoResearch | `tests/test_autoresearch.py` |
| Dashboard blockers | `tests/test_dashboard_blockers.py` |
| Dashboard portfolio | `tests/test_dashboard_portfolio.py` |
| Dashboard processes | `tests/test_dashboard_processes.py` |
| Hold hysteresis | `tests/test_hold_hysteresis.py` |
| Live replay gate | `tests/test_live_replay_gate.py` |
| Portfolio store | `tests/test_portfolio_store.py` |
| Preflight | `tests/test_preflight.py` |
| Supervisor lock | `tests/test_supervisor_lock.py` |
| Supervisor orphans | `tests/test_supervisor_orphans.py` |
| Trader health store | `tests/test_trader_health_store.py` |
| Verify stack | `tests/test_verify_stack.py` |
| Arena transaction | `tests/test_arena_transaction.py` |
| Backtest judge slope | `tests/test_backtest_judge_slope.py` |
| Crucible scheduler | `tests/test_crucible_scheduler.py` |
| Fresh snapshot | `tests/test_get_fresh_snapshot.py` |
| Kelly sizing | `tests/test_kelly_sizing.py` |
| Live trading feedback | `tests/test_live_trading_feedback.py` |
| Poly costs | `tests/test_poly_costs.py` |
| Runtime state store | `tests/test_runtime_state_store.py` |
| Stack lifecycle | `tests/test_stack_lifecycle.py` |
| Strategy atomic | `tests/test_strategy_atomic.py` |
| Strategy sandbox | `tests/test_strategy_sandbox.py` |
| Toxicity gate | `tests/test_toxicity_gate.py` |
| Trade flow verify | `tests/test_trade_flow_verify.py` |
| Oracle circuit breaker | `tests/test_oracle_circuit_breaker.py` |
| Adversarial filter | `tests/test_adversarial_filter.py` |
| Live performance monitor | `tests/test_live_performance_monitor.py` |
| Thesis re-entry cooldown | `tests/test_thesis_reentry_cooldown.py` |
| Strategy history revert | `tests/test_strategy_history_revert.py` |
| Trading activity store | `tests/test_trading_activity_store.py` |

CI (`.github/workflows/test.yml`): pytest on push to `main`; `safety-gates` job runs offline acceptance gate; `security` job runs bandit; preflight blocking (no `|| true`).

---

## 18. What IP4 Does Not Have Yet

- IP3-style 24-agent quadrant competition in the hot path
- Prime Apex meta-lanes
- Fully validated LIVE wallet crucible pass
- Automatic blueprint LLM sync on every push (manual/workflow_dispatch only)
- Resolved markets in `markets_ledger` during paper (backtest uses synthetic resolutions)

---

## 19. Repository Map

```
InvestmentProphits4/
├── InvestmentProphits4_MASTER_BLUEPRINTS.md   ← this document
├── agent/wiki/project_wiki.md                 ← session log + decisions
├── engine_1_apex/                             ← Apex Edge execution
│   ├── ip4_apex_edge.py                       ← main loop
│   ├── stoppage.py                            ← stoppage tracker + remediation
│   ├── fair_value.py                          ← fair value computation
│   ├── kelly_sizing.py                        ← fractional Kelly
│   ├── sizing.py                              ← edge gates, ladder caps
│   ├── trade_close.py                         ← position close + trim
│   ├── risk_daemon.py                         ← bracket exits
│   ├── oracle_sync.py                         ← CLOB + Gamma sync
│   ├── oracle_circuit_breaker.py              ← ingest failure circuit breaker
│   ├── live_performance_monitor.py            ← shadow live champion audit
│   ├── cap_churn_guard.py                     ← cap-rebalance churn detection
│   ├── toxicity_gate.py                       ← toxicity rejection gate
│   └── execution/                             ← gateway, nonce, RPC
├── engine_2_crucible/                           ← AutoResearch + backtest judge
│   ├── ip4_swarm_crucible.py                   ← main loop
│   ├── active_strategy.py                      ← LLM-editable strategy
│   ├── val_bpb_backtest.py                     ← Sortino judge (never LLM)
│   ├── strategy_loader.py                      ← AST sandbox + load
│   ├── strategy_instructions.md                ← human mandate
│   ├── strategy_atomic.py                      ← atomic strategy write
│   ├── backtest_corpus.py                      ← exhaust flatten
│   ├── backtest_judge.py                       ← Sortino + MAE scorer
│   ├── live_trading_feedback.py                ← Apex PnL/churn feedback
│   └── live_replay_gate.py                     ← live signal gate
├── engine_3_dashboard/                          ← Streamlit Command Center
│   ├── app.py                                  ← main UI
│   ├── db.py                                   ← DB helpers
│   ├── processes.py                            ← engine process helpers
│   └── hrana.py                                ← transient error detection
├── database/                                    ← schema, stores, seed, migrate
│   ├── migrate_schema.py                       ← schema migrations
│   ├── seed_arena.py                           ← market + agent seed
│   ├── execution_controls_store.py             ← control plane
│   ├── strategy_store.py                       ← champion strategy
│   ├── portfolio_store.py                      ← NAV + injection ledger
│   ├── trader_health_store.py                  ← stoppage persistence
│   ├── market_state_store.py                   ← oracle snapshots
│   ├── replica_store.py                        ← embedded replica sync
│   ├── resolved_corpus_store.py                ← resolved market corpus
│   ├── resolved_corpus_bootstrap.py            ← resolved market bootstrap
│   ├── runtime_state_store.py                  ← runtime observation persistence
│   ├── trading_activity_store.py               ← trade activity breakdown
│   ├── oracle_health_store.py                  ← oracle circuit state
│   ├── book_buffer_store.py                    ← BookWatcher buffer
│   ├── strategy_proposals_store.py             ← quarantined proposals
│   ├── audit_store.py                          ← audit_events
│   ├── arena_lock.py                           ← arena DB lock
│   ├── transaction.py                          ← arena transaction wrapper
│   └── sync_config.py                          ← connection mode detection
├── scripts/
│   ├── ip4_supervisor.sh                        ← entrypoint
│   ├── supervisor_watch.py                      ← process spawner
│   ├── dashboard_service.py                     ← Streamlit watchdog
│   ├── sync_master_blueprints.py                ← blueprint LLM sync
│   ├── acceptance_gate.py                       ← full system validation
│   ├── verify_stack.py                          ← stack health check
│   ├── restart_stack.py                         ← full restart
│   ├── stop_stack.py                            ← clean shutdown
│   ├── stack_lifecycle.py                       ← lifecycle management
│   ├── stack_status.py                          ← stack snapshot
│   ├── trade_flow_verify.py                     ← trade flow verification
│   ├── verify_trade_flow.py                     ← post-restart buy+sell gate
│   ├── project_python.py                        ← project python helper
│   ├── preflight.py                             ← startup checks
│   ├── soak_verify.py                           ← soak test
│   ├── trader_health_audit.py                   ← periodic health audit
│   └── start_local_sqld.py                      ← sqld bootstrap
├── shared/                                      ← CLOB, costs, deepseek
│   ├── deepseek.py                              ← DeepSeek V4 client
│   ├── poly_costs.py                            ← transaction cost model
│   ├── capital_injection.py                     ← injection ledger
│   ├── polymarket_clob.py                       ← CLOB client
│   ├── db_lock.py                               ← shared DB lock
│   ├── book_watcher.py                          ← sub-second CLOB poller
│   └── adversarial_filter.py                    ← LLM/news toxicity scan
├── tests/                                       ← 270+ tests
└── .github/workflows/
    └── test.yml                                 ← CI: pytest + safety-gates + bandit
```

### Seed markets (`database/seed_arena.py`)

| market_id | category |
|-----------|----------|
| mkt_us_election | Politics |
| mkt_btc_100k | Crypto |
| mkt_ai_agi | Science |
| mkt_oscars | Culture |
| mkt_fed_cut | Macro |
| mkt_ukraine_peace | Geopolitics |
| mkt_super_bowl | Sports |
| mkt_scotus_tariff | Legal |
| mkt_oil_100 | Energy |
| mkt_recession | Business |

---

## 20. Strategy Summary (One Paragraph)

InvestmentProphits4 paper-trades up to ten Polymarket-style binary markets by combining a **Crucible-evolved** `evaluate_market()` strategy (OBI + cross-venue consensus + depth gates at checkpoint) with an **Apex execution stack** that computes fair value from live CLOB mids, enforces synthetic transaction costs and a **0.015 net-edge bar** (exploration 0.008 only when explicitly enabled), and simulates fractional-Kelly ladder entries subject to **portfolio-wide** `APEX_MAX_LADDER_LEGS`, per-market `APEX_MAX_LEGS_PER_MARKET`, and `APEX_MAX_PORTFOLIO_PCT` deployment caps. Oracle snapshots land in `trade_exhaust` with BookWatcher-fed `book_buffer` merge; an **oracle circuit breaker** protects ingest. Stoppage telemetry distinguishes infra health from trading activity (`IDLE`, `STALLED`, `STARVED`) using `actionable_unfilled_signals` — edge-gated and cooldown-blocked signals must not inflate false STALLED states. Idle and fully-deployed rotation redeploys idle cash when strategy is HOLD-heavy. Crucible quarantines LLM proposals in `strategy_proposals`, scores KEEP candidates with Sharpe-slope and friction-aware judges, and optionally shadows live champion drift via `live_performance_monitor`. Adversarial filtering guards DeepSeek and news inputs. The acceptance gate and CI safety-gates validate schema, engines, trade flow, and blueprint consistency before handoff.

---

*Manual engineering sync 2026-06-24 (QA audit remediation, deployment tuning, stoppage fixes).*

*This document reflects the IP4 codebase at checkpoint June 2026. For session-level engineering notes see `agent/wiki/project_wiki.md`. Prior art: `../InvestmentProphits3/InvestmentProphits3_MASTER_BLUEPRINTS.md`.*

*Initial checkpoint authored 2026-06-22. Validated by `scripts/sync_master_blueprints.py --validate-only`.*