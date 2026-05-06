#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# GoPro Roll Call — double-click launcher (macOS .command)
# Place this file in the same folder as the project.
# In Finder: right-click → Open  (first time, to bypass Gatekeeper)
# Afterwards: double-click to launch.
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Run installer on first launch (creates .venv if missing)
if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
  echo "First run — installing dependencies..."
  bash "$SCRIPT_DIR/install.sh"
  echo ""
fi

bash "$SCRIPT_DIR/start.sh"

# Keep the Terminal window open so the user can see logs / errors
echo ""
echo "Press any key to close this window…"
read -r -n 1
