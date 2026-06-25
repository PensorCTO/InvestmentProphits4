# IP4 Algorithm Blueprints

**Purpose:** Reference for every tunable lever that shapes Apex Edge trading behavior — what each knob does, how levers interact, and safe tuning order.

**Companion docs:**
- [`InvestmentProphits4_MASTER_BLUEPRINTS.md`](InvestmentProphits4_MASTER_BLUEPRINTS.md) — system architecture, ops, data layer
- [`.env.example`](.env.example) — canonical env defaults
- [`agent/wiki/project_wiki.md`](agent/wiki/project_wiki.md) — engineering decisions and session notes

**Scope:** Paper-first execution on `APEX_EDGE` (`engine_1_apex/ip4_apex_edge.py`). Crucible research levers included where they affect live champion selection.

---

## 1. End-to-End Algorithm Flow

Every **10 seconds** (`APEX_EXEC_INTERVAL`), Apex runs one tick per oracle snapshot:

```
Oracle snapshot (trade_exhaust + book_buffer)
        │
        ▼
For each active market ─────────────────────────────────────────────┐
        │                                                            │
        ▼                                                            │
Regime circuit breaker (V2_REGIME_CIRCUIT_BREAKER)                  │
        │ skip if POOR_LIQUIDITY                                     │
        ▼                                                            │
Liquidity tier floor (APEX_LIQUIDITY_FLOOR)                         │
        │                                                            │
        ▼                                                            │
Champion strategy: evaluate_market(state) → BUY_YES | BUY_NO | HOLD │
        │                                                            │
        ├─ HOLD ──► thesis-expired close path (optional)              │
        │                                                            │
        └─ BUY ──► Entry gate chain ──► Fair value + edge ──►       │
                   Kelly sizing ──► PaperGateway fill                │
        │                                                            │
        ▼                                                            │
Post-tick: cap-stall / idle / fully-deployed remediation            │
           cap-churn guard observe                                   │
           stoppage tracker → trader_health                          │
        └────────────────────────────────────────────────────────────┘
```

Parallel loops (not on every tick):
- **Oracle worker** — `ORACLE_SYNC_INTERVAL` (default 30s): CLOB ingest, circuit breaker
- **Risk daemon** — `ARENA_RISK_INTERVAL` (default 45s): bracket exits, optional toxicity committee
- **BookWatcher** — DMA adaptive poll (`DMA_POLL_SLOW_MS` 250ms → `DMA_POLL_FAST_MS` 50ms on vol spike): debounced writes to `book_buffer`
- **HMM regime** — per-tick decode → `runtime_levers.py` overrides edge/Kelly/portfolio caps
- **Shadow soak** — Crucible stages `backtest_pass`; Apex promotes after 1h edge comparison

---

## 2. Lever Taxonomy

Levers are grouped by **layer**. Within a layer, order matters: upstream levers constrain downstream ones.

| Layer | Primary module(s) | What it controls |
|-------|-------------------|------------------|
| A. Strategy signal | `active_strategy.py` | *Whether* to trade (direction) |
| B. Oracle & state | `oracle_sync.py`, `strategy_loader.py` | *Quality* of inputs to strategy |
| C. Regime & liquidity | `regime_classifier.py` | Hard skip toxic books |
| D. Entry cooldowns | `ip4_apex_edge.py`, `stoppage.py`, `sizing.py` | *When* re-entry is allowed |
| E. Edge & fair value | `fair_value.py`, `execution_edge.py`, `gateway.py` | *Whether* edge justifies fill |
| F. Sizing & caps | `sizing.py`, `kelly_sizing.py`, `herding_cap.py` | *How much* to deploy |
| G. Exit paths | `trade_close.py`, `risk_daemon.py` | *Why* positions close |
| H. Remediation & rotation | `stoppage.py`, `trade_close.py` | Capital recycling when blocked |
| I. Churn guards | `cap_churn_guard.py` | Pause spread-tax loops |
| J. Health telemetry | `stoppage.py` | `trader_health` status (not fill logic) |
| K. Crucible research | `ip4_swarm_crucible.py` | Champion code selection |
| L. DMA microstructure | `book_watcher.py`, `mtf_filter.py`, `mid_vol_tracker.py` | Poll cadence, OBI debounce, phantom liquidity |
| M. HMM runtime levers | `market_regime_hmm.py`, `runtime_levers.py` | Regime-aware edge/Kelly/portfolio overrides |
| N. Shadow promotion | `shadow_strategy_monitor.py`, `strategy_store.py` | Soak-before-champion |

