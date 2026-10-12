"""cleanup_orphans_and_nest_rollins.py — One-time data cleanup.

Two independent operations, both approved by the user:

A) Delete orphaned performances (0 recordings) and their now-dangling parents.
   Failed-ingest / test residue. Deleting a performance can leave a performer
   with 0 performances; if that performer also has 0 recordings anywhere it's
   deleted too, and a canonical left with 0 linked performers is removed.
   Performers/canonicals that still have real recordings via other performances
   are untouched.

B) Nest the Sonny Rollins ensembles under canonical "Sonny Rollins".
   The "Sonny Rollins & Don Cherry Quartet" performer had lost its only
   canonical link (its 1963 recording was showing in no catalog). Link it to
   canonical "Sonny Rollins" and drop the now-redundant standalone canonical.
   (Decoupled model: canonical = grouping node; no same-named performer needed.)

Idempotent-ish: each step checks existence first. Safe to run once.

Run:
    python3 scripts/cleanup_orphans_and_nest_rollins.py --dry-run
    python3 scripts/cleanup_orphans_and_nest_rollins.py
"""

import sys
import sqlite3
from pathlib import Path

DB = Path(__file__).parent.parent / "db" / "fluxaudio.db"

ORPHAN_PERFORMANCES = [18, 20, 25, 26, 27, 32, 37]   # all have 0 recordings

# Performers left with 0 performances after the deletes above, and 0 recordings
# anywhere — safe to remove. (18 fully unlinked; 31 phantom Bill Evans child;
# 24 Bruce Springsteen — zero recordings in the whole library.)
ORPHAN_PERFORMERS = [18, 24, 31]

# Canonicals left with 0 linked performers after removing those performers.
EMPTY_CANONICALS = [25]   # Bruce Springsteen (no recordings anywhere)

# Sonny Rollins nesting
QUARTET_PERFORMER = 9     # "Sonny Rollins & Don Cherry Quartet" (has recording 9)
ROLLINS_CANONICAL = 13    # "Sonny Rollins"
QUARTET_CANONICAL = 9     # standalone "Sonny Rollins & Don Cherry Quartet" (to delete)


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    def do(sql, params=()):
        print(f"    SQL: {sql} {params}")
        if not dry:
            cur.execute(sql, params)

    # ── Guard: none of the orphan performances have recordings ────────────────
    for pid in ORPHAN_PERFORMANCES:
        n = cur.execute("SELECT count(*) FROM recording WHERE performance_id=?", (pid,)).fetchone()[0]
        assert n == 0, f"performance {pid} has {n} recordings — aborting"

    print("A) Deleting orphaned performances:", ORPHAN_PERFORMANCES)
    do(f"DELETE FROM performance WHERE id IN ({','.join('?'*len(ORPHAN_PERFORMANCES))})",
       ORPHAN_PERFORMANCES)

    print("   Deleting orphaned performers:", ORPHAN_PERFORMERS)
    for pid in ORPHAN_PERFORMERS:
        do("DELETE FROM performer_artist WHERE performer_id=?", (pid,))
        do("DELETE FROM performer_alias  WHERE performer_id=?", (pid,))
        do("DELETE FROM performer        WHERE id=?",           (pid,))

    print("   Deleting empty canonicals:", EMPTY_CANONICALS)
    for cid in EMPTY_CANONICALS:
        do("DELETE FROM artist_alias           WHERE artist_id=?", (cid,))
        do("DELETE FROM user_artist_permission WHERE artist_id=?", (cid,))
        do("DELETE FROM artist                 WHERE id=?",        (cid,))

    # ── B) Sonny Rollins nesting ──────────────────────────────────────────────
    print(f"\nB) Linking performer {QUARTET_PERFORMER} -> canonical {ROLLINS_CANONICAL} (Sonny Rollins)")
    existing = cur.execute(
        "SELECT 1 FROM performer_artist WHERE performer_id=? AND artist_id=?",
        (QUARTET_PERFORMER, ROLLINS_CANONICAL)).fetchone()
    if existing:
        print("   already linked — skip")
    else:
        do("INSERT INTO performer_artist (performer_id, artist_id, \"order\") VALUES (?,?,0)",
           (QUARTET_PERFORMER, ROLLINS_CANONICAL))

    print(f"   Deleting standalone canonical {QUARTET_CANONICAL} (Sonny Rollins & Don Cherry Quartet)")
    links = cur.execute("SELECT count(*) FROM performer_artist WHERE artist_id=?",
                        (QUARTET_CANONICAL,)).fetchone()[0]
    assert links == 0, f"canonical {QUARTET_CANONICAL} still has {links} performer links — aborting"
    do("DELETE FROM artist_alias           WHERE artist_id=?", (QUARTET_CANONICAL,))
    do("DELETE FROM user_artist_permission WHERE artist_id=?", (QUARTET_CANONICAL,))
    do("DELETE FROM artist                 WHERE id=?",        (QUARTET_CANONICAL,))

    if dry:
        print("\n[dry-run] no changes committed.")
        con.rollback()
    else:
        con.commit()
        print("\nCommitted.")
        print("integrity:", cur.execute("PRAGMA integrity_check").fetchone()[0])
        print("artists:", cur.execute("SELECT count(*) FROM artist").fetchone()[0],
              "performers:", cur.execute("SELECT count(*) FROM performer").fetchone()[0],
              "performances:", cur.execute("SELECT count(*) FROM performance").fetchone()[0])
    con.close()


if __name__ == "__main__":
    main()
