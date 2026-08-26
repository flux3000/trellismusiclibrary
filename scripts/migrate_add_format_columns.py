"""
Migration: add sample_rate_hz, bit_depth, bitrate_kbps to track_analysis.

Run after stopping Flask:
    cd ~/Workshop/dev/trellis && python3 scripts/migrate_add_format_columns.py
"""
import os, sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "../db/fluxaudio.db")
con = sqlite3.connect(DB_PATH)
cur = con.cursor()

existing = {row[1] for row in cur.execute("PRAGMA table_info(track_analysis)")}

added = []
for col, defn in [
    ("sample_rate_hz",     "INTEGER"),
    ("bit_depth",          "INTEGER"),
    ("bitrate_kbps",       "INTEGER"),
    ("spectral_cutoff_hz", "INTEGER"),
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
