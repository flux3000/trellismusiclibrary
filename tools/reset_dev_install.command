#!/bin/bash
#
# tools/reset_dev_install.command - reset a Trellis DEV install to first-run
# state.
#
# Double-click from Finder, or run ./tools/reset_dev_install.command.
#
# What it leaves alone: the repo itself (source, .git, .venv), any packaged
# build, and the library folder(s) chosen at first run (Library, Download,
# Backlog, Workshop, or an adopted "bring your own" folder); none of that is
# touched, no matter where it lives on disk.
#
# What it resets: the database, its WAL/SHM files, the transcode cache, the
# images store, avatars, the peer/AI-key Keychain entries, and the first-run
# marker: whatever of that state config.py/run.py says lives in DATA_DIR
# (honoring TRELLIS_DATA_DIR), PLUS anything of the same kind found sitting
# in the repo itself (a plain dev checkout is DATA_DIR unless overridden, and
# db/trellis.db here is sometimes a symlink out to Application Support, see
# config.py's DATA_DIR/DB_PATH/AVATAR_DIR comments). Nothing found is deleted
# outright; everything moves to ~/.Trash with a timestamp suffix, so it can
# still be recovered by hand afterward.
#
# Flags:
#   --dry-run     print every action, do nothing
#   --yes         skip the confirmation prompt
#   --no-launch   reset only, don't start Trellis afterward
#
# Usage:
#   ./tools/reset_dev_install.command [--dry-run] [--yes] [--no-launch]

set -euo pipefail

DRY_RUN=false
ASSUME_YES=false
NO_LAUNCH=false
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=true ;;
    --yes)       ASSUME_YES=true ;;
    --no-launch) NO_LAUNCH=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Repo path is derived from this script's own location, never hardcoded.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

STAMP="$(date +%Y%m%d-%H%M%S)"
TRASH_DIR="$HOME/.Trash/Trellis Music Library-$STAMP"
APP_NAME="Trellis Music Library"

# TRELLIS_DATA_DIR always wins, exactly as config.py reads it. Unset, a
# dev checkout is DATA_DIR itself (config._default_data_dir() returns
# BASE_DIR when not an installed/frozen app), so the historical default
# guess below is only ever used to find state a manual setup (a symlinked
# db/trellis.db, most commonly) has pointed elsewhere, on top of whatever
# this repo holds directly.
DATA_DIR_GUESS="${TRELLIS_DATA_DIR:-$HOME/Library/Application Support/$APP_NAME}"

note()  { echo "-- $*"; }

# Moves one file/dir into TRASH_DIR, preserving its path relative to $2
# (REPO or DATA_DIR_GUESS) so the trashed copy is recognizable. No-op if the
# source doesn't exist. Never fails the whole script on one bad move.
trash_item() {
  local src="$1" rel="$2"
  [ -e "$src" ] || [ -L "$src" ] || return 0
  local dest="$TRASH_DIR/$rel"
  if $DRY_RUN; then
    echo "[dry-run] trash: $src -> $dest"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  mv "$src" "$dest" && echo "trashed: $src" || echo "!! failed to trash: $src" >&2
}

echo "=== Trellis dev install reset ==="
echo "Repo: $REPO"

# ── 1. Quit Trellis if running ───────────────────────────────────────────────
note "Step 1: stopping any running Trellis process"

quit_matching() {
  local pattern="$1" use_exact="$2"
  local pids
  if [ "$use_exact" = "exact" ]; then
    pids=$(pgrep -x "$pattern" 2>/dev/null || true)
  else
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
  fi
  for pid in $pids; do
    # Belt and braces: confirm the command line really names the repo or the
    # app, so this can never kill an unrelated process pgrep matched loosely.
    local cmd
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
    case "$cmd" in
      *"$REPO"*|*"$APP_NAME"*) ;;
      *) continue ;;
    esac
    if $DRY_RUN; then
      echo "[dry-run] would stop pid $pid ($cmd)"
      continue
    fi
    echo "stopping pid $pid ($cmd)"
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "  still running, sending SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
}

# Dev run: `python3 run.py` launched from this repo.
quit_matching "$REPO/run.py" loose
# Packaged app, if one is also installed on this machine.
quit_matching "$APP_NAME" exact

# ── Confirmation ──────────────────────────────────────────────────────────────
if ! $DRY_RUN && ! $ASSUME_YES; then
  echo ""
  echo "This will move the following to $HOME/.Trash (nothing is deleted):"
  echo "  - the Trellis database, its WAL/SHM files, and the first-run marker"
  echo "  - the transcode cache, the images store, and avatars"
  echo "  - matching peer/AI-key entries in the Keychain (removed, not trashed)"
  echo "The repo, the venv, and your library folder(s) are left as they are."
  read -r -p "Continue? [y/N] " ans
  case "$ans" in
    y|Y) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

# ── 2. Reset app state (database, cache, images, avatars, marker) ───────────
note "Step 2: resetting app state"

