#!/usr/bin/env bash
# Start IP4 dual-engine: Apex (execution) + Crucible (AutoResearch).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "ERROR: IP4 .venv not found." >&2
  echo "Run once:  python3 scripts/ip4_bootstrap.py" >&2
  exit 1
fi

if ! "$ROOT/.venv/bin/python" -c "import libsql" 2>/dev/null; then
  echo "ERROR: IP4 .venv missing libsql." >&2
  echo "Run once:  python3 scripts/ip4_bootstrap.py --force-venv" >&2
  exit 1
fi

PYTHON="$ROOT/.venv/bin/python"

mkdir -p "$ROOT/logs"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "ERROR: .env missing. Run: python scripts/ip4_bootstrap.py" >&2
  exit 1
fi

"$PYTHON" "$ROOT/scripts/start_local_sqld.py"

"$PYTHON" "$ROOT/scripts/preflight.py" || {
  echo "Preflight failed. Fix issues above or run: $PYTHON scripts/ip4_bootstrap.py" >&2
  exit 1
}

_alive_supervisor_pid() {
  local pid
  for pid in $(pgrep -f 'scripts/supervisor_watch.py' 2>/dev/null || true); do
    if kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  done
  return 1
}

MODE="${1:-both}"
WITH_DASHBOARD=false
if [[ "${2:-}" == "--dashboard" ]] || [[ "${1:-}" == "--dashboard" ]]; then
  WITH_DASHBOARD=true
fi

if [[ "$WITH_DASHBOARD" == true ]] || [[ "$MODE" == "watch" && "${2:-}" == "--dashboard" ]]; then
  if ! "$PYTHON" -c "import streamlit" 2>/dev/null; then
    echo "ERROR: streamlit not installed in IP4 .venv." >&2
    echo "Run once:  $PYTHON -m pip install streamlit" >&2
    exit 1
  fi
fi

if [[ "$MODE" == "watch" ]] || [[ "$MODE" == "watch-bg" ]]; then
  EXISTING="$(_alive_supervisor_pid || true)"
  if [[ -n "$EXISTING" ]]; then
    echo "Supervisor already running (pid ${EXISTING})." >&2
    echo "  tail -f logs/supervisor.log logs/apex.log" >&2
    echo "  stop: pkill -f supervisor_watch.py" >&2
    exit 1
  fi
  rm -f "$ROOT/.ip4_supervisor.lock"
  WATCH_ARGS=()
  if [[ "$WITH_DASHBOARD" == true ]] || [[ "${2:-}" == "--dashboard" ]]; then
    WATCH_ARGS+=(--dashboard)
  fi
  if [[ "$MODE" == "watch-bg" ]]; then
    echo "Starting IP4 supervisor in background (DB-driven lifecycle)..."
    nohup "$PYTHON" "$ROOT/scripts/supervisor_watch.py" "${WATCH_ARGS[@]}" \
      >> "$ROOT/logs/supervisor.log" 2>&1 &
    SUP_PID=$!
    sleep 1
    if kill -0 "$SUP_PID" 2>/dev/null; then
      echo "Supervisor started (pid ${SUP_PID})."
      echo "  tail -f logs/supervisor.log logs/apex.log"
      echo "  stop: pkill -f supervisor_watch.py"
      exit 0
    fi
    echo "ERROR: Supervisor failed to start — see logs/supervisor.log" >&2
    exit 1
  fi
  echo "Starting IP4 supervisor watch (DB-driven lifecycle)..."
  echo "Running in foreground — Ctrl+C to stop. Tail: tail -f logs/supervisor.log logs/apex.log"
  exec "$PYTHON" "$ROOT/scripts/supervisor_watch.py" "${WATCH_ARGS[@]}"
fi

if [[ "$MODE" == "dashboard" ]]; then
  echo "IP4 dashboard watchdog only (does NOT start Apex or Crucible)."
  echo "For the full stack use:  $0 watch --dashboard"
  echo "Dashboard: http://127.0.0.1:${IP4_DASHBOARD_PORT:-8501}"
  exec "$PYTHON" "$ROOT/scripts/dashboard_service.py" watch
fi

if pgrep -f "engine_1_apex/ip4_apex_edge.py" >/dev/null 2>&1; then
  echo "ERROR: Apex already running (pid $(pgrep -f 'engine_1_apex/ip4_apex_edge.py' | head -1))." >&2
  exit 1
fi

APEX_ONLY=false
CRUCIBLE_ONLY=false
case "$MODE" in
  apex) APEX_ONLY=true ;;
  crucible) CRUCIBLE_ONLY=true ;;
  both) ;;
  *)
    echo "Usage: $0 [both|apex|crucible|watch|watch-bg|dashboard] [--dashboard]" >&2
    exit 1
    ;;
esac

cleanup() {
  echo "Shutting down IP4 engines..."
  [[ -n "${APEX_PID:-}" ]] && kill "$APEX_PID" 2>/dev/null || true
  [[ -n "${CRUCIBLE_PID:-}" ]] && kill "$CRUCIBLE_PID" 2>/dev/null || true
  [[ -n "${DASHBOARD_PID:-}" ]] && kill "$DASHBOARD_PID" 2>/dev/null || true
  if [[ -f "$ROOT/logs/sqld.pid" ]]; then
    SQLD_PID="$(cat "$ROOT/logs/sqld.pid" 2>/dev/null || true)"
    if [[ -n "$SQLD_PID" ]] && kill -0 "$SQLD_PID" 2>/dev/null; then
      kill "$SQLD_PID" 2>/dev/null || true
    fi
  fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ "$CRUCIBLE_ONLY" == false ]]; then
  echo "Starting Engine 1 — Apex Edge ..."
  "$PYTHON" "$ROOT/engine_1_apex/ip4_apex_edge.py" \
    2>&1 | tee -a "$ROOT/logs/apex.log" &
  APEX_PID=$!
  echo "  Apex pid=$APEX_PID  log=logs/apex.log"
fi

if [[ "$APEX_ONLY" == false ]]; then
  echo "Starting Engine 2 — AutoResearch Crucible ..."
  "$PYTHON" "$ROOT/engine_2_crucible/ip4_swarm_crucible.py" \
    2>&1 | tee -a "$ROOT/logs/crucible.log" &
  CRUCIBLE_PID=$!
  echo "  Crucible pid=$CRUCIBLE_PID  log=logs/crucible.log"
fi

if [[ "$WITH_DASHBOARD" == true ]]; then
  PORT="${IP4_DASHBOARD_PORT:-8501}"
  echo "Starting Engine 3 — Command Center Dashboard on port $PORT ..."
  "$PYTHON" -m streamlit run "$ROOT/engine_3_dashboard/app.py" \
    --server.port "$PORT" \
    --server.headless true \
    2>&1 | tee -a "$ROOT/logs/dashboard.log" &
  DASHBOARD_PID=$!
  echo "  Dashboard pid=$DASHBOARD_PID  log=logs/dashboard.log"
fi

echo ""
echo "IP4 running. Tail logs:"
echo "  tail -f logs/apex.log logs/crucible.log"
if [[ "$WITH_DASHBOARD" == true ]]; then
  echo "  tail -f logs/dashboard.log"
  echo "Dashboard: http://127.0.0.1:${IP4_DASHBOARD_PORT:-8501}"
fi
echo "Press Ctrl+C to stop."

wait
