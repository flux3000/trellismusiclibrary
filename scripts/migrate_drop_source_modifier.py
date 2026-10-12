"""scripts/migrate_drop_source_modifier.py Drop `recording.source_modifier`
("Source Detail" in the UI) — its role has been folded into
`recording.lineage`. If a recording needs to keep that info, add it to Lineage
or Notes by hand.

This is destructive (drops a column, taking its data with it) — back up
db/fluxaudio.db first if you want a copy of the old source_modifier values.

Idempotent: no-ops if the column is already gone.
Requires SQLite 3.35+ (macOS ships this since Big Sur; `ALTER TABLE ... DROP
COLUMN` will raise a clear error on anything older).

Run once from the repo root:  python3 scripts/migrate_drop_source_modifier.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    existing = {r[1] for r in cur.execute("PRAGMA table_info(recording)")}
    if "source_modifier" not in existing:
        print("recording.source_modifier already gone — nothing to do.")
        con.close()
        return

    try:
        cur.execute("ALTER TABLE recording DROP COLUMN source_modifier")
        con.commit()
        print("Dropped recording.source_modifier.")
    except sqlite3.OperationalError as e:
        con.close()
        raise SystemExit(
            "Could not drop the column (SQLite may be older than 3.35): %s\n"
            "Check your SQLite version with: python3 -c \"import sqlite3; print(sqlite3.sqlite_version)\""
            % e
        )
    con.close()


if __name__ == "__main__":
    main()
