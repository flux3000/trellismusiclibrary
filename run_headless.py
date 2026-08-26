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
from version import APP_NAME


def main():
    app = create_app()

    # Loud about identity. With two nodes running, the single most common
    # confusion is not knowing which one a terminal belongs to — so say it
    # before serving a single request.
    print("─" * 60, flush=True)
    print(f"  {APP_NAME} (headless)", flush=True)
    # SHARE_NODE_NAME is None when unset — the real name is derived from the
    # owner at request time (share._node_identity), so say that rather than
    # printing "None" in the identity banner.
    print(f"  node        : {Config.SHARE_NODE_NAME or '(derived from owner)'}", flush=True)
    print(f"  listening   : http://{Config.HOST}:{Config.PORT}", flush=True)
    print(f"  database    : {Config.DB_PATH}", flush=True)
    print(f"  share addr  : {Config.SHARE_BASE_URL or '(unset — invites carry no address)'}", flush=True)
    if Config.DEV_MODE:
        print(f"  DEV_MODE    : ON — cookie auth is bypassed for the first admin", flush=True)
    # Both nodes of the rig run the same checkout, so the banner is the only
    # thing that says which process you are looking at. SERVER_MODE changes
    # what this process SERVES (share door only, no frontend), so it earns a
    # line of its own rather than being inferred from the absence of one.
    if Config.SERVER_MODE:
        print(f"  SERVER_MODE : ON — share door only; no frontend, no admin API", flush=True)
    print("─" * 60, flush=True)

    _serve(app)


def _serve(app):
    """
    Serve the app — with waitress when it is available, and ONLY with waitress
    when this process is internet-facing.

    Flask ships a small server so you can run an app while writing it, and its
    own documentation says not to use it in production. The objection is not
    performance: it will accept connections without limit, wait on a client that
    opens one and then dawdles, and read a request body of any size someone
    cares to send. On a LAN with one user none of that matters. Behind a tunnel,
    each is a way for one bored stranger to make the machine stop answering.

    waitress is the pure-Python option — no compiler, no C extensions, installs
    on macOS and Windows without ceremony, which matters because this app is
    heading for a packaged cross-platform build.

    The fallback is deliberate and one-sided. The two-node dev rig runs this
    file too, and a missing package there should be a warning, not a wall. In
    SERVER_MODE it is a wall: refusing to boot is the same choice
    _validate_server_mode already makes, and for the same reason — a process
    that is about to be publicly reachable should fail loudly rather than come
    up quietly wrong.
    """
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        if Config.SERVER_MODE:
            sys.exit(
                "Refusing to boot: SERVER_MODE is on but waitress is not "
                "installed, and this process is meant to be internet-facing.\n"
                "Flask's built-in server is for development only.\n"
                "  pip3 install waitress"
            )
        print("  ⚠ waitress not installed — falling back to the Flask dev "
              "server. Fine locally, never for an exposed node.")
        app.run(
            host         = Config.HOST,
            port         = Config.PORT,
            debug        = False,
            use_reloader = False,
            # The dev server otherwise handles ONE request at a time, and under
            # the rig that matters doubly — node A proxying to node B means one
            # of A's workers is blocked waiting on B for the whole call.
            threaded     = True,
        )
        return

    waitress_serve(
        app,
        host    = Config.HOST,
        port    = Config.PORT,
        # A FLAC stream holds its thread for the length of the range request,
        # so threads here is "how many people can be listening at once", not a
        # throughput knob. Four (the default) is too few for a household.
        threads = 8,
        # Don't advertise the server software to the internet.
        ident   = None,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        sys.exit(0)
