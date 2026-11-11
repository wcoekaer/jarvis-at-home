#!/bin/bash
# Install / manage the Jarvis launchd service.
#
#   ./install.sh daemon     # system LaunchDaemon: starts at boot, no login needed (uses sudo)
#   ./install.sh agent      # per-user LaunchAgent: starts when you log in
#   ./install.sh context    # calendar/reminders/mail helper (LaunchAgent, every 5 min)
#   ./install.sh homekit-bridge  # HomeKit -> MQTT bridge app (needs Xcode signed in to your team)
#   ./install.sh restart    # restart whichever is installed (after editing config.yaml)
#   ./install.sh status
#   ./install.sh uninstall
set -euo pipefail

LABEL="com.jarvis.homepod"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
USER_NAME="$(id -un)"
DAEMON_PLIST="/Library/LaunchDaemons/$LABEL.plist"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/jarvis"
CTX_LABEL="com.jarvis.context"
CTX_PLIST="$HOME/Library/LaunchAgents/$CTX_LABEL.plist"
CTX_APP="$DIR/helpers/build/JarvisContext.app"

ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "Creating virtualenv..."
        /opt/homebrew/bin/python3 -m venv "$DIR/.venv"
    fi
    "$PY" -m pip install -q --disable-pip-version-check -r "$DIR/requirements.txt"
    if [ ! -f "$DIR/.env" ]; then
        echo "WARNING: $DIR/.env not found. Copy .env.example to .env and add your API key(s)."
    fi
    mkdir -p "$LOG_DIR"
}

write_plist() {  # $1 = path, $2 = "daemon" | "agent"
    local user_key=""
    if [ "$2" = "daemon" ]; then
        user_key="<key>UserName</key><string>$USER_NAME</string>"
    fi
    cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    $user_key
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DIR/jarvis.py</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>$HOME</string>
        <key>PYTHONUNBUFFERED</key><string>1</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$LOG_DIR/stdout.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
EOF
}

case "${1:-}" in
    daemon)
        # Get sudo first so a failed password leaves any existing agent running.
        sudo -v || { echo "sudo failed; nothing changed. Run this in a real terminal."; exit 1; }
        ensure_venv
        [ -f "$AGENT_PLIST" ] && { launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$AGENT_PLIST"; }
        tmp="$(mktemp)"; write_plist "$tmp" daemon
        sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
        sudo install -m 644 -o root -g wheel "$tmp" "$DAEMON_PLIST"; rm -f "$tmp"
        sudo launchctl bootstrap system "$DAEMON_PLIST"
        echo "Installed LaunchDaemon $DAEMON_PLIST (starts at boot). Logs: $LOG_DIR"
        ;;
    agent)
        ensure_venv
        mkdir -p "$(dirname "$AGENT_PLIST")"
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        write_plist "$AGENT_PLIST" agent
        launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"
        echo "Installed LaunchAgent $AGENT_PLIST (starts at login). Logs: $LOG_DIR"
        ;;
    context)
        [ -x "$CTX_APP/Contents/MacOS/JarvisContext" ] || "$DIR/helpers/build-context.sh"
        mkdir -p "$(dirname "$CTX_PLIST")" "$LOG_DIR"
        launchctl bootout "gui/$(id -u)/$CTX_LABEL" 2>/dev/null || true
        cat > "$CTX_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$CTX_LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$CTX_APP/Contents/MacOS/JarvisContext</string></array>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>300</integer>
    <key>StandardOutPath</key><string>/dev/null</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/context.err.log</string>
