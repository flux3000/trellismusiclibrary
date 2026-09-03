#!/usr/bin/env bash
#
# tools/sign_macos.sh — sign, notarize and staple dist/<APP_NAME>.app, then
# package it as a DMG and a zip that both open on a stranger's Mac with a
# double click.
#
#   ./tools/sign_macos.sh
#
# Run it after ./tools/build_macos.sh. build_macos.sh calls it automatically
# when the two variables below are set, so normally you do not run this by
# hand.
#
# ── What you need once, ever ─────────────────────────────────────────────────
#
#   1. An Apple Developer Program membership ($99/year) and a "Developer ID
#      Application" certificate installed in the login keychain. Xcode's
#      Settings → Accounts → Manage Certificates makes one; so does the
#      developer portal.
#
#           security find-identity -v -p codesigning
#
#      prints the identities you have. Use the full string, quotes and all.
#
#   2. Notary credentials stored in the keychain under a profile name, so no
#      secret ever appears in this script or in your shell history:
#
#           xcrun notarytool store-credentials "trellis-notary" \
#             --key ~/private_keys/AuthKey_XXXXXXXX.p8 \
#             --key-id XXXXXXXXXX \
#             --issuer xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#
#      An App Store Connect API key is preferred over --apple-id and an
#      app-specific password: a key is revocable on its own, and revoking it
#      does not mean changing the password on the Apple Account that also holds
#      your email.
#
#   3. Both values exported, ideally in ~/.zshrc:
#
#           export TRELLIS_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#           export TRELLIS_NOTARY_PROFILE="trellis-notary"
#
# ── Why it is shaped this way ────────────────────────────────────────────────
#
# Notarization requires the HARDENED RUNTIME, and the hardened runtime breaks
# CPython unless it is handed the entitlements in tools/entitlements.plist.
# That file explains each one. An app that notarizes cleanly and then refuses
# to launch is almost always a missing entitlement, not a signing failure.
#
# Signing is BOTTOM-UP and does not use --deep. Apple deprecated --deep and
# says outright not to use it for distribution: it applies one set of
# entitlements to everything it touches and silently skips code it does not
# recognise as code. A PyInstaller bundle is several hundred Mach-O files, so
# "silently skipped" is a real outcome, and it surfaces as a notarization
# rejection listing a file you have never heard of.
#
# FRAMEWORKS ARE SIGNED AS BUNDLES, NOT AS FILES. This bundle ships a real
# Python.framework carrying python.org's own signature. Signing the Mach-O
# inside it (Versions/3.13/Python) as a loose file breaks the framework's
# seal — its _CodeSignature/CodeResources still describes the old contents —
# and `codesign --verify --deep` then rejects the app before Apple ever sees
# it. The fix is to sign the framework's VERSION DIRECTORY, which re-seals the
# whole thing. So framework contents are excluded from the flat loop and the
# frameworks are signed as units afterwards.
#
# Every signature carries --timestamp. Without a secure timestamp the
# signature stops validating the day the certificate expires, which would
# quietly break every copy already on someone's disk.
#
# NOTARIZATION STATUS IS ASSERTED, NOT ASSUMED. `notarytool submit --wait`
# reports `status: Invalid` for a rejected submission and can still exit 0,
# because from its point of view the submission was processed successfully.
# Under `set -e` that means a rejected app sails on into stapling and
# packaging, and the first person to find out is a stranger. So the status is
# parsed and checked, and on anything but "Accepted" the notary log is fetched
# and printed — the rejection reasons are terse AND live behind a second
# command, which is a poor thing to discover at 1am.
set -euo pipefail

cd "$(dirname "$0")/.."

APP_NAME=$(python3 -c 'from version import APP_NAME; print(APP_NAME)')
VERSION=$(python3 -c 'from version import __version__; print(__version__)')
APP="dist/${APP_NAME}.app"
ENTITLEMENTS="tools/entitlements.plist"

