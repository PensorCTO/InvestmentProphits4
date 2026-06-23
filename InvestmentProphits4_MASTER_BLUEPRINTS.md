# InvestmentProphits4 — Master Blueprints

**Version:** June 2026 checkpoint (dual-engine Turso/libSQL arena, Karpathy AutoResearch Crucible, Apex Edge execution, Streamlit Command Center, trader health stoppage detection, live Polymarket CLOB when `EDGE_MODEL_MOCKED=false`)  
**Scope:** Paper-first Polymarket-inspired binary prediction market trading with optional LIVE execution scaffold. No real capital unless explicitly switched to LIVE mode with wallet keys.

This document describes **what IP4 is**, **how it works end-to-end**, and **exactly what the trading strategy is** — from oracle snapshots through Crucible research, Apex paper fills, dashboard control plane, and observability. It reflects the codebase as implemented at this checkpoint, not aspirational backlog.

**Maintained by:** `scripts/sync_master_blueprints.py` (DeepSeek `deepseek-v4-flash` on workflow dispatch) + validation on every push via GitHub Actions.

---

## Executive Summary

InvestmentProphits4 (IP4) is a **dual-engine paper arena**:

| Engine | Process | Role |
|--------|---------|------|
| **Engine 1 — Apex Edge** | `engine_1_apex/ip4_apex_edge.py` | Oracle sync → `trade_exhaust` snapshots → read `active_strategy` from DB → fair value + net edge gates → paper (or LIVE) fills |
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
| `filled=0` for hours | Edge gates, ladder caps, `max_legs`, not infra | See stoppage kinds in Wallet Health panel |
| Swarm score back to 0 | Crucible restart recalibrated after backtest returned 0 trades (`EDGE_MODEL_MOCKED=false` broke replay) | Fixed: `BACKTEST_MOCK_RESOLUTIONS` default true; recalibrate skips zero-trade replay |
| Cash ≈ $0, `skipped_cap` | Over-laddering on 1–2 markets | `APEX_MAX_LADDER_LEGS`, cap rebalance in `trade_close.py` |

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

1. Oracle worker writes `trade_exhaust` payload (per-market CLOB mid, spread, depth, overlays).
2. Load `active_strategy.python_source` → `evaluate_market(state)` per market.
3. Fair value + net edge vs `APEX_MIN_NET_EDGE` (paper: `APEX_PAPER_MIN_NET_EDGE`).
4. `PaperGateway` simulates fills; updates cash/NAV; ladder + cap checks.
5. Stoppage detector → `trader_health` row; optional cap rebalance trim.

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
| Sizing | Fractional Kelly + `APEX_MAX_POSITION_PCT` + `APEX_MAX_LADDER_LEGS` |
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
| `APEX_PAPER_MIN_NET_EDGE` | 0.008 | Paper execution edge bar |
| `APEX_MIN_LADDER_USD` | 5.0 | Minimum notional per ladder leg |

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

Migrations: `database/migrate_schema.py` (includes `trader_health`).

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

---

## 14. LLM / Agent Infrastructure

| Use | Model | Module |
|-----|-------|--------|
| Crucible strategy proposals | DeepSeek `deepseek-v4-flash` | `shared/deepseek.py` |
| Blueprint sync (optional) | DeepSeek `deepseek-v4-flash` | `scripts/sync_master_blueprints.py` |
| Embeddings (future / risk) | Ollama Qwen3-Embedding-0.6B | env `EMBEDDING_MODEL` |

Crucible env:

| Variable | Purpose |
|----------|---------|
| `DEEPSEEK_V4_API` | API key |
| `AUTORESEARCH_DRY_RUN` | Skip LLM; backtest-only loop |
| `AUTORESEARCH_MIN_LIVE_FILL_ELIGIBLE` | Require live edge gate before KEEP |

---

## 15. Configuration Reference

See `.env.example` for full list. Checkpoint highlights:

```
EDGE_MODEL_MOCKED=false          # live CLOB oracle
EXECUTION_MODE=paper
APEX_MAX_LADDER_LEGS=3
APEX_STOPPAGE_TICKS=6
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

CI (`.github/workflows/sync-master-blueprints.yml`): validates blueprint on push to `main`.

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
├── engine_2_crucible/                           ← AutoResearch + backtest judge
├── engine_3_dashboard/                          ← Streamlit Command Center
├── database/                                    ← schema, stores, seed, migrate
├── scripts/
│   ├── ip4_supervisor.sh                        ← entrypoint
│   ├── supervisor_watch.py                      ← process spawner
│   ├── dashboard_service.py                     ← Streamlit watchdog
│   ├── sync_master_blueprints.py                ← blueprint LLM sync
│   └── trader_health_audit.py
├── shared/                                      ← CLOB, costs, deepseek
└── tests/
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

InvestmentProphits4 paper-trades up to ten Polymarket-style binary markets by combining a **Crucible-evolved** `evaluate_market()` strategy with an **Apex execution stack** that computes fair value from live CLOB mids plus order-book imbalance, enforces synthetic transaction costs and configurable net-edge thresholds, and simulates fractional-Kelly ladder entries subject to per-market exposure caps and a maximum open-leg count. Oracle snapshots land in `trade_exhaust` with full depth fields so the same strategy logic runs in backtest replay and live ticks; the backtest judge scores Sortino on replay rows using synthetic resolutions for still-open markets, while Apex uses real books when `EDGE_MODEL_MOCKED=false`. A DB-driven supervisor spawns Apex and Crucible from `execution_controls`, the Streamlit Command Center exposes kill switch and mode transitions without replacing the supervisor, and inline stoppage detection records wallet health when signals exist but fills stall — distinguishing edge-gate rejection, HOLD-heavy signal starvation, and capital lock-up from process failure.

---

*This document reflects the IP4 codebase at checkpoint June 2026. For session-level engineering notes see `agent/wiki/project_wiki.md`. Prior art: `../InvestmentProphits3/InvestmentProphits3_MASTER_BLUEPRINTS.md`.*

*Initial checkpoint authored 2026-06-22. Validated by `scripts/sync_master_blueprints.py --validate-only`.*

---
