# InvestmentProphits4 — Dual-Engine Paper Arena

IP4 splits **fast execution** (Engine 1 — Apex) from **strategy research** (Engine 2 — Crucible Karpathy loop).

| Engine | Script | Role |
|--------|--------|------|
| **Apex** | `engine_1_apex/ip4_apex_edge.py` | Oracle → `trade_exhaust`, read `active_strategy` from Turso, paper trades |
| **Crucible** | `engine_2_crucible/ip4_swarm_crucible.py` | Propose → edit `active_strategy.py` → backtest → keep/revert → push winners to Turso |

IP4 is **fully standalone** — it never reuses InvestmentProphits3's Python environment or database paths.

---

**Command Center (DB-driven control plane + dashboard):**

```bash
# One-time: install dashboard dependency into IP4 venv
.venv/bin/python -m pip install streamlit

# DB-driven supervisor + Streamlit UI (uses .venv python — not system python/pip)
./scripts/ip4_supervisor.sh watch --dashboard
# Dashboard: http://127.0.0.1:8501
```

Legacy one-shot spawn (no DB lifecycle watch):

```bash
./scripts/ip4_supervisor.sh both --dashboard
```

---

## Quick start (local paper — local sqld + embedded replica)

**Prerequisites:** Python 3.11+ (3.9+ works), [Turso CLI](https://docs.turso.tech/cli/installation) for local sqld:

```bash
brew install tursodatabase/tap/turso
# or use ~/.turso/turso if already installed
```

```bash
cd InvestmentProphits4

# One-time setup (creates IP4 .venv, starts local sqld, migrates schema, seeds)
python3 scripts/ip4_bootstrap.py

# Start both engines (foreground, logs in logs/)
chmod +x scripts/ip4_supervisor.sh
./scripts/ip4_supervisor.sh
```

**Apex only** (no LLM research loop):

```bash
./scripts/ip4_supervisor.sh apex
```

**Crucible without DeepSeek** (backtest-only dry loop for QA):

```bash
# In .env:
#   AUTORESEARCH_DRY_RUN=true
./scripts/ip4_supervisor.sh crucible
```

---

## Environment (`.env`)

Copy from `.env.example`. Minimum for local paper:

| Variable | Required | Notes |
|----------|----------|-------|
| `LOCAL_REPLICA_PATH` | No | Embedded replica file (default `./ip4_local_replica.db`) |
| `LOCAL_SQLD_URL` | No | Local sqld primary (default `http://127.0.0.1:8080`) |
| `LOCAL_SQLD_DB_FILE` | No | sqld primary file (default `data/ip4_sqld_primary.db`) |
| `TURSO_DATABASE_URL` | No* | *When set with token, uses Turso Cloud instead of local sqld |
| `TURSO_AUTH_TOKEN` | No* | |
| `POLYGON_RPC_URLS` | No | Preflight latency probe target |
| `PREFLIGHT_MAX_RPC_LATENCY_MS` | No | Default `50` — abort startup if exceeded |
| `DEEPSEEK_V4_API` | For Crucible | Or set `AUTORESEARCH_DRY_RUN=true` |
| `EDGE_MODEL_MOCKED` | No | Default `true` — synthetic oracle overlays |

Production / multi-node: set Turso credentials so Apex and Crucible share state via cloud primary + embedded replicas.

Local paper mode runs `turso dev` sqld as the libSQL primary. Apex and Crucible connect via the **libSQL client** (never stdlib `sqlite3`). With Turso Cloud credentials, connections use embedded replica sync; locally, connections go direct to sqld (turso dev does not support embedded-replica pull — sqld still provides MVCC and write-to-primary semantics).

---

## Manual steps (if you prefer)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python scripts/start_local_sqld.py
python database/migrate_schema.py
python database/seed_arena.py
python scripts/preflight.py

python engine_1_apex/ip4_apex_edge.py &
python engine_2_crucible/ip4_swarm_crucible.py &
```

---

## Karpathy AutoResearch files (Engine 2)

| File | Who edits |
|------|-----------|
| `engine_2_crucible/strategy_instructions.md` | **Human only** — system mandate |
| `engine_2_crucible/active_strategy.py` | **Crucible LLM** — `evaluate_market()` logic |
| `engine_2_crucible/val_bpb_backtest.py` | **Never LLM** — Sortino judge, prints `SCORE:x.xxxx` |

After Crucible beats `best_score`, validated Python source is written to Turso `active_strategy.python_source`. Apex reloads on the next tick (no restart).

**Architecture reference:** [`InvestmentProphits4_MASTER_BLUEPRINTS.md`](InvestmentProphits4_MASTER_BLUEPRINTS.md) — validated on push; LLM refresh via `scripts/sync_master_blueprints.py` (workflow dispatch).

---

## Tests

```bash
source .venv/bin/activate
pytest tests/ -v
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `turso CLI not found` | `brew install tursodatabase/tap/turso` |
| `libsql install failed` | `python3 scripts/ip4_bootstrap.py --force-venv` |
| `no such table: trade_exhaust` | `python scripts/ip4_bootstrap.py` |
| `APEX_EDGE not active` | `python database/seed_arena.py` |
| Preflight RPC latency FAIL | In **paper mode**, RPC checks are skipped automatically. For LIVE, check network/API keys; or `PREFLIGHT_SKIP_LATENCY=true` for offline dev only |
| Crucible exits immediately on DeepSeek | Set `DEEPSEEK_V4_API` or `AUTORESEARCH_DRY_RUN=true` |
| `ORACLE STALE` in Apex logs | Wait ~30s for first oracle cycle, or check markets seeded |
| Backtest always `SCORE:0.0000` | Normal until Apex writes `trade_exhaust` rows; need resolved markets for non-zero scores |

Stop engines: `Ctrl+C` in supervisor, or `pkill -f ip4_apex_edge.py`.
