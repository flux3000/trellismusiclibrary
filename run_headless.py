"""
run_headless.py — Flux Audio entry point WITHOUT PyWebView.

Why this exists
---------------
`run.py` starts Flask in a background thread and opens a native window. That is
right for the Mac app and wrong for two situations:

1. **The two-node peer-sharing dev rig** (2026-08-08). Developing the consumer
   side needs a second Flux node to enroll into and browse. A second PyWebView
   window is pure noise — node B is a server, not something to look at.
2. **Server exposure.** Running behind a Cloudflare Tunnel means running
   headless. This has been on the pre-live blocking list since July.

Usage
-----
    # Node B for the dev rig — own DB, own port, own identity
    FLUX_DB_PATH=/tmp/fluxnode_b.db \
    FLUX_PORT=5758 \
    SHARE_BASE_URL=http://127.0.0.1:5758 \
    SHARE_NODE_NAME="Node B" \
    SHARE_OWNER_NAME="Test Peer" \
    DEV_MODE=true \
    python3 run_headless.py

Everything is driven by config.py, so this file stays deliberately thin — it is
an entry point, not a second place where configuration lives.
"""

import sys

from app import create_app
from config import Config


def main():
    app = create_app()

    # Loud about identity. With two nodes running, the single most common
    # confusion is not knowing which one a terminal belongs to — so say it
    # before serving a single request.
    print("─" * 60)
    print(f"  Flux Audio (headless)")
    # SHARE_NODE_NAME is None when unset — the real name is derived from the
    # owner at request time (share._node_identity), so say that rather than
    # printing "None" in the identity banner.
    print(f"  node        : {Config.SHARE_NODE_NAME or '(derived from owner)'}")
    print(f"  listening   : http://{Config.HOST}:{Config.PORT}")
    print(f"  database    : {Config.DB_PATH}")
    print(f"  share addr  : {Config.SHARE_BASE_URL or '(unset — invites carry no address)'}")
    if Config.DEV_MODE:
        print(f"  DEV_MODE    : ON — cookie auth is bypassed for the first admin")
    print("─" * 60)

    # threaded=True for the same reason run.py sets it: the dev server otherwise
    # serves one request at a time, and a slow request stalls everything. Under
    # the rig that matters doubly — node A proxying to node B means one of A's
    # workers is blocked waiting on B for the whole call.
    app.run(
        host         = Config.HOST,
        port         = Config.PORT,
        debug        = False,
        use_reloader = False,
        threaded     = True,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
