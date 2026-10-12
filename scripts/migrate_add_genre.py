"""scripts/migrate_add_genre.py

Adds the Genre dimension: a new `genre` table plus a nullable
`performer.genre_id` FK. Additive and idempotent — safe to re-run. Seeds the
20-genre starting vocabulary from the Genre design spec; admins can add more
later through the normal CRUD — the seed is a starting point, not a closed
vocabulary.

    python3 scripts/migrate_add_genre.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)

SEED_GENRES = [
    "Rock", "Blues", "Jazz", "Fusion", "Bluegrass", "Newgrass", "Folk",
    "Americana", "Country", "Funk", "Soul", "R&B", "Reggae", "Jam",
    "Psychedelic", "Punk", "Metal", "Gospel", "Classical", "World",
]


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    existing_tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "genre" in existing_tables:
        print("genre table already exists — nothing to do.")
    else:
        cur.execute("""
            CREATE TABLE genre (
                id          INTEGER PRIMARY KEY,
                name        VARCHAR(80) NOT NULL UNIQUE,
                description TEXT,
                created_at  DATETIME NOT NULL,
                updated_at  DATETIME
            )""")
        con.commit()
        print("Created genre table.")

    performer_cols = {r[1] for r in cur.execute("PRAGMA table_info(performer)")}
    if "genre_id" in performer_cols:
        print("performer.genre_id already exists — nothing to do.")
    else:
        cur.execute("ALTER TABLE performer ADD COLUMN genre_id INTEGER REFERENCES genre(id)")
        con.commit()
        print("Added performer.genre_id.")

    # Seed vocabulary — case-insensitive, only inserts names not already present.
    have = {r[0].lower() for r in cur.execute("SELECT name FROM genre")}
    inserted = 0
    for name in SEED_GENRES:
        if name.lower() in have:
            continue
        cur.execute(
            "INSERT INTO genre (name, created_at) VALUES (?, datetime('now'))",
            (name,))
        inserted += 1
    con.commit()
    if inserted:
        print(f"Seeded {inserted} genre(s).")
    else:
        print("Seed genres already present — nothing to insert.")

    con.close()


if __name__ == "__main__":
    main()
