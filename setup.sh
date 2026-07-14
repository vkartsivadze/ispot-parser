#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Homebrew ───────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || eval "$(/usr/local/bin/brew shellenv)" 2>/dev/null
else
    echo "Homebrew already installed."
fi

# ── Python 3 ───────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "Installing Python 3..."
    brew install python
else
    echo "Python $(python3 --version) already installed."
fi

# ── Python packages ────────────────────────────────────────
echo "Installing Python dependencies..."
pip3 install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

# ── Camoufox browser ───────────────────────────────────────
echo "Downloading Camoufox browser (Firefox)..."
python3 -m camoufox fetch

# ── Cron schedule (12:00, 15:00, 18:00, 21:00 daily) ──────
PYTHON=$(command -v python3)
CRON_JOB="0 12,15,18,21 * * * cd \"$SCRIPT_DIR\" && $PYTHON parser.py >> \"$SCRIPT_DIR/parser.log\" 2>&1"

if crontab -l 2>/dev/null | grep -qF "ispot-parser"; then
    echo "Cron job already exists, skipping."
else
    echo "Adding cron schedule (12:00, 15:00, 18:00, 21:00)..."
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "Cron job added."
fi

echo ""
echo "Setup complete."
echo "Parser will run automatically at 12:00, 15:00, 18:00, 21:00."
echo "Logs: $SCRIPT_DIR/parser.log"
echo ""
echo "To run manually: python3 $SCRIPT_DIR/parser.py"
