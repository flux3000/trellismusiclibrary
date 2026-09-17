"""
probe_peer_door.py — Walk node B's peer door exactly as a peer's app will.

The consumer side (milestone 2 step 5) doesn't exist yet, so there is no UI
that can reach these endpoints. This script stands in for it: it enrolls with
an invite code, then walks the whole peer experience and prints what a peer
sees — followed by a handful of things a peer must NOT be able to see.

Stdlib only (urllib, not requests) so a dev tool doesn't add a dependency.

Usage
-----
    # First run — spends the invite, saves the token
    python3 scripts/probe_peer_door.py --enroll <CODE>

    # Later runs — reuses the saved token
    python3 scripts/probe_peer_door.py

    # Against something other than node B
    python3 scripts/probe_peer_door.py --base http://127.0.0.1:5758

NOTE: enrolling CONSUMES the invite (single use, by design). To get another,
re-run `python3 scripts/setup_node_b.py --force`, which rebuilds node B's
database and mints a fresh one.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).parent.parent
TOKEN_FILE = REPO / "tmp" / "node_b_peer_token.txt"     # tmp/ is gitignored
DEFAULT_BASE = "http://127.0.0.1:5758"


# ── HTTP ──────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do NOT follow redirects.

    This bit me on the first run (2026-08-08). The local API sets
    `login_manager.login_view` but no `unauthorized_handler`, so an
    unauthenticated call to /api/artists/1 answers 302 → the login page.
    urllib follows that on GET, lands on the SPA shell, and reports 200 with
    HTML — which reads exactly like "the peer token got into the admin API."

    A probe that turns a redirect into a false security failure is worse than
    no probe. The status code we want to see is the one the endpoint actually
    returned, so redirects stop here.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _call(base, path, token=None, method="GET", body=None):
    """Returns (status, parsed_json_or_text). Never raises on HTTP error —
    the error codes ARE the thing being tested."""
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=15) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.status, json.loads(raw)
            return resp.status, f"<{len(raw)} bytes of {ctype or 'unknown type'}>"
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")[:200]
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {url} — is node B running?  ({e.reason})")


def _ok(status):
    return "✓" if 200 <= status < 300 else "✗"


# ── Probe ─────────────────────────────────────────────────────────────────────

def enroll(base, code):
    status, body = _call(base, "/api/share/enroll", method="POST",
                         body={"invite_code": code, "device_label": "probe script"})
    if status != 201:
        sys.exit(f"Enroll failed ({status}): {body}")
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(body["token"])
    print(f"✓ Enrolled with {body['node_name']} (owner: {body['owner_name']})")
    print(f"  I am known there as: {body['peer_name']}")
    print(f"  Token saved to {TOKEN_FILE}")
    return body["token"]


def probe(base, token):
    print()
    print("═" * 70)
    print("  WHAT THE PEER SEES")
    print("═" * 70)

    status, me = _call(base, "/api/share/me", token)
    print(f"\n{_ok(status)} /me  →  {me}")

    status, collections = _call(base, "/api/share/collections", token)
    print(f"\n{_ok(status)} /collections  →  {len(collections)} granted")
    for c in collections:
        print(f"    · {c['name']}  ({c['recording_count']} recordings)")
    if not collections:
        sys.exit("No granted collections — nothing further to probe.")

    col_id = collections[0]["id"]
    status, detail = _call(base, f"/api/share/collections/{col_id}", token)
    recs = detail.get("recordings", [])
    print(f"\n{_ok(status)} /collections/{col_id}  →  {len(recs)} recordings")
    for r in recs[:5]:
        card = "card fields ✓" if "genre_color" in r else "card fields MISSING"
        print(f"    · [{r['id']}] {r.get('date')} — {r.get('artist')} "
              f"@ {r.get('venue')}   ({card})")
    if len(recs) > 5:
        print(f"    … and {len(recs) - 5} more")
    if not recs:
        sys.exit("Granted collection is empty — put something in it and re-run.")

    # ── Every act and venue in the granted collection ─────────────────────────
    # Walk them ALL, not just the first. A collection of six Béla recordings
    # spans six DIFFERENT Artist rows (Béla Fleck, Bela Fleck & Jerry
    # Douglas, Acoustic All-Stars…), because a billed act is its own entity.
    # Probing only the first one makes it look as though a single artist is
    # reachable, which is how this section came to exist.
    print("\n── Every artist in the granted collection ──")
    artist_ids = sorted({r["artist_id"] for r in recs if r.get("artist_id")})
    for pid in artist_ids:
        pstatus, pbody = _call(base, f"/api/share/artists/{pid}", token)
        rstatus, rbody = _call(base, f"/api/share/artists/{pid}/recordings", token)
        shown = sum(len(p.get("recordings", [])) for p in rbody) if rstatus == 200 else 0
        name = pbody.get("name") if pstatus == 200 else pbody
        print(f"  {_ok(pstatus)} [{pid:>4}] {str(name)[:44]:<44} "
              f"{shown} recording(s) visible")
    print(f"  → {len(artist_ids)} distinct acts, all reachable if every row is ✓")

    print("\n── Every venue in the granted collection ──")
    venue_names = {}
    for r in recs:
        status_r, rec_full = _call(base, f"/api/share/recordings/{r['id']}", token)
        if status_r == 200 and rec_full.get("venue_id"):
            venue_names[rec_full["venue_id"]] = rec_full.get("venue")
    for vid, vname in sorted(venue_names.items()):
        vstatus, vbody = _call(base, f"/api/share/venues/{vid}", token)
        if vstatus == 200:
            print(f"  {_ok(vstatus)} [{vid:>4}] {str(vname)[:44]:<44} "
                  f"{vbody.get('recording_count')} visible / "
                  f"{vbody.get('performance_count')} performances")
        else:
            print(f"  {_ok(vstatus)} [{vid:>4}] {str(vname)[:44]:<44} {vstatus}")

    rec_id = recs[0]["id"]
    status, rec = _call(base, f"/api/share/recordings/{rec_id}", token)
    print(f"\n{_ok(status)} /recordings/{rec_id}  →  {rec.get('artist')}, "
          f"{len(rec.get('tracks', []))} tracks")
    print(f"    nav ids: artist_id={rec.get('artist_id')} "
          f"venue_id={rec.get('venue_id')}")
    if rec.get("artist_id") is None:
        print("    ⚠ no artist_id — the artist page is unreachable")

    artist_id = rec.get("artist_id")
    venue_id = rec.get("venue_id")

    if artist_id:
        status, perf = _call(base, f"/api/share/artists/{artist_id}", token)
        print(f"\n── One artist in detail ──")
        print(f"{_ok(status)} /artists/{artist_id}  →  {perf.get('name')}")
        print(f"    bio:      {'yes' if perf.get('bio') else 'none'}")
        print(f"    dossier:  {'yes' if perf.get('dossier') else 'none'}")
        print(f"    genre:    {(perf.get('genre') or {}).get('name') or 'none'}")
        print(f"    members:  {len(perf.get('members', []))}")
        print(f"    photos:   {len(perf.get('images', []))}")
        for img in perf.get("images", [])[:1]:
            istatus, ibody = _call(base, img["url"], token)
            print(f"    {_ok(istatus)} photo fetch {img['url']} → {ibody}")

        status, prs = _call(base, f"/api/share/artists/{artist_id}/recordings", token)
        shown = sum(len(p.get("recordings", [])) for p in prs)
        print(f"\n{_ok(status)} /artists/{artist_id}/recordings  →  "
              f"{len(prs)} performances, {shown} recordings VISIBLE")

        for m in perf.get("members", [])[:1]:
            status, art = _call(base, f"/api/share/musicians/{m['id']}", token)
            print(f"\n{_ok(status)} /musicians/{m['id']}  →  {art.get('name') if status==200 else art}")
            if status == 200:
                print(f"    acts visible: {[p['name'] for p in art.get('artists', [])]}")

    if venue_id:
        status, ven = _call(base, f"/api/share/venues/{venue_id}", token)
        print(f"\n{_ok(status)} /venues/{venue_id}  →  {ven.get('name')}")
        print(f"    performance_count: {ven.get('performance_count')}  "
              f"recording_count: {ven.get('recording_count')}")
        print(f"    (both MUST be counts of what the peer can see, not the "
              f"venue's whole history)")

    status, genres = _call(base, "/api/share/genres/", token)
    print(f"\n{_ok(status)} /genres/  →  {len(genres)} genres in the visible set")
    for g in genres[:8]:
        print(f"    · {g['name']}: {g['artist_count']} artists, "
              f"{g['recording_count']} recordings")

    # ── Negative space ────────────────────────────────────────────────────────
    print()
    print("═" * 70)
    print("  WHAT THE PEER MUST NOT SEE  (all of these should fail)")
    print("═" * 70)

    # As of 2026-08-08 the local door answers JSON 401 for /api/* paths
    # (login_manager.unauthorized_handler). Before that it 302'd to a POST-only
    # login route, fell through to the SPA catch-all, and returned 200 + HTML —
    # which this probe read as a breach. A 302 here now means the handler
    # didn't fire, so it is no longer accepted as a pass.
    REFUSED = (401, 403)

    checks = [
        ("no token at all",            "/api/share/collections",      None),
        ("a garbage token",            "/api/share/collections",      "xxx"),
        ("the local admin API",        "/api/artists/1",           token),
        ("the raw-FLAC stream route",  "/api/stream/1",               token),
        ("peer administration",        "/api/peers/",                 token),
    ]
    for label, path, tok in checks:
        status, _ = _call(base, path, tok)
        verdict = "✓" if status in REFUSED else f"✗ GOT {status}"
        note = "  ← 302 means unauthorized_handler didn't fire" if status == 302 else ""
        print(f"  {verdict}  {label:<26} {path}  → {status}{note}")

    print()
    print("  Writes (a peer token must have no route to any of these):")
    for method, path in [("PUT", "/api/artists/1"), ("DELETE", "/api/recordings/1"),
                         ("POST", "/api/collections/")]:
        status, _ = _call(base, path, token, method=method)
        verdict = "✓" if status in REFUSED + (404, 405) else f"✗ GOT {status}"
        print(f"  {verdict}  {method:<6} {path}  → {status}")

    print()
    print("Done. A clean run is: every ✓ above, no ✗ anywhere.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--enroll", metavar="CODE", help="invite code (single use)")
    args = ap.parse_args()

    if args.enroll:
        token = enroll(args.base, args.enroll)
    elif TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        print(f"Using saved token from {TOKEN_FILE}")
    else:
        sys.exit("No saved token. Run once with --enroll <CODE> "
                 "(the code setup_node_b.py printed).")

    probe(args.base, token)


if __name__ == "__main__":
    main()
