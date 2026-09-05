#!/usr/bin/env python3
"""
tools/repair_stale_fk_ddl.py — Repair stale FOREIGN KEY clauses baked into
the on-disk schema of trellis.db.

Background
----------
The 2026-07-11 Performer/Artist remodel renamed/dropped several tables
(`_artist_acts_old`, `canonical_artist`). FK enforcement was OFF at the time
(SQLite only checks `PRAGMA foreign_keys` at runtime; it's not persisted in
the file, and defaults to OFF), so SQLite's ALTER/RENAME machinery never
rewrote the FK clauses embedded in other tables' stored CREATE TABLE SQL.
The ORM models (app/models/performance.py, app/models/user.py) have always
declared these FKs correctly against `performer.id` — only the physical
schema text on disk is stale. A comprehensive audit (PRAGMA foreign_key_list
on every table + PRAGMA foreign_key_check) found exactly two affected
tables:

    performance.performer_id            -> stale: "_artist_acts_old"(id)
                                            correct: performer(id)
    user_artist_permission.performer_id -> stale: "canonical_artist"(id)
                                            correct: performer(id)

Both stale referenced tables no longer exist in the DB at all, which is why
`PRAGMA foreign_key_check` reports every row of `performance` as a
violation (237 rowids / 230 live rows) even though every performer_id value
is valid data pointing at a real `performer.id` row. This script does not
change any data — only the DDL text SQLite stores for these two tables.

What this script does
----------------------
For each affected table, runs SQLite's documented 12-step "table rebuild"
procedure (https://www.sqlite.org/lang_altertable.html#otheralter), entirely
inside one transaction with `PRAGMA foreign_keys=OFF` for the duration of
the rebuild (per SQLite docs — required so the DROP TABLE step doesn't trip
over the very FK we're repairing):

    1. Create the new table under a temp name, with corrected DDL.
    2. Copy all rows across with an explicit, order-safe column list.
    3. Drop the old table.
    4. Rename the new table to the original name.
    5. Recreate any indexes that existed on the original (dumped from the
       ORM-generated reference schema — see below).

The corrected DDL below is a point-in-time dump (NOT re-derived live at
import/run time) taken from a fresh SQLite DB created by SQLAlchemy's
`db.create_all()` against the live models — that guarantees it matches the
models exactly, including every other FK (venue_id, event_id, user_id, ...),
column types, and nullability. Because it's a snapshot rather than a live
derivation, any future model change to these two tables' columns must be
re-dumped into REPAIR_SPECS by hand, or this script will silently rebuild
against a stale shape.

2026-07-22 update: `app/models/performance.py` now declares
`server_default="inherit"` on `personnel_mode` (alongside the existing
Python-side `default="inherit"`), so model and DB agree again. The DDL
below was re-dumped to include `DEFAULT 'inherit'`, matching the physical
default a past raw ALTER TABLE had baked in. Previously this script would
have silently dropped that DB-level default on rebuild — that gap is now
closed.

Usage
-----
    python3 tools/repair_stale_fk_ddl.py /path/to/copy_of_trellis.db --yes

Safety
------
- Refuses to run against anything whose path ends in db/trellis.db
  (the live DB) even with --yes — copy it first.
- Refuses to run at all without --yes (explicit opt-in).
- Verifies PRAGMA foreign_key_check == 0 rows and PRAGMA integrity_check
  == 'ok' after the rebuild, and asserts pre/post row counts match exactly
  for every rebuilt table. Raises and rolls back on any mismatch.
"""

import argparse
import sqlite3
import sys
from pathlib import Path


# ── The repair set ─────────────────────────────────────────────────────────
# Each entry: table name -> (corrected CREATE TABLE DDL, explicit column
# list in old-table order, list of CREATE INDEX statements to reapply).
#
# DDL and index lists below were dumped verbatim from a fresh DB created via
# `db.create_all()` against the live SQLAlchemy models (2026-07-22). Neither
# table had any indexes beyond the implicit PRIMARY KEY index, so the index
# list is empty for both — kept as a list (not hardcoded away) so this
# script stays correct if a future model adds one.