---

## 3. Layer A — Strategy Signal (Champion Code)

**Source:** `engine_2_crucible/active_strategy.py` (Turso `active_strategy.python_source`)

The champion is **Python code**, not env vars. Crucible evolves it via DeepSeek proposals + backtest KEEP/REVERT.

### Current champion pattern (checkpoint)

| Rule | Effect |
|------|--------|
| Mid ∈ (0.05, 0.95) | Skip extreme prices |
| `spread ≤ 0.015` | Skip wide spreads |
| `\|cross_venue_adj\| ≥ 0.006` | Require cross-venue consensus |
| `bid_depth + ask_depth ≥ 40` | Minimum book depth |
| OBI + cross agree | `BUY_YES` or `BUY_NO`; else `HOLD` |

**Interaction:** Strategy HOLD → no entry signals → `dominant_block_reason=all_hold`. Strategy BUY on N markets + caps → `fully_deployed` or `cap_blocked`.

**Crucible-only levers** (change *who* gets promoted, not runtime env):

| Variable | Default | Role |
|----------|---------|------|
| `AUTORESEARCH_MIN_LIVE_SIGNALS` | 1 | Min non-HOLD markets before KEEP |
| `AUTORESEARCH_MIN_LIVE_FILL_ELIGIBLE` | 1 | Min markets passing Apex edge gate before KEEP |
| `AUTORESEARCH_MIN_REPLAY_FILL_ELIGIBLE` | 5 | Min replay fills in backtest gate |
| `AUTORESEARCH_MAX_REPLAY_EDGE_REJECT_RATE` | 0.5 | Max edge rejection rate in replay |
| `BACKTEST_MOCK_RESOLUTIONS` | false | Synthetic resolutions for research score only |
| `CRUCIBLE_EXPLORATION` | false | Lower edge bar for Crucible eligibility checks |
| `JUDGE_MIN_IS_SORTINO` | 0.0 | In-sample Sortino floor for KEEP |
| `JUDGE_MIN_OOS_SORTINO` | 0.0 | Out-of-sample Sortino floor for KEEP |
| `JUDGE_MAX_OOS_MDD` | 0.10 | Hard REVERT if OOS drawdown exceeds 10% |
| `WALK_FORWARD_HIDDEN_FRACTION` | 0 | Optional hidden holdout (default off; 80/20 IS/OOS) |
| `SHADOW_PROMOTE_WINDOW_S` | 3600 | Shadow soak before champion promotion |
| `SHADOW_PROMOTE_MIN_EDGE_DELTA` | 0.002 | Min edge advantage for shadow promotion |

---

## 4. Layer B — Oracle & Market State

| Variable | Default | Role | Interacts with |
|----------|---------|------|----------------|
| `EDGE_MODEL_MOCKED` | false | `true` = synthetic OBI; `false` = live CLOB | Strategy signals, edge calc, Crucible KEEP gates |
| `ORACLE_SYNC_INTERVAL` | 30 | Oracle ingest cadence (seconds) | Snapshot freshness vs load |
| `ORACLE_STALE_SECONDS` | 60 | Stale snapshot threshold | Apex may skip tick |
| `ORACLE_CB_ENABLED` | true | Circuit breaker on ingest failures | Halts oracle; Apex starves |
| `ORACLE_CB_FAILURE_THRESHOLD` | 3 | Failures before trip | — |
| `ORACLE_CB_RECOVERY_SECONDS` | 120 | Cooldown before retry | — |
| `CROSS_VENUE_ENABLED` | false | Live cross-venue overlay feed | Strategy `cross_venue_adj` |
| `CROSS_VENUE_OBI_PROXY` | true | OBI-derived proxy when Kalshi missing | Strategy consensus gate |
| `MTF_POLL_MS` | 250 | BookWatcher base poll (overridden by DMA) |
| `DMA_ENABLED` | true | Vol-adaptive poll 250→50ms on spike |
| `DMA_VOL_WINDOW_S` | 10 | Short vol window for spike detection |
| `DMA_VOL_LOOKBACK_S` | 3600 | Baseline lookback for P90 threshold |
| `DMA_POLL_FAST_MS` | 50 | Fast poll during vol spike |
| `DMA_POLL_SLOW_MS` | 250 | Normal poll cadence |
| `DMA_MIN_NOTIONAL_USD` | 50 | Min notional for OBI level weight |
| `BOOK_WATCHER_MAX_CONCURRENT` | 8 | Parallel book fetches | Latency vs load |

