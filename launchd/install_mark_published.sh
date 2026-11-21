#!/bin/bash
# Install (or remove) a launchd job that runs mark_published.py every 6 hours
# and once at login. Output goes to ~/Library/Logs/mark_published.log.
#
#   ./launchd/install_mark_published.sh            # install / update
#   ./launchd/install_mark_published.sh --remove   # uninstall
#   INTERVAL_HOURS=3 ./launchd/install_mark_published.sh
set -euo pipefail

LABEL="com.ronconrails.mark-published"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGFILE="$HOME/Library/Logs/mark_published.log"
INTERVAL_HOURS="${INTERVAL_HOURS:-6}"

if [[ -x "$REPO/.venv/bin/python3" ]]; then
  PYTHON="$REPO/.venv/bin/python3"
else
  PYTHON="/usr/bin/python3"
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$PLIST"
  echo "Removed $LABEL."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/mark_published.py</string>
    <string>--quiet</string>
  </array>
  <key>StartInterval</key>
  <integer>$((INTERVAL_HOURS * 3600))</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOGFILE</string>
  <key>StandardErrorPath</key>
  <string>$LOGFILE</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL: runs every $INTERVAL_HOURS hours and at login."
echo "  script: $REPO/mark_published.py"
echo "  python: $PYTHON"
echo "  log:    $LOGFILE"
echo "Run it now:  launchctl kickstart gui/$(id -u)/$LABEL"
