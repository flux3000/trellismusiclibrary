"""scripts/migrate_add_genre_color_and_performer_images.py

  1. `genre.color` — nullable `#rrggbb` string driving the Browse cards' colour
     flair, editable through a colour picker in the Genre editor. Seeded with a
     starting palette matched BY NAME against the genres actually in this DB;
     anything unmatched stays NULL and renders neutral grey, which is a
     supported state rather than a gap (70 of 164 performers have no genre at
     all).

  2. `performer_image` — multiple images per Performer with one flagged
     primary, replacing the single `performer.image_ext` slot.

Both additive and idempotent — safe to re-run.

`performer.image_ext` is deliberately NOT dropped. SQLite cannot drop a column
without a full table rebuild, and this project has already been bitten by
rebuild-adjacent DDL. The column is backfilled into `performer_image` and then
simply ignored by the application.

The backfill DOES NOT MOVE OR RENAME ANY FILE. The legacy image keeps its
`profile<ext>` name on disk and the new row records that name verbatim, so an
interrupted run can never orphan a photo — re-running just re-checks rows.

This touches neither `performance` nor `user_artist_permission`, so the
hardcoded DDL snapshots in tools/repair_stale_fk_ddl.py need no re-dump.

    python3 scripts/migrate_add_genre_color_and_performer_images.py
    python3 scripts/migrate_add_genre_color_and_performer_images.py --dry-run
"""

import os
import sys
import sqlite3
from datetime import datetime, timezone

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)

# Starting palette. Matched case-insensitively against genre.name, so it fits
# the vocabulary in THIS database rather than the original spec list. Chosen
# to stay legible on both themes' backgrounds — the dark theme is a warm
# near-black (#1a1714), the light a warm cream (#f0ebe3), so fully-saturated
# primaries read badly on one or the other. These are starting points; the
# whole reason the picker exists is that the user will disagree with some of
# them.
SEED_COLORS = {
    "bluegrass & traditional":   "#7a9a5b",   # grass green
    "acoustic jazz & newgrass":  "#5fa39a",   # teal, adjacent to bluegrass
    "jazz":                      "#6a7fb5",   # blue
    "blues":                     "#3f7fa8",   # deeper blue
    "americana & country":       "#c08a4a",   # tan
    "rock & pop":                "#c9605a",   # red
    "punk":                      "#8f4a5f",   # oxblood
    "funk":                      "#d08a2e",   # orange
    "soul / r&b":                "#a05a3c",   # burnt sienna
    "rap":                       "#7d7f88",   # slate
    "folk":                      "#9a8f5e",   # olive
    "reggae":                    "#3f9a6a",   # green, distinct from bluegrass
    "world":                     "#b06a8a",   # rose
    "psychedelic":               "#9b6fc0",   # purple
}


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    cur = con.cursor()
    now = datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")

    def tables():
        return {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def cols(t):
        return {r[1] for r in cur.execute(f"PRAGMA table_info({t})")}

    print(f"DB: {os.path.abspath(DB)}")
    print(f"Mode: {'DRY RUN' if dry else 'write'}\n")

    # ── 1. genre.color ───────────────────────────────────────────────────────
    if "genre" not in tables():
        print("!! genre table missing — run migrate_add_genre.py first.")
        return 1

    if "color" in cols("genre"):
        print("genre.color already exists.")
    else:
        print("+ ALTER TABLE genre ADD COLUMN color VARCHAR(7)")
        if not dry:
            cur.execute("ALTER TABLE genre ADD COLUMN color VARCHAR(7)")
            con.commit()

    # Seed only genres that have no colour yet — never overwrite a colour the
    # user has already picked, on any re-run.
    if not dry:
        seeded = skipped = unmatched = 0
        for gid, name, color in cur.execute(
                "SELECT id, name, color FROM genre").fetchall():
            if color:
                skipped += 1
                continue
            want = SEED_COLORS.get((name or "").strip().lower())
            if not want:
                unmatched += 1
                continue
            cur.execute("UPDATE genre SET color=? WHERE id=?", (want, gid))
            seeded += 1
        con.commit()
        print(f"  seeded {seeded} · already coloured {skipped} · "
              f"no palette entry {unmatched} (these stay NULL → neutral grey)")

    # ── 2. performer_image ───────────────────────────────────────────────────
    if "performer_image" in tables():
        print("performer_image table already exists.")
    else:
        print("+ CREATE TABLE performer_image")
        if not dry:
            cur.execute("""
                CREATE TABLE performer_image (
                    id           INTEGER PRIMARY KEY,
                    performer_id INTEGER NOT NULL
                                 REFERENCES performer(id) ON DELETE CASCADE,
                    filename     VARCHAR(255) NOT NULL,
                    ext          VARCHAR(8)   NOT NULL,
                    is_primary   BOOLEAN NOT NULL DEFAULT 0,
                    sort_order   INTEGER NOT NULL DEFAULT 0,
                    origin       VARCHAR(24) NOT NULL DEFAULT 'upload',
                    caption      VARCHAR(255),
                    credit       VARCHAR(255),
                    source_ref   VARCHAR(255),
                    created_at   DATETIME
                )""")
            cur.execute("CREATE INDEX ix_performer_image_performer_id "
                        "ON performer_image(performer_id)")
            con.commit()

    # ── 2b. performer_image.source_ref ───────────────────────────────────────
    # Separate step because the table may already exist from an earlier run of
    # this script.
    if "performer_image" in tables():
        if "source_ref" in cols("performer_image"):
            print("performer_image.source_ref already exists.")
        else:
            print("+ ALTER TABLE performer_image ADD COLUMN source_ref VARCHAR(255)")
            if not dry:
                cur.execute("ALTER TABLE performer_image ADD COLUMN source_ref VARCHAR(255)")
                con.commit()

    # ── 3. backfill legacy image_ext ─────────────────────────────────────────
    if "image_ext" not in cols("performer"):
        print("performer.image_ext absent — nothing to backfill.")
    else:
        legacy = cur.execute(
            "SELECT id, name, image_ext FROM performer "
            "WHERE image_ext IS NOT NULL AND image_ext <> ''").fetchall()
        print(f"\nLegacy images to backfill: {len(legacy)}")
        done = 0
        for pid, pname, ext in legacy:
            if not dry:
                exists = cur.execute(
                    "SELECT 1 FROM performer_image WHERE performer_id=?",
                    (pid,)).fetchone()
                if exists:
                    continue
                cur.execute(
                    "INSERT INTO performer_image (performer_id, filename, ext, "
                    "is_primary, sort_order, origin, created_at) "
                    "VALUES (?,?,?,1,0,'upload',?)",
                    (pid, "profile" + ext, ext, now))
            done += 1
            print(f"  · {pname} → profile{ext} (file left in place)")
        if not dry:
            con.commit()
        print(f"Backfilled {done}.")

    con.close()
    print("\nDone." if not dry else "\nDry run — nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
