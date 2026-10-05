"""
tests/test_ai_assist_v2.py -- what is left of the 2026-09-07 AI Assist tests after Lomax
(2026-10-05) replaced app/utils/ai_assist.py and artist_research.py: the lineup_json
migration script, and the artist payload before any research has run. The search budget, caching,
info-file cap, recap, grounding and modes are covered by tests/test_lomax_*.py.
"""

import pytest


@pytest.fixture()
def api(app):
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def test_migrate_add_performer_lineup_is_idempotent(tmp_path):
    # Exercises a one-off migration against the schema of its day, so it keeps
    # that day's names (not renamed 2026-09-16).
    import sqlite3
    from scripts import migrate_add_performer_lineup as mod

    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE performer (id INTEGER PRIMARY KEY, name TEXT)")
    con.commit()
    con.close()

    original = mod.DB
    try:
        mod.DB = str(db_path)
        mod.main()
        mod.main()   # must not raise "duplicate column"
    finally:
        mod.DB = original

    con = sqlite3.connect(str(db_path))
    cols = [r[1] for r in con.execute("PRAGMA table_info(performer)")]
    con.close()
    assert "lineup_json" in cols


def test_a_artist_with_no_lineup_research_reports_none(api, seeded_ids):
    # null, not {} — the page distinguishes "never run" from "ran, found
    # nobody", and those deserve different words on screen.
    got = api.get(f"/api/artists/{seeded_ids['artist_id']}").get_json()
    assert got["lineup"] is None