BASE="${APP_NAME// /}-${VERSION}-macOS"     # TrellisMusicLibrary-0.1.2-macOS
DMG="dist/${BASE}.dmg"
ZIP="dist/${BASE}.zip"

[[ "$(uname)" == "Darwin" ]] || { echo "Signing only happens on macOS." >&2; exit 1; }
[[ -d "$APP" ]] || { echo "$APP is not there. Run ./tools/build_macos.sh first." >&2; exit 1; }
[[ -f "$ENTITLEMENTS" ]] || { echo "$ENTITLEMENTS is missing." >&2; exit 1; }

IDENTITY="${TRELLIS_SIGN_IDENTITY:-}"
PROFILE="${TRELLIS_NOTARY_PROFILE:-}"
if [[ -z "$IDENTITY" ]]; then
  echo "TRELLIS_SIGN_IDENTITY is not set. See the header of this script." >&2
  echo "Available identities:" >&2
  security find-identity -v -p codesigning >&2 || true
  exit 1
fi

echo "── Signing ${APP_NAME} ${VERSION} ─────────────────────────────"
echo "  identity: ${IDENTITY}"

# ── Helpers ─────────────────────────────────────────────────────────────────

# Submit one artifact to the notary service and REFUSE to continue unless the
# verdict is "Accepted". On anything else, fetch and print the log, which is
# where the actual reason lives.
notarize () {
  local target="$1" label="$2" out id status
  out=$(mktemp)

  echo "  submitting ${label}… (this takes minutes, not seconds)"
  if ! xcrun notarytool submit "$target" \
         --keychain-profile "$PROFILE" --wait --output-format json >"$out"; then
    echo "  ✗ notarytool itself failed on ${label}." >&2
    cat "$out" >&2
    rm -f "$out"
    exit 1
  fi

  # python3 rather than jq, which is not on a stock macOS.
  id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$out")
  status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$out")
  rm -f "$out"

  echo "  ${label}: ${status}  (submission ${id})"

  if [[ "$status" != "Accepted" ]]; then
    echo >&2
    echo "  ✗ ${label} was NOT accepted. Apple's log follows. It names files;" >&2
    echo "    the usual causes are a missing --timestamp, a nested binary that" >&2
    echo "    was skipped, or a framework signed as a loose file." >&2
    echo >&2
    if [[ -n "$id" ]]; then
      xcrun notarytool log "$id" --keychain-profile "$PROFILE" >&2 || \
        echo "    (could not fetch the log; try: xcrun notarytool log $id --keychain-profile $PROFILE)" >&2
    fi
    exit 1
  fi
}

# ── 1. Strip extended attributes ────────────────────────────────────────────
# Finder, Time Machine and the build itself leave xattrs on files inside the
# bundle. codesign refuses to sign a file carrying resource forks, and the
# error names the attribute rather than the fix.
xattr -cr "$APP"

# ── 2. Find what has to be signed ───────────────────────────────────────────
MAIN_EXE="$APP/Contents/MacOS/${APP_NAME}"
[[ -f "$MAIN_EXE" ]] || { echo "No executable at $MAIN_EXE" >&2; exit 1; }

echo "  finding code to sign…"

# Discovery lives in tools/find_signables.py. It is Python rather than a
# find | sort pipeline for one blunt reason: ordering NUL-separated paths by
# depth requires `sort -z`, which is a GNU extension whose presence in BSD sort
# varies by macOS release — and when it is absent the pipeline does not fail,
# it returns nothing, which reads exactly like "this bundle has no libraries."
# The Python version also detects Mach-O by reading four magic bytes instead of
# forking `file` seven hundred times, and can be tested on any machine against
# any bundle without a certificate. See its docstring for the rest.
FRAMEWORK_LIST=$(mktemp)
MACHO_LIST=$(mktemp)
python3 tools/find_signables.py "$APP" frameworks > "$FRAMEWORK_LIST"
python3 tools/find_signables.py "$APP" binaries   > "$MACHO_LIST"

