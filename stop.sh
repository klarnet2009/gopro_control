#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# GoPro Roll Call — stop server
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GREEN="\033[0;32m"; YELLOW="\033[0;33m"; RESET="\033[0m"
ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }

PID_FILE="$SCRIPT_DIR/.gopro_pid"
STOPPED=0

# ── Try PID file first ───────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null && ok "Stopped server (PID $PID)" && STOPPED=1
    sleep 0.5
    kill -9 "$PID" 2>/dev/null || true   # force-kill if still alive
  fi
  rm -f "$PID_FILE"
fi

# ── Fallback: kill by port ───────────────────────────────────────────────────
PORT_PIDS=$(lsof -ti :8000 2>/dev/null || true)
if [[ -n "$PORT_PIDS" ]]; then
  echo "$PORT_PIDS" | xargs kill -9 2>/dev/null && ok "Killed remaining process(es) on port 8000" && STOPPED=1
fi

if [[ $STOPPED -eq 0 ]]; then
  warn "No running GoPro Roll Call server found"
fi