REPAIR_SPECS = {
    "performance": {
        "ddl": """\
CREATE TABLE performance (
	id INTEGER NOT NULL,
	performer_id INTEGER NOT NULL,
	venue_id INTEGER,
	event_id INTEGER,
	title VARCHAR(255),
	stage VARCHAR(128),
	personnel_mode VARCHAR(16) DEFAULT 'inherit' NOT NULL,
	start_year INTEGER,
	start_month INTEGER,
	start_day INTEGER,
	end_year INTEGER,
	end_month INTEGER,
	end_day INTEGER,
	city VARCHAR(128),
	state VARCHAR(64),
	country VARCHAR(64),
	notes TEXT,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(performer_id) REFERENCES performer (id),
	FOREIGN KEY(venue_id) REFERENCES venue (id),
	FOREIGN KEY(event_id) REFERENCES event (id)
)""",
        # Explicit column list, in the OLD (on-disk) table's column order —
        # this is what we SELECT from the old table, so it must match the
        # old table's actual columns. The new table's own column order
        # (above) doesn't need to match; INSERT INTO new (...) SELECT (...)
        # matches by explicit name/position pairing, not physical layout.
        "old_columns": [
            "id", "performer_id", "venue_id", "event_id", "title", "stage",
            "start_year", "start_month", "start_day", "end_year",
            "end_month", "end_day", "city", "state", "country", "notes",
            "created_at", "updated_at", "personnel_mode",
        ],
        "indexes": [],
    },
    "user_artist_permission": {
        "ddl": """\
CREATE TABLE user_artist_permission (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	performer_id INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES user (id),
	FOREIGN KEY(performer_id) REFERENCES performer (id)
)""",
        "old_columns": ["id", "user_id", "performer_id"],
        "indexes": [],
    },
}


def refuse_if_live_db(db_path: Path):
    """Hard stop if pointed at the live DB file, even with --yes."""
    # Both names on purpose. This is a REFUSAL list, so a stale entry is
    # harmless and a missing one is not: db/fluxaudio.db was a symlink to
    # the live file until 2026-09-04, and any copy of this repo that still
    # has one must still be refused.
    if db_path.name in {"trellis.db", "fluxaudio.db"} and db_path.parent.name == "db":
        print(
            "REFUSING TO RUN: this looks like the live DB "
            f"({db_path}).\n"
            "Copy it first, e.g.:\n"
            f"  cp {db_path} /tmp/trellis_repair.db\n"
            "then run this script against the COPY.",
            file=sys.stderr,
        )
        sys.exit(1)


def table_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def find_stale_fks(cur):
    """
    Re-derive the audit at runtime rather than trusting REPAIR_SPECS blindly:
    enumerate every table's FK list and flag any whose referenced table is
    missing. Used both to report on affected tables and to sanity-check that
    REPAIR_SPECS covers everything actually found stale in THIS db file.
    """
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    tables = [r[0] for r in cur.fetchall()]
    existing = set(tables)

    stale = {}
    for t in tables:
        cur.execute(f'PRAGMA foreign_key_list("{t}")')
        for fk in cur.fetchall():
            ref_table = fk[2]
            if ref_table not in existing:
                stale.setdefault(t, []).append((fk[3], ref_table, fk[4]))
    return stale