**State enrichment:** `build_market_state()` merges snapshot + `book_buffer` + overlays. Strategy and edge layers both consume the same dict.

---

## 5. Layer C — Regime & Liquidity

Hard **skip** before strategy evaluation completes entry path. Uses **z-score Schmitt hysteresis** (June 2026).

| Variable | Default | Role |
|----------|---------|------|
| `V2_REGIME_CIRCUIT_BREAKER` | true | Enable POOR_LIQUIDITY hold |
| `REGIME_Z_SPREAD_TRIP` | 2.5 | Spread z-score trip threshold |
| `REGIME_Z_DEPTH_TRIP` | −2.0 | Depth z-score trip threshold |
| `REGIME_SCORE_TRIP` | 80 | Regime score 0–100 to enter POOR |
| `REGIME_SCORE_RECOVER` | 40 | Score must stay below for recovery |
| `REGIME_RECOVER_TICKS` | 3 | Consecutive ticks below recover score |
| `REGIME_Z_WINDOW_S` | 300 | Rolling window for z-scores (5 min) |
| `REGIME_EPHEMERAL_THRESHOLD` | 0.7 | Flickering book reason (auxiliary) |
| `REGIME_SPREAD_MULT` | 2.0 | **Deprecated** — superseded by z-scores |
| `APEX_LIQUIDITY_FLOOR` | 50000 | Min tier volume; markets below skipped |

**Schmitt trigger:** Enter POOR when score > 80; exit only after score < 40 for 3 consecutive Apex ticks (~30s).

**Effect:** Increments `skipped_hold` (regime) — distinct from strategy HOLD.

---

## 6. Layer D — Entry Cooldowns

Applied **before** `signals += 1` (critical for stoppage accuracy).

| Variable | Default | Trigger | Blocks |
|----------|---------|---------|--------|
| `APEX_REMEDIATE_COOLDOWN_SECONDS` | 120 | After cap-stall / portfolio remediate close | Re-entry on that market |
| `APEX_IDLE_ROTATE_REENTRY_COOLDOWN_SECONDS` | 900 | After idle or fully-deployed rotate | Re-entry on rotated market |
| `APEX_THESIS_REENTRY_COOLDOWN_SECONDS` | 900 | After `THESIS_EXPIRED` close | Re-entry (prevents buy→HOLD→close loop) |
| `APEX_STOP_LOSS_COOLDOWN_SECONDS` | 900 | After `CLOSED_STOP_LOSS` | Re-entry (escalates 2× per repeat) |
| `APEX_STOP_LOSS_ESCALATION_MAX` | 4 | Max escalation exponent | Cap on cooldown length |
| `APEX_STOP_LOSS_LOOKBACK_HOURS` | 24 | Window for repeat stop-out count | Escalation input |
| `APEX_STOP_LOSS_REENTRY_EDGE_MARGIN` | 0.01 | Extra edge required after stop-loss | Gateway re-entry |
| `APEX_ROTATE_REENTRY_MID_DELTA` | 0.02 | Min mid move to allow refill post-rotate | Same-direction refill after `FULLY_DEPLOYED_ROTATE` |
| `APEX_ROTATE_REENTRY_FV_DELTA` | 0.02 | Min fair-value move | Same as above |
| `APEX_ROTATE_REENTRY_BLOCK_SECONDS` | 3600 | Max duration of rotate reentry block | — |

