#!/usr/bin/env bash
# One-time (and re-runnable) setup of the nightly Aptem sync on an Ubuntu/Debian VPS.
#
#   cd /opt/evidence-content-fetch          # wherever you cloned the repo
#   sudo NIGHTLY_TIMEZONE=Europe/London ./deploy/install_vps.sh
#
# What it does:
#   1. creates .venv and installs backend/requirements.txt
#   2. installs the browser (Google Chrome on amd64, bundled Chromium on arm64)
#   3. links .playwright-runtime/playwright -> the pip Playwright package, which
#      is where scripts/capture-aptem-modern.cjs loads Playwright + node from
#   4. installs + enables the systemd timer (every night 00:00 NIGHTLY_TIMEZONE)
#
# It never runs migrations and never touches Aptem or the database.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo ./deploy/install_vps.sh" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-${SUDO_USER:-}}"
TIMEZONE="${NIGHTLY_TIMEZONE:-Europe/London}"
UNIT_DIR=/etc/systemd/system

if [ -z "$APP_USER" ] || [ "$APP_USER" = "root" ]; then
    echo "Set APP_USER to the (non-root) account that owns $ROOT, e.g. sudo APP_USER=deploy $0" >&2
    exit 1
fi
APP_GROUP="$(id -gn "$APP_USER")"
as_app() { sudo -u "$APP_USER" -H "$@"; }

echo "==> Project: $ROOT  user: $APP_USER  timezone: $TIMEZONE"

[ -f "$ROOT/backend/.env" ] || { echo "Missing $ROOT/backend/.env (copy it from the old machine first)" >&2; exit 1; }
[ -f "/usr/share/zoneinfo/$TIMEZONE" ] || { echo "Unknown timezone: $TIMEZONE" >&2; exit 1; }

echo "==> System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip util-linux >/dev/null

python3 - <<'EOF'
import sys
if sys.version_info < (3, 12):
    sys.exit(f"Python 3.12+ is required (Django 6); found {sys.version.split()[0]}")
EOF

echo "==> Python virtualenv"
chown -R "$APP_USER:$APP_GROUP" "$ROOT"
[ -x "$ROOT/.venv/bin/python" ] || as_app python3 -m venv "$ROOT/.venv"
as_app "$ROOT/.venv/bin/pip" install -q --upgrade pip
as_app "$ROOT/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

echo "==> Playwright browser"
"$ROOT/.venv/bin/python" -m playwright install-deps chromium >/dev/null
ARCH="$(dpkg --print-architecture)"
if [ "$ARCH" = "amd64" ]; then
    # Same browser the collector used on Windows (channel: 'chrome').
    "$ROOT/.venv/bin/python" -m playwright install chrome
else
    # Google Chrome has no Linux build for $ARCH; use Playwright's Chromium.
    as_app "$ROOT/.venv/bin/python" -m playwright install chromium
    if ! grep -q '^APTEM_BROWSER_CHANNEL=' "$ROOT/backend/.env"; then
        printf '\n# Linux %s VPS: no Google Chrome build, use bundled Chromium\nAPTEM_BROWSER_CHANNEL=\n' "$ARCH" >> "$ROOT/backend/.env"
        echo "    added APTEM_BROWSER_CHANNEL= to backend/.env"
    fi
fi

echo "==> Project-local Playwright runtime"
PW_DIR="$(as_app "$ROOT/.venv/bin/python" -c 'import os, playwright; print(os.path.dirname(playwright.__file__))')"
[ -x "$PW_DIR/driver/node" ] && [ -d "$PW_DIR/driver/package" ] || { echo "Playwright driver not found in $PW_DIR" >&2; exit 1; }
as_app mkdir -p "$ROOT/.playwright-runtime"
if [ -d "$ROOT/.playwright-runtime/playwright" ] && [ ! -L "$ROOT/.playwright-runtime/playwright" ]; then
    # A Windows copy (node.exe) carried over with the project folder.
    mv "$ROOT/.playwright-runtime/playwright" "$ROOT/.playwright-runtime/playwright.windows-$(date +%s)"
fi
as_app ln -sfn "$PW_DIR" "$ROOT/.playwright-runtime/playwright"

chmod +x "$ROOT/scripts/nightly_sync.sh"
chmod 600 "$ROOT/backend/.env"
chown "$APP_USER:$APP_GROUP" "$ROOT/backend/.env"

echo "==> Checking Django + database connection"
as_app bash -c "cd '$ROOT/backend' && '$ROOT/.venv/bin/python' manage.py check >/dev/null && '$ROOT/.venv/bin/python' manage.py showmigrations aptem | tail -3"

echo "==> systemd timer"
render() {
    sed -e "s|@ROOT@|$ROOT|g" -e "s|@APP_USER@|$APP_USER|g" \
        -e "s|@APP_GROUP@|$APP_GROUP|g" -e "s|@TIMEZONE@|$TIMEZONE|g" "$1" > "$2"
}
render "$ROOT/deploy/systemd/aptem-nightly.service" "$UNIT_DIR/aptem-nightly.service"
render "$ROOT/deploy/systemd/aptem-nightly.timer"   "$UNIT_DIR/aptem-nightly.timer"
systemctl daemon-reload
systemctl enable --now aptem-nightly.timer

echo
systemctl list-timers aptem-nightly.timer --no-pager
echo
echo "Done. Next run is shown above. Useful commands:"
echo "  sudo systemctl start aptem-nightly.service      # run now"
echo "  journalctl -u aptem-nightly.service -f          # follow output"
echo "  ls -t $ROOT/logs/nightly_*.log | head           # per-run log files"
