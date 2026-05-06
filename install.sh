#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# GoPro Roll Call — macOS installer
# Run once after cloning: bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

BOLD="\033[1m"; GREEN="\033[0;32m"; YELLOW="\033[0;33m"; RED="\033[0;31m"; RESET="\033[0m"

banner() { echo -e "\n${BOLD}$1${RESET}"; }
ok()     { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn()   { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail()   { echo -e "  ${RED}✗${RESET}  $1"; }

banner "GoPro Roll Call — Installer"
echo    "  Project: $SCRIPT_DIR"

# ── 1. macOS version ────────────────────────────────────────────────────────
banner "Checking system…"
MACOS_VER=$(sw_vers -productVersion 2>/dev/null || echo "0.0")
MACOS_MAJOR=$(echo "$MACOS_VER" | cut -d. -f1)
if [[ "$MACOS_MAJOR" -lt 12 ]]; then
  fail "macOS $MACOS_VER detected. Bluetooth LE (bleak) requires macOS 12 Monterey or later."
  exit 1
fi
ok "macOS $MACOS_VER"

# ── 2. Python ────────────────────────────────────────────────────────────────
banner "Checking Python…"

find_python() {
  for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
      VER=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      MAJOR=$(echo "$VER" | cut -d. -f1)
      MINOR=$(echo "$VER" | cut -d. -f2)
      if [[ "$MAJOR" -eq 3 && "$MINOR" -ge 11 ]]; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

if ! PYTHON=$(find_python); then
  fail "Python 3.11+ not found."
  echo ""
  echo "  Install via Homebrew:"
  echo "    brew install python@3.12"
  echo ""
  echo "  Or download from: https://www.python.org/downloads/"
  exit 1
fi

PYVER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
ok "Python $PYVER  ($PYTHON)"

# ── 3. Virtual environment ──────────────────────────────────────────────────
banner "Setting up virtual environment…"
if [[ -d ".venv" ]]; then
  warn ".venv already exists — skipping creation (delete it to reinstall from scratch)"
else
  "$PYTHON" -m venv .venv
  ok "Created .venv"
fi

# Activate
source .venv/bin/activate
ok "Activated .venv  (Python $("$PYTHON" --version 2>&1))"

# ── 4. Install dependencies ─────────────────────────────────────────────────
banner "Installing dependencies…"
pip install --upgrade pip --quiet
pip install -e ".[dev]" --quiet
ok "All packages installed"

# ── 5. Config file ──────────────────────────────────────────────────────────
banner "Config…"
if [[ ! -f "config.yaml" ]]; then
  cp config.example.yaml config.yaml
  ok "Created config.yaml from example — edit it to add your cameras"
else
  ok "config.yaml already exists"
fi

# ── 6. ffmpeg (optional, for COHN preview) ──────────────────────────────────
banner "Checking ffmpeg (optional, for COHN live preview)…"
if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "'ffmpeg' is not installed."
  echo ""
  echo "  The COHN live preview feature will be unavailable without ffmpeg."
  echo "  Install via 'brew install ffmpeg' (Mac), 'apt install ffmpeg' (Linux),"
  echo "  or 'choco install ffmpeg' (Windows), then restart the server."
  echo ""
else
  ok "ffmpeg present"
fi

# ── 7. Bluetooth permission reminder ────────────────────────────────────────
banner "Bluetooth access"
echo ""
echo -e "  ${BOLD}IMPORTANT — macOS requires explicit Bluetooth permission for BLE.${RESET}"
echo ""
echo "  The first time you click LINK on a camera, macOS will prompt:"
echo "    \"Terminal would like to use Bluetooth\""
echo "  Click Allow."
echo ""
echo "  If you already dismissed that prompt or nothing happens:"
echo "    System Settings → Privacy & Security → Bluetooth"
echo "    → enable the toggle next to Terminal (or your terminal app)"
echo ""

# ── 8. Done ─────────────────────────────────────────────────────────────────
banner "Installation complete"
echo ""
echo -e "  To start the server:  ${BOLD}./start.sh${RESET}"
echo -e "  To stop the server:   ${BOLD}./stop.sh${RESET}"
echo ""
echo "  Or double-click  GoPro Roll Call.command  in Finder."
echo ""
