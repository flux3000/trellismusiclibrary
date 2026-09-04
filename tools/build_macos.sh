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
APP_NAME=$(python3 -c 'from version import APP_NAME; print(APP_NAME)')

echo "── Building ${APP_NAME} ${VERSION} ─────────────────────"

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

APP="dist/${APP_NAME}.app"
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

# ── The check that actually matters ──────────────────────────────────────────
# A bundle that is missing a data file or a hidden import does not fail during
# use — it fails on LAUNCH, before there is a window, and from Finder that looks
# like nothing happening at all. Which is how a broken build got as far as being
# double-clicked on 2026-08-25.
#
# So: start the real bundled binary, against a throwaway data directory so it
# cannot touch a real library, and make it prove it can get all the way through
# importing itself and creating a database.
echo
echo "── Startup self-test ──────────────────────────────────────────"
SELFTEST_DIR=$(mktemp -d)
if TRELLIS_DATA_DIR="$SELFTEST_DIR" TRELLIS_SELFTEST=1 \
     "$APP/Contents/MacOS/${APP_NAME}" 2>&1 | sed 's/^/  /'; then
  echo "  ✓ the app starts"
else
  echo
  echo "  ✗ THE APP DOES NOT START. The output above is the real error —" >&2
  echo "    a missing data file or hidden import. Add it to trellis.spec." >&2
  rm -rf "$SELFTEST_DIR"
  exit 1
fi
rm -rf "$SELFTEST_DIR"

# PyInstaller leaves TWO copies: the raw collected output, and the .app it
# assembles from it. The .app is fully self-contained, so the raw folder is
# leftovers — and leaving it there doubles the disk cost of every build and
# invites someone to ship the wrong one.
rm -rf "dist/${APP_NAME}"

echo
du -sh "$APP"
echo
echo "── Done ───────────────────────────────────────────────────────"
echo "  $APP"

# ── Signing ─────────────────────────────────────────────────────────────────
# Opt-in, on purpose. With no signing identity in the environment this script
# behaves exactly as it did before signing existed, so a build never fails
# because of a certificate on a machine that has no reason to hold one.
if [[ -n "${TRELLIS_SIGN_IDENTITY:-}" ]]; then
  echo
  ./tools/sign_macos.sh
else
  echo
  echo "  This build is UNSIGNED, so the first launch on any Mac, including"
  echo "  yours, is refused by Gatekeeper with no Open button anywhere."
  echo "  Control-click → Open has NOT worked since macOS Sequoia. Either:"
  echo "    xattr -dr com.apple.quarantine \"$APP\""
  echo "  or open it, dismiss the dialog, then System Settings → Privacy &"
  echo "  Security → Open Anyway. Once per machine. README.md and the"
  echo "  website's /download page both say the same thing; keep them in step."
  echo
  echo "  To ship it instead: set TRELLIS_SIGN_IDENTITY and"
  echo "  TRELLIS_NOTARY_PROFILE, then rebuild. tools/sign_macos.sh explains"
  echo "  where both come from."
fi

# ── What to do next ─────────────────────────────────────────────────────────
# version.py says which Trellis this is; a git tag says which commit produced
# it. They live in two systems, neither can read the other at the moment it
# matters, and nothing makes them agree — which is why v0.1.2 and v0.1.3 were
# both tagged while version.py still said 0.1.1, shipping two releases that
# misreported themselves in the About box and in CFBundleVersion.
#
# This enforces nothing and never blocks a build. It prints the commands with
# the number already filled in, at the one moment the number is on screen
# anyway, so there is nothing to retype and no ordering to remember.
#
# Guarded so a missing tag or a missing git is not an error: this is the last
# thing a successful build does, and it must not be what makes one fail.
echo
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  :                                  # not a checkout (unpacked tarball) — nothing to say
elif git rev-parse -q --verify "refs/tags/v${VERSION}" >/dev/null 2>&1; then
  # The dangerous case, and the reason this block earns its keep. Two different
  # binaries now claim one version, and nothing downstream can tell them apart.
  TAGGED=$(git rev-list -n1 "v${VERSION}" 2>/dev/null | cut -c1-7)
  echo "  ⚠ Tag v${VERSION} ALREADY EXISTS, pointing at ${TAGGED}."
  echo "    This build is a SECOND, different binary claiming that version."
  echo "    Bump version.py before this goes anywhere."
else
  echo "  Next, once you are happy with it:"
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "    git commit -am \"Version ${VERSION}\""
  fi
  echo "    git tag -a v${VERSION} -m \"v${VERSION}\""
  echo "    git push origin main && git push origin v${VERSION}"
fi