FW_COUNT=$(tr -cd '\0' < "$FRAMEWORK_LIST" | wc -c | tr -d ' ')
COUNT=$(tr -cd '\0' < "$MACHO_LIST" | wc -c | tr -d ' ')

# A low number means find_signables.py is broken, not that a 300 MB PyInstaller
# bundle contains no libraries. Signing only the outer bundle would look like
# success right up until the notary service rejects it. As of 0.1.3 the real
# figure is 344 binaries and 1 framework; the floor is set well below that so
# adding a dependency does not trip it.
if [[ "$COUNT" -lt 50 ]]; then
  echo "  ✗ Only ${COUNT} nested binaries found. A PyInstaller bundle has" >&2
  echo "    hundreds. Something is wrong with the search, not the bundle." >&2
  rm -f "$MACHO_LIST" "$FRAMEWORK_LIST"
  exit 1
fi

echo "  ${COUNT} nested binaries, ${FW_COUNT} framework bundle(s)"

# ── 3. Sign, innermost outwards ─────────────────────────────────────────────
#
# NOTE the entitlements are NOT applied to the nested code. An entitlement is a
# property of a running PROCESS, and none of these files is one — the
# exceptions Python needs belong to the executable that hosts it. Stamping them
# onto three hundred libraries grants nothing extra and gives the notary
# service three hundred more places to ask why.
#
# --force because PyInstaller ships dylibs that already carry a vendor's ad-hoc
# signature, and an existing signature is otherwise a hard error.

echo "  signing ${COUNT} nested binaries…"
while IFS= read -r -d '' f; do
  codesign --force --timestamp --options runtime \
           --sign "$IDENTITY" "$f" >/dev/null
done < "$MACHO_LIST"
rm -f "$MACHO_LIST"

if [[ "$FW_COUNT" -gt 0 ]]; then
  echo "  signing ${FW_COUNT} framework bundle(s)…"
  while IFS= read -r -d '' fw; do
    echo "    ${fw#"$APP"/}"
    codesign --force --timestamp --options runtime \
             --sign "$IDENTITY" "$fw" >/dev/null
  done < "$FRAMEWORK_LIST"
fi
rm -f "$FRAMEWORK_LIST"

# These two carry the entitlements, because these two are what actually runs.
echo "  signing the executable…"
codesign --force --timestamp --options runtime \
         --entitlements "$ENTITLEMENTS" \
         --sign "$IDENTITY" "$MAIN_EXE"

echo "  signing the bundle…"
codesign --force --timestamp --options runtime \
         --entitlements "$ENTITLEMENTS" \
         --sign "$IDENTITY" "$APP"

# --deep is wrong for SIGNING and right for VERIFYING: here it means "check
# every nested piece too", which is exactly the question being asked. If the
# framework were signed as a loose file, this is where it would blow up.
echo "  verifying the signature…"
codesign --verify --deep --strict --verbose=2 "$APP"

# ── 3b. Prove the SIGNED app still starts ───────────────────────────────────
# The hardened runtime is applied at signing time, so the app that was tested
# during the build is not the app being shipped. This is where a missing
# entitlement surfaces, and it surfaces as an instant silent death — which
# from Finder looks identical to nothing happening.
#
# Catching it here rather than after notarization matters: a notarization
# round trip is minutes, and there is no sense spending them on a bundle that
# cannot open.
echo
echo "── Startup self-test, signed ──────────────────────────────────"
SELFTEST_DIR=$(mktemp -d)
if TRELLIS_DATA_DIR="$SELFTEST_DIR" TRELLIS_SELFTEST=1 \
     "$MAIN_EXE" 2>&1 | sed 's/^/  /'; then
  echo "  ✓ the signed app starts"