**Interaction chain:**
1. Idle-rotate cooldown expires → market *eligible* again
2. Rotate reentry block still applies if mid/fv unchanged → `skipped_cooldown`
3. Stop-loss cooldown is independent and checked before signal count

---

## 7. Layer E — Edge & Fair Value

Determines **fill eligibility** after strategy emits BUY.

### Fair value stack

| Variable | Default | Role |
|----------|---------|------|
| `OBI_FAIR_WEIGHT` | 0.08 | OBI bump to mid for fair value (live) |
| `OBI_FAIR_FLOOR` | 0.012 | Min absolute bump on longshots |
| `OBI_FAIR_FLOOR_RATIO` | 0.35 | Ratio cap for longshot bump |
| `PAPER_OBI_FAIR_WEIGHT` | 0.30 | Stronger bump in paper mode |
| `V2_SIGNAL_FAIR_SCALE` | 0.20 | V2 composite → fair adjustment |
| `V2_COMPOSITE_EDGE_BOOST` | 1.0 | Multiplier on composite score |

### Edge gate

| Variable | Default | Role |
|----------|---------|------|
| `APEX_MIN_NET_EDGE` | **0.015** | Primary net edge bar (1.5¢) |
| `APEX_EDGE_MODE` | *(empty)* | Set `exploration` → uses 0.008 bar |
| `APEX_EXPLORATION_MIN_NET_EDGE` | 0.008 | Research / exploration bar |
| `APEX_LONGSHOT_MID_THRESHOLD` | 0.05 | Mids below this use longshot bar |
| `APEX_LONGSHOT_MIN_NET_EDGE` | 0.020 | Higher bar on sub-5% mids |
| `V2_MIN_NET_EDGE_COST_MULT` | 1.5 (paper) / 2.0 (live) | Cost multiplier in composite gate |

### V2 composite weights (microstructure gate)

| Variable | Default | Feature |
|----------|---------|---------|
| `V2_EDGE_WEIGHT_MICROPRICE` | 0.25 | Microprice deviation |
| `V2_EDGE_WEIGHT_FLOW` | 0.25 | 5s flow imbalance |
| `V2_EDGE_WEIGHT_OBI` | 0.20 | Order book imbalance |
| `V2_EDGE_WEIGHT_LIQUIDITY` | 0.15 | Liquidity quality |
| `V2_EDGE_WEIGHT_RELIABILITY` | 0.10 | Historical reliability |

**Interaction:** Strategy BUY + edge fail → `skipped_edge` → `dominant_block_reason=edge_gated`. Does **not** increment `zero_fill_streak` (not actionable).

**Typical live pattern:** `mkt_fed_cut` BUY_NO with net edge ≈ −0.47 → rejected every tick while other markets HOLD.

---

## 8. Layer F — Sizing & Deployment Caps

Controls **how much capital** each fill uses and **how many** positions may be open.

### Kelly & ladder

| Variable | Default | Role |
|----------|---------|------|
| `APEX_FRACTIONAL_KELLY` | 0.05 | Base fraction of cash per leg (5%) |
| `APEX_MAX_FRACTIONAL_KELLY` | 0.05 | Hard cap on dynamic Kelly |
| `KELLY_MIN_EDGE_SLOPE` | −0.002 | Short vs long flow slope threshold |
| `KELLY_MIN_SCALE` | 0.25 | Min Kelly scale when slope deteriorates |
| `APEX_MIN_LADDER_USD` | 5.0 | Minimum notional per leg; below → skip |

Dynamic Kelly (`kelly_sizing.py`) overrides base when edge slope is favorable; bounded by `APEX_MAX_FRACTIONAL_KELLY`.

### Position & portfolio caps

| Variable | Default | Role |
|----------|---------|------|
| `APEX_MAX_POSITION_PCT` | 0.05 | Max NAV fraction **per market** |
| `APEX_MAX_PORTFOLIO_PCT` | 0.50 | Max NAV fraction **across all open legs** |
| `APEX_MAX_LADDER_LEGS` | 6 (`.env`) / 3 (code default) | Max concurrent open legs portfolio-wide |
| `APEX_MAX_LEGS_PER_MARKET` | 1 | Max legs per market (default `floor(ladder/2)`) |
| `MAX_SWARM_MARKET_EXPOSURE` | 2500 | Institutional herding floor ($) |
| `SWARM_HERDING_NAV_PCT` | *(uses position pct)* | NAV fraction for herding cap |