</dict>
</plist>
EOF
        launchctl bootstrap "gui/$(id -u)" "$CTX_PLIST"
        echo "Installed context helper $CTX_PLIST (refreshes every 5 minutes while you are logged in)"
        ;;
    homekit-bridge)
        # Build (signed, HomeKit entitlement), install to ~/Applications and keep running.
        # Needs Xcode signed into your Apple developer account (Settings > Accounts).
        HK_LABEL="com.jarvis.homekit"
        HK_PLIST="$HOME/Library/LaunchAgents/$HK_LABEL.plist"
        HK_APP="$HOME/Applications/JarvisHome.app"
        # codesign needs the login keychain unlocked in *this* session (SSH sessions
        # start locked; errSecInternalComponent means it couldn't use the key).
        KC="$HOME/Library/Keychains/login.keychain-db"
        echo "Unlocking the login keychain for code signing (your Mac login password):"
        security unlock-keychain "$KC"
        # Signing team and bundle id come from .env (kept out of git).
        JARVIS_TEAM_ID="$(sed -n 's/^JARVIS_TEAM_ID=//p' "$DIR/.env")"
        JARVIS_BUNDLE_PREFIX="$(sed -n 's/^JARVIS_BUNDLE_PREFIX=//p' "$DIR/.env")"
        if [ -z "$JARVIS_TEAM_ID" ] || [ -z "$JARVIS_BUNDLE_PREFIX" ]; then
            echo "Set JARVIS_TEAM_ID and JARVIS_BUNDLE_PREFIX in .env (see README)"; exit 1
        fi
        export JARVIS_TEAM_ID JARVIS_BUNDLE_PREFIX
        ( cd "$DIR/helpers/JarvisHome" && xcodegen generate >/dev/null && \
          xcodebuild -project JarvisHome.xcodeproj -scheme JarvisHome \
            -destination 'platform=macOS,variant=Mac Catalyst' -configuration Release \
            -allowProvisioningUpdates -derivedDataPath build build > "$LOG_DIR/homekit-build.log" 2>&1 )
        grep -E "error|errSec|BUILD (SUCCEEDED|FAILED)" "$LOG_DIR/homekit-build.log" | grep -v CoreSimulator | tail -8
        built="$DIR/helpers/JarvisHome/build/Build/Products/Release-maccatalyst/JarvisHome.app"
        [ -d "$built" ] || { echo "build failed"; exit 1; }
        mkdir -p "$HOME/Applications"
        launchctl bootout "gui/$(id -u)/$HK_LABEL" 2>/dev/null || true
        rm -rf "$HK_APP"; cp -R "$built" "$HK_APP"
        cat > "$HK_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$HK_LABEL</string>
    <key>ProgramArguments</key>
    <!-- Catalyst apps must be launched via LaunchServices (open), not exec'd
         directly, or UIKit aborts with "_mainSceneIdentifier should have been set".
         -W keeps open running while the app runs so KeepAlive can restart it. -->
    <array>
        <string>/usr/bin/open</string><string>-W</string><string>-g</string>
        <string>--env</string><string>JARVIS_ENV=$DIR/.env</string>
        <string>$HK_APP</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardErrorPath</key><string>$LOG_DIR/homekit.err.log</string>
</dict>
</plist>
EOF
        # First launch via Finder so macOS can show the Home access prompt.
        open "$HK_APP"
        read -r -p "Approve 'JarvisHome would like to access your home data' if asked, then press Enter... " _
        pkill -f "$HK_APP/Contents/MacOS/JarvisHome" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$HK_PLIST" 2>/dev/null || true
        echo "Installed JarvisHome bridge ($HK_APP), kept running by $HK_PLIST"
        ;;
    restart)
        if [ -f "$DAEMON_PLIST" ]; then sudo launchctl kickstart -k "system/$LABEL"
        elif [ -f "$AGENT_PLIST" ]; then launchctl kickstart -k "gui/$(id -u)/$LABEL"
        else echo "not installed"; exit 1; fi
        echo "restarted"
        ;;
    status)
        if [ -f "$DAEMON_PLIST" ]; then sudo launchctl print "system/$LABEL" | grep -E "state|pid|last exit" || true
        elif [ -f "$AGENT_PLIST" ]; then launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|pid|last exit" || true
        else echo "not installed"; fi
        tail -n 15 "$LOG_DIR/jarvis.log" 2>/dev/null || true
        ;;
    uninstall)
        if [ -f "$DAEMON_PLIST" ]; then sudo launchctl bootout "system/$LABEL" 2>/dev/null || true; sudo rm -f "$DAEMON_PLIST"; fi
        if [ -f "$AGENT_PLIST" ]; then launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$AGENT_PLIST"; fi
        if [ -f "$CTX_PLIST" ]; then launchctl bootout "gui/$(id -u)/$CTX_LABEL" 2>/dev/null || true; rm -f "$CTX_PLIST"; fi
        HK_PLIST="$HOME/Library/LaunchAgents/com.jarvis.homekit.plist"
        if [ -f "$HK_PLIST" ]; then launchctl bootout "gui/$(id -u)/com.jarvis.homekit" 2>/dev/null || true; rm -f "$HK_PLIST"; fi
        echo "uninstalled"
        ;;
    *)
        sed -n '2,11p' "$0"; exit 1 ;;
esac
