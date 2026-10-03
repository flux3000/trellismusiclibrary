"""
migrate_unified_import.py -- columns for the unified importer (Review First).

  bulk_ingest_run.mode               'auto' | 'hold', NOT NULL, default 'auto'
  bulk_ingest_run.applied_json       blanket "applies to every recording" values
  bulk_ingest_item.ingest_requested  a person asked for this item to be ingested

Existing runs predate Review First, so they backfill to 'auto'. Additive and
idempotent -- safe to re-run. New installs get the columns from create_all().

Run once:
    cd ~/Workshop/dev/trellis
    python3 scripts/migrate_unified_import.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy as sa

# (table, column, DDL type clause)
_COLUMNS = (
    ("bulk_ingest_run", "mode", "VARCHAR(8) NOT NULL DEFAULT 'auto'"),
    ("bulk_ingest_run", "applied_json", "TEXT"),
    ("bulk_ingest_item", "ingest_requested", "BOOLEAN NOT NULL DEFAULT 0"),
)


def migrate(engine):
    """Add whichever columns are missing; returns the list it added."""
    added = []
    with engine.connect() as conn:
        for table, column, ddl in _COLUMNS:
            cols = [row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))]
            if not cols:
                # Table absent: create_all() will make it with the columns.
                continue
            if column in cols:
                continue
            conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(f"{table}.{column}")
        conn.commit()
    return added


def main():
    from app import create_app
    from app.extensions import db

    app = create_app()
    with app.app_context():
        # Say which file was touched: a shell without TRELLIS_DATA_DIR migrates
        # the repo's empty db/ instead of the real library database.
        print(f"Database: {db.engine.url.database}")
        added = migrate(db.engine)
    if added:
        print("Added: " + ", ".join(added))
    else:
        print("Nothing to do -- all columns already exist.")


if __name__ == "__main__":
    main()
