#!/usr/bin/env bash
#
# tools/build_macos.sh — produce Trellis.app.
#
# Must run ON A MAC. PyInstaller does not cross-compile: a Mac app is built on
# a Mac, a Windows one on Windows. There is no flag for this.
#
#   ./tools/build_macos.sh
#
# Output: dist/Trellis.app  — drag it to /Applications, or to another machine.
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION=$(python3 -c 'from version import __version__; print(__version__)')

echo "── Building Trellis ${VERSION} ────────────────────────────────"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This builds a MAC app and must run on macOS. On Linux or Windows you"
  echo "would get a binary for that platform instead — which is fine, but it is"
  echo "not what this script is for." >&2
  exit 1
fi

python3 -m PyInstaller --version >/dev/null 2>&1 || {
  echo "Installing PyInstaller…"; pip3 install pyinstaller; }

# A stale build/ is the usual explanation for "I fixed that and it is still
# broken" — PyInstaller caches aggressively.
rm -rf build dist

python3 -m PyInstaller trellis.spec --noconfirm --clean

APP="dist/Trellis.app"
[[ -d "$APP" ]] || { echo "Build finished but $APP is missing." >&2; exit 1; }

echo
echo "── Checks ─────────────────────────────────────────────────────"
# The three things that are wrong often enough to be worth asserting.
[[ -f "$APP/Contents/Resources/Trellis.icns" ]] \
  && echo "  ✓ icon present" || echo "  ✗ icon MISSING"
find "$APP" -name 'libsndfile*' | grep -q . \
  && echo "  ✓ libsndfile bundled" || echo "  ✗ libsndfile MISSING — audio analysis will crash"
[[ -f "$APP/Contents/Resources/app/static/index.html" ]] \
  || [[ -f "$APP/Contents/Frameworks/app/static/index.html" ]] \
  && echo "  ✓ frontend bundled" || echo "  ✗ frontend MISSING — the window will be blank"

echo
du -sh "$APP"
echo
echo "── Done ───────────────────────────────────────────────────────"
echo "  $APP"
echo
echo "  First launch on ANY Mac, including yours, will be refused by Gatekeeper"
echo "  because the app is unsigned. Right-click it → Open → Open. Once per"
echo "  machine. Signing it properly costs \$99/year and can wait."
