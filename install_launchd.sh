#!/bin/bash
# Sets up the LeaseResources dispatcher on this Mac (the counterpart of install_task.ps1): installs and starts the
# LaunchAgent com.goocy.leaseresources, which keeps `res.py watch` (the dispatcher's keeper) running from login on.
# The dispatcher restarts itself whenever res.py or probes.py change, so code updates need no re-run.
# Re-running it cleanly replaces any existing copy. Nothing is registered in ~/.claude/settings.json.
#
#   ./install_launchd.sh           # install + (re)load
#   ./install_launchd.sh uninstall # unload + remove the agent (leases/ and logs/ are left alone)
set -euo pipefail

# Under sudo, id -u returns 0 and there is no gui/0 domain: the bootstrap fails and every file created comes out root-owned.
[[ $EUID -eq 0 ]] && { echo "Run this without sudo: launchctl needs your own GUI domain, and id -u returns 0 under sudo."; exit 1; }

LABEL="com.goocy.leaseresources"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/LeaseResources"             # machine-local since 2026-09-28 (nothing rewritten in ResilioSync)
LOG="$LOGDIR/launchd.log"
VENV="$HOME/venvs/LeaseResources"
PYTHON="$VENV/bin/python"
mkdir -p "$LOGDIR"

if [[ "${1:-}" == "uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" || true   # not loaded yet prints "No such process"; anything else is worth seeing
    rm -f "$PLIST"
    echo "Removed LaunchAgent $LABEL."
    exit 0
fi

if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c "import psutil" 2>/dev/null; then
    BREW="/opt/homebrew/bin/python3"                       # Apple Silicon Homebrew (Altiplano)
    [[ -x "$BREW" ]] || BREW="/usr/local/bin/python3"      # Intel Homebrew (Sandbank)
    echo "The venv $VENV with psutil is missing. Create it with:"
    echo "  $BREW -m venv $VENV && $VENV/bin/pip install psutil"
    exit 1
fi

# Fails loudly if this hostname is not one of the machines in config/defaults.json.
"$PYTHON" -B "$DIR/res.py" status >/dev/null

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>$LABEL</string>

    <!-- A LaunchAgent, not a Daemon: notifications and the user's processes are only visible from the user's session. -->
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-B</string>
        <string>$DIR/res.py</string>
        <string>watch</string>
    </array>
    <key>WorkingDirectory</key> <string>$DIR</string>

    <!-- Continuous from login; relaunched if it dies, at most once a minute. -->
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>
    <key>ThrottleInterval</key> <integer>60</integer>
    <key>ProcessType</key>      <string>Background</string>

    <!-- Bootstrap output and tracebacks only; res.py writes ~/Library/Logs/LeaseResources/leaseresources.<machine>.<yyMMdd>.log itself. -->
    <key>StandardOutPath</key>  <string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$LABEL" || true   # not loaded yet prints "No such process"; anything else is worth seeing
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed and loaded LaunchAgent $LABEL."
echo "Runs : $PYTHON -B $DIR/res.py watch"
echo "Log  : $LOGDIR/leaseresources.<machine>.<yyMMdd>.log"
echo "Check: python3 $DIR/res.py status"