else
  echo
  echo "  ✗ THE SIGNED APP DOES NOT START, and the unsigned one did." >&2
  echo "    That is the hardened runtime, not your code. Read the note in" >&2
  echo "    tools/entitlements.plist and add ONE more entitlement, rebuild," >&2
  echo "    and test again. Adding two at once teaches you nothing." >&2
  rm -rf "$SELFTEST_DIR"
  exit 1
fi
rm -rf "$SELFTEST_DIR"

# ── 4. Notarize the app ─────────────────────────────────────────────────────
# ditto, not zip. The notary service reads a zip made by ditto correctly and
# has historically choked on ones made by other tools, which shows up as a
# rejection with nothing useful in it.
if [[ -z "$PROFILE" ]]; then
  echo
  echo "  TRELLIS_NOTARY_PROFILE is not set, so the app is SIGNED BUT NOT"
  echo "  NOTARIZED. It will still be blocked on a machine that did not build"
  echo "  it. Set the profile and run this again."
  exit 0
fi

echo
echo "── Notarizing the app ─────────────────────────────────────────"
NOTARIZE_DIR=$(mktemp -d)
ditto -c -k --keepParent "$APP" "$NOTARIZE_DIR/app.zip"
notarize "$NOTARIZE_DIR/app.zip" "the app"
rm -rf "$NOTARIZE_DIR"

# Stapling writes the notarization ticket INTO the bundle, so a Mac with no
# network still sees an approved app. Without it, a first launch offline is a
# refusal.
echo "  stapling…"
xcrun stapler staple "$APP"

# ── 5. Package ──────────────────────────────────────────────────────────────
# Two artifacts on purpose. The DMG is the front door: it mounts to a window
# holding the app and an Applications alias, so "drag it there" is the whole
# instruction. The zip is for people who would rather not mount anything, and
# it is made FROM the stapled app, so it needs no notarization of its own.
echo
echo "── Packaging ──────────────────────────────────────────────────"
rm -f "$DMG" "$ZIP"

STAGE_ROOT=$(mktemp -d)
STAGE="$STAGE_ROOT/$APP_NAME"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/${APP_NAME}.app"
ln -s /Applications "$STAGE/Applications"

hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" \
               -ov -format UDZO -quiet "$DMG"
rm -rf "$STAGE_ROOT"

# A DMG is itself code as far as Gatekeeper is concerned, so it gets its own
# signature and its own notarization pass. Skipping this means the download is
# flagged even though the app inside it is clean. No hardened runtime here —
# a disk image is not a process.
echo
echo "── Notarizing the disk image ──────────────────────────────────"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"
notarize "$DMG" "the DMG"
echo "  stapling…"
xcrun stapler staple "$DMG"

ditto -c -k --keepParent "$APP" "$ZIP"

# ── 6. Prove it ─────────────────────────────────────────────────────────────
# The check that matters is not "did codesign succeed" but "would Gatekeeper
# let a stranger open this". spctl is that question, asked directly — and it
# is asked of BOTH artifacts, because they are assessed by different rules:
# an app is assessed as executable code, a disk image as something being
# opened, against its primary signature.
echo
echo "── Checks ─────────────────────────────────────────────────────"
spctl --assess --type execute --verbose=4 "$APP" 2>&1 | sed 's/^/  /'
spctl --assess --type open --context context:primary-signature \
      --verbose=4 "$DMG" 2>&1 | sed 's/^/  /'
xcrun stapler validate "$APP" 2>&1 | sed 's/^/  /'
xcrun stapler validate "$DMG" 2>&1 | sed 's/^/  /'

echo
ls -lh "$DMG" "$ZIP" | sed 's/^/  /'
echo
echo "── Done ───────────────────────────────────────────────────────"
echo "  $DMG"
echo "  $ZIP"
echo
echo "  Both are signed, notarized and stapled. They open on a Mac that has"
echo "  never seen them, with no right-click and no xattr command. When you"
echo "  publish these, update the Gatekeeper section of README.md and the"
echo "  download page on the website, which both still describe the old"
echo "  unsigned behaviour."
