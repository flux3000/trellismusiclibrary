"""scripts/migrate_add_quality.py — Listening Quality persistence.

Adds (all additive, idempotent, safe to re-run):
  quality_analysis   (new) — pre-ingest staging, keyed by source folder path
  recording_quality  (new) — permanent per-recording score

Nothing existing is touched and no row changes meaning: both tables are new and
start empty. Design Plan.md" §3 for why this is two tables rather than one.

Run once from the repo root:
    python3 scripts/migrate_add_quality.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)

# The score payload, identical in both tables — mirrors _ScoreColumnsMixin in
# app/models/quality.py. Keep the two in step.
_SCORE_COLUMNS = """
    listening_quality      REAL,
    score_tone             REAL,
    score_noise            REAL,
    score_dynamics         REAL,
    features_json          TEXT,
    technical_issues_json  TEXT,
    flags_json             TEXT,
    sampled_json           TEXT,
    technical_deduction    REAL DEFAULT 0.0,
    analysis_version       VARCHAR(16),
    score_version          VARCHAR(16)
"""


def _has_table(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _create(cur, name, ddl, indexes=()):
    if _has_table(cur, name):
        print(f"{name} already exists")
        return
    cur.execute(ddl)
    for idx in indexes:
        cur.execute(idx)
    print(f"created {name}")


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # ── quality_analysis — pre-ingest staging ────────────────────────────────
    _create(
        cur,
        "quality_analysis",
        f"""
        CREATE TABLE quality_analysis (
            id             INTEGER PRIMARY KEY,
            folder_path    VARCHAR(1024) NOT NULL UNIQUE,
            source_dir     VARCHAR(1024),
            name           VARCHAR(512),
            triage_status  VARCHAR(16) NOT NULL DEFAULT 'pending',
            recording_id   INTEGER REFERENCES recording(id) ON DELETE SET NULL,
            error          TEXT,
            {_SCORE_COLUMNS},
            created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        indexes=(
            "CREATE INDEX ix_quality_analysis_folder_path "
            "ON quality_analysis (folder_path)",
            "CREATE INDEX ix_quality_analysis_source_dir "
            "ON quality_analysis (source_dir)",
            "CREATE INDEX ix_quality_analysis_triage_status "
            "ON quality_analysis (triage_status)",
            "CREATE INDEX ix_quality_analysis_recording_id "
            "ON quality_analysis (recording_id)",
        ),
    )

    # ── recording_quality — permanent ────────────────────────────────────────
    _create(
        cur,
        "recording_quality",
        f"""
        CREATE TABLE recording_quality (
            id            INTEGER PRIMARY KEY,
            recording_id  INTEGER NOT NULL UNIQUE
                          REFERENCES recording(id) ON DELETE CASCADE,
            {_SCORE_COLUMNS},
            analyzed_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        indexes=(
            "CREATE INDEX ix_recording_quality_recording_id "
            "ON recording_quality (recording_id)",
        ),
    )

    con.commit()
    con.close()
    print("done")


if __name__ == "__main__":
    main()
