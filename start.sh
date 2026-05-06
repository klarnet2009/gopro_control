#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# GoPro Roll Call — start server
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

BOLD="\033[1m"; GREEN="\033[0;32m"; YELLOW="\033[0;33m"; RED="\033[0;31m"; RESET="\033[0m"
ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET}  $1"; }

PID_FILE="$SCRIPT_DIR/.gopro_pid"
LOG_FILE="$SCRIPT_DIR/.gopro_server.log"
URL="http://127.0.0.1:8000"

echo -e "\n${BOLD}GoPro Roll Call${RESET}"

# ── Check venv ───────────────────────────────────────────────────────────────
if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
  fail "Virtual environment not found. Run install.sh first:"
  echo "       bash install.sh"
  exit 1
fi

# ── Check already running ────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    ok "Server already running (PID $OLD_PID)"
    echo ""
    echo -e "  Opening ${BOLD}$URL${RESET}"
    open "$URL"
    exit 0
  else
    rm -f "$PID_FILE"
  fi
fi

# Also check port directly in case PID file is stale
if lsof -ti :8000 &>/dev/null; then
  warn "Port 8000 is already in use by another process"
  echo ""
  echo -e "  Opening ${BOLD}$URL${RESET}"
  open "$URL"
  exit 0
fi

# ── Start server ─────────────────────────────────────────────────────────────
chflags nohidden "$SCRIPT_DIR"/.venv/lib/python*/site-packages/*.pth \
  "$SCRIPT_DIR"/.venv/lib/python*/site-packages/__editable__*.pth 2>/dev/null || true
source "$SCRIPT_DIR/.venv/bin/activate"

echo "  Starting server…  (logs → .gopro_server.log)"
python -m gopro_mgmt > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# ── Wait for ready ───────────────────────────────────────────────────────────
TRIES=0
MAX_TRIES=20   # 10 seconds total
while [[ $TRIES -lt $MAX_TRIES ]]; do
  if curl -sf "$URL/api/cameras" -o /dev/null 2>/dev/null; then
    break
  fi
  TRIES=$((TRIES + 1))
  sleep 0.5
done

if [[ $TRIES -eq $MAX_TRIES ]]; then
  fail "Server did not start within 10 seconds"
  echo ""
  echo "  Last log lines:"
  tail -20 "$LOG_FILE" | sed 's/^/    /'
  kill "$SERVER_PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 1
fi

ok "Server ready  (PID $SERVER_PID)"
echo ""
echo -e "  Opening ${BOLD}$URL${RESET}"
echo -e "  Stop with: ${BOLD}./stop.sh${RESET}  or  Ctrl+C in this terminal"
echo ""

open "$URL"