# db/trellis.db is sometimes a real file (plain dev checkout) and sometimes a
# symlink Ryan pointed at DATA_DIR_GUESS so a source run and a packaged
# install share one database (see config.py's DB_PATH/AVATAR_DIR comments).
# Resolve it to find the REAL file to trash, but leave the symlink itself in
# place: run.py recreates the target directory on its next launch, so the
# link keeps working exactly as it did before.
REPO_DB_LINK="$REPO/db/trellis.db"
REAL_DB=""
if [ -L "$REPO_DB_LINK" ]; then
  REAL_DB="$(cd "$(dirname "$REPO_DB_LINK")" && readlink -f "$REPO_DB_LINK" 2>/dev/null || python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$REPO_DB_LINK")"
  note "  db/trellis.db is a symlink -> $REAL_DB (link left in place)"
elif [ -e "$REPO_DB_LINK" ]; then
  REAL_DB="$REPO_DB_LINK"
elif [ -e "$DATA_DIR_GUESS/db/trellis.db" ]; then
  REAL_DB="$DATA_DIR_GUESS/db/trellis.db"
fi

# ── Keychain entries, read from the DB BEFORE it's trashed ──────────────────
# Exact service/account only (app/utils/prefs.py): service "trellis",
# accounts "anthropic_api_key:<user id>" and "remote_token:<remote_node id>".
# No fuzzy matching: never `security find-generic-password` by substring.
note "Step 3: removing Keychain entries (service \"trellis\")"
if [ -n "$REAL_DB" ] && [ -f "$REAL_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  USER_IDS=$(sqlite3 -readonly "$REAL_DB" "SELECT id FROM user;" 2>/dev/null || true)
  NODE_IDS=$(sqlite3 -readonly "$REAL_DB" "SELECT id FROM remote_node;" 2>/dev/null || true)
  for uid in $USER_IDS; do
    account="anthropic_api_key:$uid"
    if $DRY_RUN; then
      echo "[dry-run] security delete-generic-password -s trellis -a \"$account\""
    else
      security delete-generic-password -s "trellis" -a "$account" >/dev/null 2>&1 \
        && echo "  removed Keychain entry: $account" \
        || echo "  (no Keychain entry for $account)"
    fi
  done
  for nid in $NODE_IDS; do
    account="remote_token:$nid"
    if $DRY_RUN; then
      echo "[dry-run] security delete-generic-password -s trellis -a \"$account\""
    else
      security delete-generic-password -s "trellis" -a "$account" >/dev/null 2>&1 \
        && echo "  removed Keychain entry: $account" \
        || echo "  (no Keychain entry for $account)"
    fi
  done
else
  note "  no readable database found, skipping Keychain cleanup"
fi

# ── Trash app-state paths ────────────────────────────────────────────────────
# Primary target, as asked: DATA_DIR_GUESS (Application Support, or
# TRELLIS_DATA_DIR when set) holds the database + avatars for this checkout's
# shared-db setup, and holds everything (cache, images, marker too) whenever
# DATA_DIR really is that folder rather than the repo.
if [ -d "$DATA_DIR_GUESS" ] && [ "$DATA_DIR_GUESS" != "$REPO" ]; then
  trash_item "$DATA_DIR_GUESS" "$(basename "$DATA_DIR_GUESS")"
else
  note "  $DATA_DIR_GUESS not found, nothing to move from there"
fi

# Anything of the same kind still sitting in the repo (the common case for a
# plain `python3 run.py` checkout, where DATA_DIR defaults to the repo
# itself) gets trashed too, and called out below since it lives outside
# DATA_DIR_GUESS.
if [ -n "$REAL_DB" ] && [ ! -L "$REPO_DB_LINK" ]; then
  trash_item "$REPO/db/trellis.db" "db/trellis.db"
  trash_item "$REPO/db/trellis.db-wal" "db/trellis.db-wal"
  trash_item "$REPO/db/trellis.db-shm" "db/trellis.db-shm"
fi
trash_item "$REPO/db/window_state.json" "db/window_state.json"
trash_item "$REPO/cache/transcodes" "cache/transcodes"
trash_item "$REPO/images" "images"
# The first-run marker: TRELLIS_ROOT_MARKER = DATA_DIR/trellis_root.json.
# For a plain dev checkout that IS the repo root; found there in this
# checkout even though the database symlink points elsewhere.
trash_item "$REPO/trellis_root.json" "trellis_root.json"

# ── 4. Next first run ────────────────────────────────────────────────────────
note "Step 4: library root next first run will see"
LIBRARY_NOTE="first run will ask (no folder chosen yet)"
MARKER_COPY="$TRASH_DIR/trellis_root.json"
$DRY_RUN && MARKER_COPY="$REPO/trellis_root.json"
if [ -f "$MARKER_COPY" ] && command -v python3 >/dev/null 2>&1; then
  ROOT=$(python3 - "$MARKER_COPY" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
root = data.get("library_root") or data.get("trellis_root")
if root:
    print(root)
PY
)
  [ -n "$ROOT" ] && LIBRARY_NOTE="$ROOT (recovered from the trashed marker; not touched)"
fi
echo "  $LIBRARY_NOTE"

echo ""
echo "Done."

# ── 5. Relaunch ──────────────────────────────────────────────────────────
if $NO_LAUNCH || $DRY_RUN; then
  exit 0
fi

echo ""
echo "Starting Trellis (DEV_MODE=true python3 run.py)..."
cd "$REPO"
# shellcheck disable=SC1091
source .venv/bin/activate
DEV_MODE=true python3 run.py