**Budget computation:** `compute_ladder_budget()` returns `(kelly_size, skip_reason)`.

| skip_reason | Meaning |
|-------------|---------|
| `min_ladder` | Kelly below $5 floor |
| `position_cap` | Market at max exposure |
| `portfolio_cap` | Total deployment at max |
| `max_legs` | Portfolio leg count at cap |
| `max_legs_per_market` | Market leg count at cap |
| `herding_headroom_insufficient` | Herding cap blocks size |

**Interaction (6×1 deployment):**
- 3 legs open + 3 markets signaling BUY same direction → `skipped_already_positioned` → `fully_deployed`
- 6 legs open + new BUY signal elsewhere → `cap_blocked` → cap-stall remediate path

---

## 9. Layer G — Exit Paths

Positions close for **alpha reasons** (signal/risk) or **capital management** (remediation).

| Exit reason | Driver | Typical trigger |
|-------------|--------|-----------------|
| `SIGNAL_FLIP` | Strategy direction change | BUY_NO while holding YES |
| `THESIS_EXPIRED` | Strategy HOLD persistence | `APEX_CLOSE_ON_HOLD` + hold streak |
| `STOP_LOSS` | Risk daemon / bracket | Price move vs entry |
| `TAKE_PROFIT` | Risk daemon | Target hit |
| `CAP_STALL_REMEDIATE` | Cap blocked with signaling market | Persistent `cap_blocked` |
| `IDLE_DEPLOYMENT_ROTATE` | HOLD leg while cash idle | Fully deployed, HOLD thesis |
| `FULLY_DEPLOYED_ROTATE` | Smallest leg while cash idle | Fully deployed, no actionable fills |
| `PORTFOLIO_CAP_ROTATE` | Portfolio cap blocks new entry | Trim smallest leg |
| `CAP_REBALANCE` / trim | Per-market position cap headroom | In-tick trim before fill |
| `CAP_TRIM` | Partial 50% trim on worst ΔEdge HOLD legs | High-conviction entry waiting |
| `MAX_LEGS_REBALANCE` | Legacy rebalance | — |
| `WALLET_RESET` | Operator / bankruptcy injection | — |

### Thesis-expired levers

| Variable | Default | Role |
|----------|---------|------|
| `APEX_CLOSE_ON_HOLD` | true | Enable HOLD → close path |
| `APEX_HOLD_CLOSE_TICKS` | 3 | Consecutive HOLD ticks before close |
| `APEX_MIN_HOLD_SECONDS` | 60 | Min hold time before thesis close |

---

## 10. Layer H — Remediation & Rotation

When cash is idle but caps block new entries, Apex **recycles capital** by closing legs.

### Timing

| Variable | Default | Role |
|----------|---------|------|
| `APEX_CAP_STALL_REMEDIATE_TICKS` | **18** | Consecutive cap-block ticks before remediate (~3 min @ 10s) |
| `APEX_CAP_STALL_ENTRY_COOLDOWN_SECONDS` | 60 (paper) / 600 (live) | Cooldown after cap-stall close |
| `APEX_CAP_TRIM_ENABLED` | true | Partial trim before full-leg rotate |
| `APEX_CAP_TRIM_FRACTION` | 0.50 | Fraction trimmed per leg |
| `APEX_CAP_TRIM_MIN_LEGS` | 2 | Worst HOLD legs to trim |
| `APEX_CAP_TRIM_MIN_EDGE_MULT` | 2.0 | Pending signal edge must be ≥ mult × min_edge |

**Rotation sort:** Remediation closes legs by **lowest alpha decay** (ΔEdge = current net edge − entry net edge), not smallest notional.

### Fully-deployed / idle rotation gates (June 2026 anti-churn)

| Variable | Default | Role |
|----------|---------|------|
| `APEX_FULLY_DEPLOYED_ROTATE_MIN_FRACTION` | 0.67 | Min fraction of ladder filled before rotate |
| `APEX_FULLY_DEPLOYED_ROTATE_MIN_OPEN_LEGS` | *(derived)* | Explicit override (e.g. 4 when ladder=6) |

