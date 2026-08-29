"""
Migration: add spectral_flatness, duration_s, non_music_score to track_analysis.

The non-music signal (2026-08-28) — see app/utils/track_signals.py. The first
two are raw per-track measurements written by analyse_track(); the third is
derived at the recording level and is NULL until a recording has been analysed
with at least four tracks.

Run after stopping Flask:
    cd ~/Workshop/dev/trellis && python3 scripts/migrate_add_non_music_signal.py

Idempotent — safe to run twice. Existing rows keep NULLs and are filled in by
the next analysis pass (analysis_version moved to "3" in the same change, so
every stale row re-analyses on its own).
"""
import os, sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "../db/fluxaudio.db")
con = sqlite3.connect(DB_PATH)
cur = con.cursor()

existing = {row[1] for row in cur.execute("PRAGMA table_info(track_analysis)")}

added = []
for col, defn in [
    ("spectral_flatness", "REAL"),
    ("duration_s",        "REAL"),
    ("non_music_score",   "REAL"),
]:
    if col not in existing:
        cur.execute(f"ALTER TABLE track_analysis ADD COLUMN {col} {defn}")
        added.append(col)

con.commit()
con.close()

if added:
    print(f"Added columns: {', '.join(added)}")
else:
    print("All columns already present — nothing to do.")
