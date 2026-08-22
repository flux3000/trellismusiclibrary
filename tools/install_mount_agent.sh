#!/bin/bash
#
# tools/install_mount_agent.sh — install (or remove) the LaunchAgent that keeps
# the library share mounted. See tools/mount_library.py for the why.
#
#   ./tools/install_mount_agent.sh            install + start
#   ./tools/install_mount_agent.sh uninstall  stop + remove
#
# Runs in the gui/<uid> launchd domain on purpose: the AppleScript `mount volume`
# call needs a GUI session to reach the login keychain. A system-domain daemon
# would have neither, which is also why a headless Flux would never see this
# mount.

set -euo pipefail

LABEL="com.fluxaudio.mountlibrary"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mount_library.py"

# Resolve python3 now, at install time. Hardcoding /usr/bin/python3 is
# wrong twice over: it is Apple's stub (which can block on a GUI prompt
# when Command Line Tools are incomplete), and this project runs on the
# global framework python3. launchd gets a minimal PATH, so the plist
# must carry an absolute path either way.
PYTHON="$(command -v python3 || true)"
[[ -x "$PYTHON" ]] || { echo "No python3 on PATH" >&2; exit 1; }
PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed ${LABEL}."
  exit 0
fi

[[ -x "$HELPER" ]] || { echo "Helper not executable: $HELPER" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/FluxAudio"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${HELPER}</string>
  </array>

  <!-- At login, then every 5 minutes. The helper exits in milliseconds when
       the mount is healthy, so the steady-state cost is negligible. -->
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>

  <!-- Crash-loop guard: if the helper somehow dies instantly, launchd will
       still not restart it faster than this. -->
  <key>ThrottleInterval</key><integer>60</integer>

  <key>StandardOutPath</key><string>${HOME}/Library/Logs/FluxAudio/agent.out</string>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/FluxAudio/agent.err</string>
</dict>
</plist>
PLISTEOF

# bootout first so re-running this script is idempotent
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "$PLIST"

echo "Installed ${LABEL}"
echo "  python : ${PYTHON}"
echo "  helper : ${HELPER}"
echo "  log    : ~/Library/Logs/FluxAudio/mount_library.log"
echo
echo "Verify with:  launchctl print ${DOMAIN}/${LABEL} | head -20"