**Derived example:** `APEX_MAX_LADDER_LEGS=6` → rotate requires **≥4 open legs**.

### Remediation decision tree

```
cap_blocked_streak >= APEX_CAP_STALL_REMEDIATE_TICKS ?
        │
        ├─ should_cap_trim (cap_blocked + high-conviction pending entry)
        │     └─ trim 50% from 2 worst ΔEdge HOLD legs → CAP_TRIM
        │
        ├─ should_remediate_cap_stall (genuine cap_blocked, new entries waiting)
        │     └─ close worst ΔEdge leg on SIGNALING market → CAP_STALL_REMEDIATE
        │
        ├─ should_remediate_portfolio_cap (portfolio_cap + actionable signals)
        │     └─ close globally worst ΔEdge leg → PORTFOLIO_CAP_ROTATE
        │
        ├─ should_remediate_idle_deployment (fully_deployed + HOLD legs)
        │     └─ close worst ΔEdge leg on HOLD market → IDLE_DEPLOYMENT_ROTATE
        │
        └─ should_remediate_fully_deployed (fully_deployed + open_legs >= min)
              └─ close globally worst ΔEdge leg → FULLY_DEPLOYED_ROTATE
                    └─ record RotateReentrySnapshot (blocks same-thesis refill)
```

**`fully_deployed` vs `cap_blocked`:**

| State | Condition |
|-------|-----------|
| `fully_deployed` | Already positioned on signaling markets; no actionable unfilled signals; cash idle |
| `cap_blocked` | Actionable BUY signals exist but caps prevent fill |

**Critical interaction:** With 3/6 legs and `min_open_legs=4`, rotate **does not fire** even when `fully_deployed` — cash stays idle until a 4th leg fills or strategy signals change.

---

## 11. Layer I — Churn Guards

`CapChurnGuard` pauses **rebalance and new entries** when spread-tax loops are detected.

| Variable | Default | Detects |
|----------|---------|---------|
| `APEX_CAP_CHURN_GUARD` | true | Master enable |
| `APEX_CAP_CHURN_WINDOW_TICKS` | 6 | Rolling tick window |
| `APEX_CAP_CHURN_MIN_SAME_TICK_EVENTS` | 2 | Trim+fill same tick |
| `APEX_CAP_CHURN_COOLDOWN_SECONDS` | 600 | Pause duration when tripped |
| `APEX_CAP_CHURN_NAV_DRAWDOWN_PCT` | 0.12 | NAV drawdown + rebalance activity |
| `APEX_CAP_CHURN_ROTATE_FILL_WINDOW_SECONDS` | 180 | Single-market rotate→fill window |
| `APEX_CAP_CHURN_MIN_ROTATE_FILL_PAIRS` | 2 | Pairs before single-market trip |
| `APEX_CAP_CHURN_CROSS_MARKET_WINDOW_SECONDS` | 900 | Cross-market window |
| `APEX_CAP_CHURN_MIN_CROSS_MARKET_ROTATES` | 3 | Markets with rotate+fill in window |

**When active:** `blocks_rebalance()` and `blocks_new_entries()` return true → logs `APEX CAP CHURN GUARD`.

**Acceptance gate mirrors:**

| Variable | Default |
|----------|---------|
| `ACCEPTANCE_MIN_NAV_PCT_OF_SESSION` | 0.85 |
| `ACCEPTANCE_MAX_CHURN_RATIO` | 0.75 |
| `ACCEPTANCE_MAX_SAME_TICK_CHURN` | 3 |

---

## 12. Layer J — Stoppage & Health Telemetry

Does **not** block fills directly — classifies wallet state for dashboard and ops.

| Variable | Default | Role |
|----------|---------|------|
| `APEX_TRADING_STALL_TICKS` | 18 | Actionable signals, no fill → `trading=STALLED` |
| `APEX_STOPPAGE_TICKS` | 6 | Consecutive starvation → DEGRADED/STOPPED |

### `dominant_block_reason` (per tick)

