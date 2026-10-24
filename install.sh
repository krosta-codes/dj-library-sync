#!/bin/bash
# Set up djsync: virtualenv, dependencies and the launchd background agent.
set -euo pipefail

HOME_DIR="$HOME"
# Location independent: this script defines where the project lives.
DJSYNC_HOME="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.jurekkrosta.djsync"
PLIST_TARGET="$HOME_DIR/Library/LaunchAgents/$LABEL.plist"

cd "$DJSYNC_HOME"

echo "==> Creating the virtualenv"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet pyrekordbox

mkdir -p logs backups
chmod +x bin/djsync

echo "==> Checking the setup"
./bin/djsync status

echo
read -r -p "Install the background agent (syncs automatically)? [y/N] " answer
if [[ "${answer:-n}" =~ ^[Yy]$ ]]; then
    mkdir -p "$HOME_DIR/Library/LaunchAgents"
    sed -e "s|__DJSYNC_HOME__|$DJSYNC_HOME|g" -e "s|__HOME__|$HOME_DIR|g" "$DJSYNC_HOME/$LABEL.plist" > "$PLIST_TARGET"
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST_TARGET"
    echo "Agent installed. It syncs when rekordbox quits, and every 30 minutes as a fallback."
    echo "Remove it later with: launchctl bootout gui/\$UID/$LABEL && rm $PLIST_TARGET"
else
    echo "Skipped. Run the sync by hand with: $DJSYNC_HOME/bin/djsync"
fi

echo
echo "Optional, so you can type 'djsync' from anywhere:"
echo "  ln -s $DJSYNC_HOME/bin/djsync /opt/homebrew/bin/djsync"
