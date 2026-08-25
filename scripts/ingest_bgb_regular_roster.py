"""
scripts/ingest_bgb_regular_roster.py — Blue Grass Boys "Regular" roster.

Populates Membership stints (and any new Artist rows they need) for Performer
116, "Bill Monroe and the Bluegrass Boys," from a reviewed CSV built out of
doodah.net/bgb/alpha.html — the "Regular" members tier only (73 people, 107
stints; Fill-in/Studio/Unconfirmed deliberately out of scope, deferred not
cancelled — see the 2026-08-25 meeting note).

REQUIRES: scripts/migrate_add_membership_instrument.py has already been run.

The CSV is the unit of review — Ryan edits it by hand (names, instrument
strings, date splits) before this script ever runs. This script trusts the
CSV completely and does no independent judgment calls; every '*_if_different'
or 'REUSE_EXISTING' decision was made at CSV-authoring time, not here.

Idempotent: safe to re-run after fixing a row. Matches existing Artists by
name (exact, case-sensitive) as well as by explicit existing_artist_id, so a
partial prior run won't duplicate people. Matches existing Membership stints
on (performer_id, artist_id, start_*, end_*) so a partial prior run won't
duplicate stints either.

DRY-RUN by default — prints every insert it WOULD make. Add --commit to
apply, same convention as cleanup_dangling_memberships.py.

    python3 scripts/ingest_bgb_regular_roster.py
    python3 scripts/ingest_bgb_regular_roster.py --commit
    python3 scripts/ingest_bgb_regular_roster.py --csv path/to/edited.csv --commit
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.extensions import db
from app.models.artist import Artist, Membership
from app.models.performer import Performer, PerformerResource

PERFORMER_ID = 116  # Bill Monroe and the Bluegrass Boys
DEFAULT_CSV = os.path.join(os.path.dirname(__file__), "data", "bgb_regular_ingest_review.csv")

SOURCE_URL = "https://doodah.net/bgb/"
SOURCE_LABEL = "A Tribute to Bill Monroe's Blue Grass Boys (doodah.net)"


def _int_or_none(s):
    s = (s or "").strip()
    return int(s) if s else None


def resolve_artist(row, cache, existing_by_name):
    """Return (artist, created: bool). `cache` is keyed by CSV artist_name so
    a person appearing across multiple stint rows resolves to one Artist."""
    name = row["artist_name"].strip()
    if name in cache:
        return cache[name], False

    if row["artist_action"] == "REUSE_EXISTING":
        artist_id = int(row["existing_artist_id"])
        artist = db.session.get(Artist, artist_id)
        if artist is None:
            raise ValueError(f"existing_artist_id {artist_id} for {name!r} does not exist")
        if artist.name != name:
            print(f"  note: existing artist #{artist_id} is named "
                  f"{artist.name!r}, CSV says {name!r} — using the DB name.")
        cache[name] = artist
        return artist, False

    # CREATE_NEW — but check by exact name first, in case a prior run (or
    # someone else) already created this person.
    if name in existing_by_name:
        cache[name] = existing_by_name[name]
        return existing_by_name[name], False

    legal = row["legal_name_if_different"].strip()
    bio = f'Also known as {legal}.' if legal else None
    # "Lastname, First \"Nick\"" -> "Lastname, First" sort_name (nickname dropped).
    last, first_part = row["source_name"].split(",", 1)
    sort_name = f'{last.strip()}, {first_part.split(chr(34))[0].strip()}'

    artist = Artist(name=name, sort_name=sort_name, bio=bio)
    db.session.add(artist)
    db.session.flush()  # get artist.id without committing
    cache[name] = artist
    existing_by_name[name] = artist
    return artist, True


def main(csv_path, commit):
    app = create_app()
    with app.app_context():
        performer = db.session.get(Performer, PERFORMER_ID)
        if performer is None:
            print(f"Performer {PERFORMER_ID} not found — aborting.")
            return

        existing_by_name = {a.name: a for a in db.session.query(Artist).all()}
        existing_stints = {
            (m.performer_id, m.artist_id, m.start_year, m.start_month, m.start_day,
             m.end_year, m.end_month, m.end_day)
            for m in performer.memberships
        }
        next_order = (max((m.order for m in performer.memberships), default=-1) + 1)

        artist_cache = {}
        n_artists_created = 0
        n_stints_created = 0
        n_stints_skipped = 0

        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                artist, created = resolve_artist(row, artist_cache, existing_by_name)
                if created:
                    n_artists_created += 1
                    print(f"  + Artist: {artist.name!r}"
                          + (f"  (sort: {artist.sort_name!r})" if artist.sort_name else ""))

                key = (
                    PERFORMER_ID, artist.id if artist.id else None,
                    _int_or_none(row["start_year"]), _int_or_none(row["start_month"]), _int_or_none(row["start_day"]),
                    _int_or_none(row["end_year"]), _int_or_none(row["end_month"]), _int_or_none(row["end_day"]),
                )
                if key in existing_stints:
                    n_stints_skipped += 1
                    continue

                m = Membership(
                    performer_id=PERFORMER_ID,
                    artist_id=artist.id,
                    order=next_order,
                    instrument=row["instruments"],
                    start_year=key[2], start_month=key[3], start_day=key[4],
                    end_year=key[5], end_month=key[6], end_day=key[7],
                )
                db.session.add(m)
                existing_stints.add(key)
                next_order += 1
                n_stints_created += 1
                print(f"    stint: {artist.name} — {row['instruments']} "
                      f"({row['stint_start']} to {row['stint_end']})")

        # Attribution — PerformerResource, same pattern as the PMDB example
        # in that model's docstring. Idempotent on exact URL match.
        has_resource = any(r.url == SOURCE_URL for r in performer.resources)
        if has_resource:
            print("PerformerResource for doodah.net already present — skipping.")
        else:
            next_res_order = (max((r.order for r in performer.resources), default=-1) + 1)
            db.session.add(PerformerResource(
                performer_id=PERFORMER_ID, label=SOURCE_LABEL, url=SOURCE_URL,
                order=next_res_order,
            ))
            print(f"  + PerformerResource: {SOURCE_LABEL!r}")

        print()
        print(f"Artists to create:  {n_artists_created}")
        print(f"Stints to create:   {n_stints_created}")
        print(f"Stints skipped (already present): {n_stints_skipped}")

        if commit:
            db.session.commit()
            print("COMMITTED.")
        else:
            db.session.rollback()
            print("DRY-RUN — nothing written. Re-run with --commit to apply.")


if __name__ == "__main__":
    args = sys.argv[1:]
    commit = "--commit" in args
    csv_path = DEFAULT_CSV
    if "--csv" in args:
        csv_path = args[args.index("--csv") + 1]
    main(csv_path, commit)