| Value | Meaning |
|-------|---------|
| `activity` | Fill or alpha close this tick |
| `all_hold` | Every evaluated market returned HOLD |
| `fully_deployed` | Max legs aligned; no actionable unfilled signals |
| `cap_blocked` | Caps blocking actionable signals |
| `edge_gated` | All signals failed edge gate |
| `execution_rejected` | Gateway rejected (non-edge) |
| `none` | No signals |

### `trading_status`

| Value | Condition |
|-------|-----------|
| `ACTIVE` | Fill, flip close, or rebalance close |
| `IDLE` | Signals present but not filling (edge, cap, cooldown) |
| `STALLED` | Actionable unfilled signals for `APEX_TRADING_STALL_TICKS` |
| `STARVED` | All HOLD (strategy) |

**Note:** `edge_gated` alone → HEALTHY, `zero_fill_streak=0`. Stop-loss cooldown must be checked **before** signal count to avoid false STALLED.

---

## 13. Layer K — Risk Daemon (Parallel Loop)

| Variable | Default | Role |
|----------|---------|------|
| `ARENA_RISK_INTERVAL` | 45 | Risk loop cadence (seconds) |
| `COMMITTEE_ENABLED` | false | LLM toxicity committee on entries |
| `COMMITTEE_TOXICITY_VETO_THRESHOLD` | 0.75 | Veto threshold |
| `TOXICITY_FAIL_CLOSED` | false | Block when toxicity service unavailable |
| `LIVE_AUDIT_ENABLED` | true | Auto-revert champion on live drift (`.env.example`) |
| `LIVE_AUDIT_SHADOW` | true | Log-only revert when audit enabled |
| `HMM_ENABLED` | true | 3-state regime decoder per tick |
| `HMM_STATE_PERSIST_TICKS` | 2 | Ticks before HMM state switch |
| `HMM_MEAN_REVERT_MIN_NET_EDGE` | 0.012 | Lower edge bar in mean-revert regime |
| `HMM_MEAN_REVERT_OBI_WEIGHT` | 0.28 | Higher OBI weight in mean-revert |
| `HMM_TOXIC_MAX_FRACTIONAL_KELLY` | 0.025 | Kelly cap in toxic regime |
| `HMM_TOXIC_MAX_PORTFOLIO_PCT` | 0.20 | Portfolio cap in toxic regime |

---

## 14. Interaction Matrix

How lever groups combine in common scenarios:

### Scenario: Low activity mid-session (observed June 2026)

| Layer | State |
|-------|-------|
| Strategy | 5× HOLD, 3× BUY_YES (recession, oil, ukraine), 1× BUY_NO (fed_cut) |
| Edge | fed_cut rejected (−0.47 net edge) |
| Caps | 3 legs open; `max_legs_per_market=3` skip count |
| Rotate min | 3 < 4 required → **no rotate** |
| Health | `IDLE`, `fully_deployed`, `streak=0` |

**Tuning levers (in order of risk):**
1. Lower `APEX_FULLY_DEPLOYED_ROTATE_MIN_OPEN_LEGS` to 3 → more rotation (re-churn risk)
2. Lower `APEX_MIN_NET_EDGE` → more fills (quality risk)
3. `APEX_EDGE_MODE=exploration` → 0.008 bar (research only)
4. Crucible evolves strategy with more BUY signals

### Scenario: Rotate-fill spread loop (pre-June 2026 fix)

| Layer | Old behavior | Fix lever |
|-------|--------------|-----------|
| Rotate timing | 6 ticks (~1 min) | `APEX_CAP_STALL_REMEDIATE_TICKS=18` |
| Rotate at 1 leg | Always fired | `APEX_FULLY_DEPLOYED_ROTATE_MIN_FRACTION=0.67` |
| Refill same price | After 900s cooldown | `APEX_ROTATE_REENTRY_*` |
| 3-market carousel | Unguarded | `APEX_CAP_CHURN_CROSS_MARKET_*` |

### Scenario: Thesis micro-bleed

| Symptom | Levers |
|---------|--------|
| buy → HOLD → close → re-buy | `APEX_THESIS_REENTRY_COOLDOWN_SECONDS=900`, `APEX_CLOSE_ON_HOLD=true` |
| Exploration over-trading | Keep `APEX_EDGE_MODE` empty; `CRUCIBLE_EXPLORATION=false` |

