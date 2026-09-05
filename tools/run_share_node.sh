#!/usr/bin/env bash
#
# tools/run_share_node.sh — start the public share node behind the Cloudflare
# Tunnel.
#
#   ./tools/run_share_node.sh
#
# This existed only in Ryan's shell history until 2026-09-04. That is a poor
# home for load-bearing infrastructure: if it is lost, share.trellismusiclibrary.com
# still resolves and the tunnel still answers, so the failure presents as a
# tunnel problem rather than as "nothing is listening on 5760." Committing it
# makes the thing reproducible and reviewable.
#
# ── What this process is ─────────────────────────────────────────────────────
#
# SERVER_MODE makes create_app() register ONLY the share blueprint and return
# early, so the front-door routes 404 because they were never constructed —
# not because something filters them. It serves the real library: the share
# node deliberately sets no database path, because sharing means sharing what
# you actually have.
#
# It runs in the FOREGROUND on purpose. Background it yourself if you want to;
# a launcher that daemonizes silently is a launcher whose failures you never
# see.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${TRELLIS_PORT:-5760}"

# ── Use the project venv when there is one ──────────────────────────────────
# The old hand-typed command called bare `python3`, which works from an
# activated shell and fails confusingly from anywhere else — the app's
# dependencies simply are not there. Prefer the venv explicitly so it does not
# matter how the shell was set up.
if [[ -x .venv/bin/python3 ]]; then
  PY=.venv/bin/python3
else
  PY=python3
  echo "  ⚠ No .venv found — using system python3. If this fails on an import,"
  echo "    that is why."
fi

# ── Refuse to start a second one ────────────────────────────────────────────
# Two share nodes on one port is not a thing that half-works: the second fails
# to bind, and if you missed the error you are left believing the node you just
# "restarted" is the one serving traffic.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ Something is already listening on port ${PORT}:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  echo >&2
  echo "  If that is an old share node, stop it first." >&2
  exit 1
fi

# ── The tunnel is a separate process ────────────────────────────────────────
# A warning, not a failure: serving 5760 locally is legitimate on its own, and
# this script has no business deciding the tunnel must be up. But "the node is
# running and nobody can reach it" is worth one line of explanation.
if ! pgrep -x cloudflared >/dev/null 2>&1; then
  echo "  ⚠ cloudflared does not appear to be running, so share.trellismusiclibrary.com"
  echo "    has nothing behind it. This node will still serve 127.0.0.1:${PORT}."
fi

echo "── Trellis share node ─────────────────────────────────────────"
echo "  port    : ${PORT}"
echo "  python  : ${PY}"
echo "  library : the real one (no TRELLIS_DB_PATH set, deliberately)"
echo

# ── Environment ─────────────────────────────────────────────────────────────
#
# SECRET_KEY is regenerated per launch unless you export one. That invalidates
# any browser session across a restart, which costs nothing here: the share
# door authenticates peers with a Bearer token, not a cookie, and it serves no
# SPA. The upside is that no long-lived secret sits in this file or in the
# repo. Export SECRET_KEY yourself if you ever need sessions to survive.
#
# TRELLIS_COOKIE_NAME is NOT optional and must differ from the desktop app's.
# Cookies are scoped by HOST, not by port, so a share node using the default
# "session" name on the same machine would overwrite the desktop app's login
# and vice versa.
#
# TRUSTED_CLIENT_IP_HEADER exists because cloudflared connects from 127.0.0.1.
# Without it every visitor on earth shares one rate-limit bucket. config.py
# only honours it when the immediate peer is loopback, so setting it here
# cannot be abused by a direct caller.
#
# SHARE_BASE_URL is passed through if you export it and otherwise left unset,
# matching the old command exactly. ⚠ Note it would not fix invites anyway:
# the DESKTOP app mints those, so it is the desktop side that needs an address
# to put in them.
exec env \
  SERVER_MODE=true \
  SECRET_KEY="${SECRET_KEY:-$("$PY" -c 'import secrets; print(secrets.token_hex(32))')}" \
  TRELLIS_COOKIE_NAME="${TRELLIS_COOKIE_NAME:-trellis_share}" \
  TRELLIS_PORT="$PORT" \
  TRUSTED_CLIENT_IP_HEADER="${TRUSTED_CLIENT_IP_HEADER:-CF-Connecting-IP}" \
  "$PY" run_headless.py
