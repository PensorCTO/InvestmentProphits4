# IP4 Arena Wiki

Live trading narrative — Crucible research loop, Apex fills, stoppage events, market intelligence.

**Engineering progress** lives in [`project_wiki.md`](project_wiki.md). Do not mix the two.

## Latest Arena Snapshot

*Last updated: 2026-06-22 (manual seed — wire auto-update when arena tick writer lands)*

| Field | Value |
|-------|-------|
| Apex agent | `APEX_EDGE` |
| Execution mode | PAPER |
| DB apex_state | RUNNING |
| Wallet health | Often DEGRADED/STOPPED when EXECUTION_STARVATION (signals rejected for negative net edge) |
| Champion strategy | Turso `active_strategy.python_source` (may lag local `active_strategy.py`) |
| Cross-venue | `CROSS_VENUE_ENABLED=false` in `.env` → baseline strategy mostly HOLD |

### Recent behavior (observed)

- Apex ticks every ~10s; oracle sync every ~30s.
- Typical stoppage: **EXECUTION_STARVATION** — 2 signals/tick, 0 fills, negative net edge vs 0.015 gate.
- Crucible judge: **resolved-only** Sortino (`is_resolved=1`); synthetic corpus logs `RESEARCH_SCORE` only when `BACKTEST_MOCK_RESOLUTIONS=true`.

### Narrative log

*(Append per-session trading notes here — fills, KEEP/REVERT, notable market moves.)*