### Scenario: Stop-loss churn

| Symptom | Levers |
|---------|--------|
| Stop → immediate refill | `APEX_STOP_LOSS_COOLDOWN_SECONDS`, escalation |
| Cap-stall refill during cooldown | `cap_stall_remediation_paused()` on stop-loss cooldown |

---

## 15. Safe Tuning Order

When calibrating live paper runs:

1. **Confirm signal quality** — strategy HOLD rate, edge-gated markets (`logs/apex.log` REJECTED lines)
2. **Edge bar** — `APEX_MIN_NET_EDGE` (do not lower until churn is controlled)
3. **Deployment shape** — `APEX_MAX_LADDER_LEGS` × `APEX_MAX_LEGS_PER_MARKET`
4. **Rotate thresholds** — `APEX_FULLY_DEPLOYED_ROTATE_MIN_*` then `APEX_CAP_STALL_REMEDIATE_TICKS`
5. **Cooldowns** — thesis, stop-loss, idle-rotate, rotate-reentry
6. **Churn guards** — leave enabled; tune thresholds only if false positives
7. **Crucible** — evolve strategy code last (slow loop)

**Restart required:** All Apex env changes need `scripts/restart_stack.py` (supervisor respawns Apex).

---

## 16. Quick Reference — Current Production `.env` Profile

Typical calibrated checkpoint (June 2026):

```bash
APEX_MAX_LADDER_LEGS=6
APEX_MAX_LEGS_PER_MARKET=1
APEX_CAP_STALL_REMEDIATE_TICKS=18
APEX_FULLY_DEPLOYED_ROTATE_MIN_FRACTION=0.67
APEX_ROTATE_REENTRY_MID_DELTA=0.02
APEX_ROTATE_REENTRY_FV_DELTA=0.02
APEX_ROTATE_REENTRY_BLOCK_SECONDS=3600
APEX_CAP_CHURN_CROSS_MARKET_WINDOW_SECONDS=900
APEX_CAP_CHURN_MIN_CROSS_MARKET_ROTATES=3
APEX_MIN_NET_EDGE=0.015
APEX_THESIS_REENTRY_COOLDOWN_SECONDS=900
APEX_IDLE_ROTATE_REENTRY_COOLDOWN_SECONDS=900
CRUCIBLE_EXPLORATION=false
EDGE_MODEL_MOCKED=false
```

---

## 17. Module Map

| Concern | File |
|---------|------|
| Tick orchestration | `engine_1_apex/ip4_apex_edge.py` |
| Stoppage / remediate | `engine_1_apex/stoppage.py` |
| Churn guard | `engine_1_apex/cap_churn_guard.py` |
| Sizing / cooldowns | `engine_1_apex/sizing.py` |
| Dynamic Kelly | `engine_1_apex/kelly_sizing.py` |
| Herding cap | `engine_1_apex/herding_cap.py` |
| Edge composite | `engine_1_apex/execution_edge.py` |
| Fill gateway | `engine_1_apex/gateway.py` |
| Close / trim | `engine_1_apex/trade_close.py` |
| Strategy loader | `engine_2_crucible/strategy_loader.py` |
| Champion code | `engine_2_crucible/active_strategy.py` |
| Costs | `shared/poly_costs.py` |
| Regime | `shared/regime_classifier.py` |
| DMA / vol tracker | `shared/mid_vol_tracker.py`, `shared/rolling_stats.py` |
| HMM + runtime levers | `engine_1_apex/market_regime_hmm.py`, `engine_1_apex/runtime_levers.py` |
| Shadow soak | `engine_1_apex/shadow_strategy_monitor.py` |
| WFO judge | `engine_2_crucible/backtest_judge.py`, `walk_forward_pipeline.py` |
| Algo blueprints | `IP4_ALGO_BLUEPRINTS.md` (this document) |

---

*Last updated: 2026-06-25 — DMA adaptive polling, z-score regime hysteresis, alpha-decay rotation + CAP_TRIM, Crucible shadow soak, HMM runtime levers, live audit shadow-first.*
