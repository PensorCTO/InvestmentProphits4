# IP4 Project Wiki

Engineering memory for InvestmentProphits4. The authoritative architecture doc is
`InvestmentProphits4_MASTER_BLUEPRINTS.md` (auto-validated on push; LLM-sync via workflow dispatch).

## Session Log

### 2026-06-22 — Checkpoint: dual-engine + blueprint + git

- **Apex + Crucible** run under `scripts/supervisor_watch.py` (DB-driven lifecycle). Dashboard buttons only update `execution_controls`; they do not spawn processes without supervisor.
- **Dashboard** uses `scripts/dashboard_service.py` watchdog (HTTP health on 8501). Fresh libSQL per query; `@st.fragment` auto-refresh.
- **Stoppage detector** (`engine_1_apex/stoppage.py`) + `trader_health` table + dashboard Wallet Health panel.
- **Backtest** decoupled from `EDGE_MODEL_MOCKED` — `BACKTEST_MOCK_RESOLUTIONS` defaults true in `val_bpb_backtest.py`.
- **Master blueprints** process established (mirrors IP3): `scripts/sync_master_blueprints.py`, GitHub Action validate on push.

## Active Work

- Monitor Crucible `best_score` after recalibrate guard; peak was ~0.0691 before restarts zeroed stored score.
- Consider lowering `APEX_MAX_POSITION_PCT` from 1.0 to reduce single-market ladder lock-up.

## Decisions Log

- **2026-06-22:** Backtest synthetic resolutions independent of live CLOB mock flag — open markets have no ledger resolutions during paper research.
- **2026-06-22:** Recalibrate must not lower `best_score` when champion replay returns zero trades.

## Lessons Learned

- Dashboard Start/Stop writes DB intent only; supervisor is the process spawner.
- Never cache libSQL HTTP sessions in Streamlit (`invalid baton` / `STREAM_EXPIRED`).
- Crucible restart + broken backtest replay can wipe `best_score` if recalibrate is too aggressive.