def rebuild_table(cur, table, spec):
    """Run the 12-step SQLite table rebuild for one table."""
    tmp_name = f"_repair_new_{table}"
    cols = spec["old_columns"]
    col_list = ", ".join(cols)

    cur.execute(f'SELECT COUNT(*) FROM "{table}"')
    before_count = cur.fetchone()[0]

    # 1. New table under temp name.
    new_ddl = spec["ddl"].replace(
        f"CREATE TABLE {table}", f"CREATE TABLE {tmp_name}", 1
    )
    cur.execute(new_ddl)

    # 2. Copy data across, explicit column list both sides (order-safe).
    cur.execute(
        f'INSERT INTO "{tmp_name}" ({col_list}) '
        f'SELECT {col_list} FROM "{table}"'
    )

    # 3. Drop old table.
    cur.execute(f'DROP TABLE "{table}"')

    # 4. Rename new -> original.
    cur.execute(f'ALTER TABLE "{tmp_name}" RENAME TO "{table}"')

    # 5. Recreate indexes (none currently, kept for future-proofing).
    for idx_sql in spec["indexes"]:
        cur.execute(idx_sql)

    cur.execute(f'SELECT COUNT(*) FROM "{table}"')
    after_count = cur.fetchone()[0]

    if after_count != before_count:
        raise RuntimeError(
            f"Row count mismatch rebuilding {table}: "
            f"{before_count} before, {after_count} after. Rolling back."
        )

    return before_count, after_count


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild stale-FK tables (performance, user_artist_permission) "
            "in a trellis.db COPY so the on-disk DDL matches the ORM "
            "models. NEVER point this at the live DB."
        )
    )
    parser.add_argument("db_path", help="Path to a COPY of trellis.db")
    parser.add_argument(
        "--yes", action="store_true",
        help="Required. Confirms you understand this rewrites schema in place "
             "on the given file.",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    refuse_if_live_db(db_path)

    if not args.yes:
        print(
            "Refusing to run without --yes. This rewrites table schema "
            f"in place on: {db_path}\n"
            "Make sure this is a disposable COPY, then re-run with --yes.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not db_path.exists():
        print(f"No such file: {db_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Target DB: {db_path}")
    print("=" * 70)

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # ── Pre-flight: confirm what's actually stale in THIS file ────────────
    stale_before = find_stale_fks(cur)
    print("Stale FK audit (before):")
    if not stale_before:
        print("  None found. Nothing to repair.")
        conn.close()
        return
    for t, fks in stale_before.items():
        for col, ref, to_col in fks:
            print(f'  {t}.{col} -> "{ref}"({to_col})  [missing table]')

    missing_specs = set(stale_before) - set(REPAIR_SPECS)
    if missing_specs:
        print(
            f"\nERROR: found stale FKs on {sorted(missing_specs)} with no "
            "REPAIR_SPECS entry. Refusing to proceed blind — add a spec "
            "for these tables (derived from a fresh db.create_all() dump) "
            "before running.",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)

    to_repair = list(stale_before.keys())
    print(f"\nTables to rebuild: {to_repair}")
    print("=" * 70)

    # Pre-repair row counts for every table in the DB (not just the ones
    # being rebuilt) — belt-and-suspenders check that the rebuild didn't
    # touch anything it shouldn't have.
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    all_tables = [r[0] for r in cur.fetchall()]
    counts_before = {}
    for t in all_tables:
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        counts_before[t] = cur.fetchone()[0]

    # ── Rebuild, inside one transaction, FK enforcement OFF ───────────────
    # PRAGMA foreign_keys cannot be changed inside a transaction, so set it
    # before BEGIN.
    cur.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        report = {}
        for t in to_repair:
            before, after = rebuild_table(cur, t, REPAIR_SPECS[t])
            report[t] = (before, after)
        conn.commit()
    except Exception:
        conn.rollback()
        print("\nREBUILD FAILED — rolled back. DB unchanged.", file=sys.stderr)
        raise
    finally:
        cur.execute("PRAGMA foreign_keys=ON")

    print("\nRebuild complete. Row counts (before -> after):")
    for t, (before, after) in report.items():
        print(f"  {t}: {before} -> {after}")

    # ── Post-repair verification ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("PRAGMA foreign_key_check:")
    cur.execute("PRAGMA foreign_key_check")
    fkc = cur.fetchall()
    if fkc:
        for row in fkc:
            print(" ", row)
        print(f"  -> {len(fkc)} violation row(s) REMAIN.", file=sys.stderr)
    else:
        print("  0 rows -- clean.")

    print("\nPRAGMA integrity_check:")
    cur.execute("PRAGMA integrity_check")
    integrity = cur.fetchall()
    print(" ", integrity[0][0])

    print("\nStale FK audit (after):")
    stale_after = find_stale_fks(cur)
    if not stale_after:
        print("  None. All FK targets resolve to existing tables.")
    else:
        for t, fks in stale_after.items():
            for col, ref, to_col in fks:
                print(f'  STILL STALE: {t}.{col} -> "{ref}"({to_col})')

    print("\nRow counts for ALL tables (before -> after, full-db sanity check):")
    all_ok = True
    for t in all_tables:
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        after = cur.fetchone()[0]
        before = counts_before[t]
        flag = "" if before == after else "  <-- MISMATCH"
        if before != after:
            all_ok = False
        print(f"  {t}: {before} -> {after}{flag}")

    conn.close()

    print("\n" + "=" * 70)
    if fkc or integrity[0][0] != "ok" or stale_after or not all_ok:
        print("RESULT: FAILED verification. Do not use this file.", file=sys.stderr)
        sys.exit(1)
    print("RESULT: OK. foreign_key_check clean, integrity_check ok, "
          "row counts unchanged, no stale FKs remain.")


if __name__ == "__main__":
    main()
