"""
The import queue's Metadata band (app/utils/completeness.py), the "At " venue lead, and an
event read from a free-form folder name. Pure: Resolved stand-ins, hand-built scans.
"""
from types import SimpleNamespace as NS

import pytest

from app.utils.completeness import RANK, completeness, completeness_band
from app.utils.resolve import resolve
from tests.test_confidence import _artist_scan


def _res(artist="Boxcars", date=(2014, 7, 25), venue="Bicentennial Park Pavilion", city="Columbus",
         state="OH", country=None, titles=None, n=21):
    f = lambda v: NS(value=v)
    d = {"year": date[0], "month": date[1], "day": date[2]} if date else {}
    titles = titles if titles is not None else [f"Song {i}" for i in range(1, n + 1)]
    return NS(artist=f(artist), date=f(d), venue=f(venue), city=f(city), state=f(state), country=f(country),
              tracks=[{"title": t} for t in titles])


def test_boxcars_with_no_source_or_lineage_is_high():
    r = _res()
    assert completeness_band(r) == "green"
    assert completeness(r) == {"band": "green", "rating": "High", "rank": 2, "missing": []}


def test_no_location_is_medium():
    r = _res(artist="Go Kurosawa", city=None, state=None, country=None)
    assert completeness_band(r) == "yellow"
    assert completeness(r)["missing"] == ["location"]


@pytest.mark.parametrize("kw,missing", [
    ({"venue": None}, ["venue"]),
    ({"state": None, "country": None}, ["location"]),
    ({"titles": ["Song"] * 20 + ["Track 21"]}, ["tracks"]),
])
def test_exactly_one_of_venue_location_titles_missing_is_medium(kw, missing):
    r = _res(**kw)
    assert completeness_band(r) == "yellow" and completeness(r)["missing"] == missing


def test_a_missing_artist_is_low():
    assert completeness_band(_res(artist=None)) == "red"


def test_a_missing_or_partial_date_is_low():
    assert completeness_band(_res(date=None)) == "red"
    assert completeness_band(_res(date=(2014, 7, None))) == "red"


def test_two_missing_fields_are_low():
    assert completeness_band(_res(venue=None, city=None, state=None)) == "red"


@pytest.mark.parametrize("title", ["Track 3", "track 03", "Untitled", "", None])
def test_placeholder_titles_count_as_missing(title):
    r = _res(titles=["Song One", title, "Song Three"])
    assert completeness(r)["missing"] == ["tracks"]


def test_no_tracks_is_missing_titles_and_a_city_with_a_country_is_a_location():
    assert completeness(_res(titles=[]))["missing"] == ["tracks"]
    assert completeness_band(_res(state=None, country="Canada")) == "green"


def test_sort_order_is_high_over_medium_over_low():
    assert RANK["green"] > RANK["yellow"] > RANK["red"]


def test_none_reads_low():
    assert completeness_band(None) == "red"


# ── "At " / "@" venue lead ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,out", [
    ("At Barley's Tap Room", "Barley's Tap Room"),
    ("AT Fillmore", "Fillmore"),
    ("@ Barley's Tap Room", "Barley's Tap Room"),
    ("@Fillmore West", "Fillmore West"),
    ("At The Gate", "At The Gate"),
    ("Atlanta Hall", "Atlanta Hall"),
    ("Attic Club", "Attic Club"),
    ("At", "At"),
])
def test_strip_venue_at(raw, out):
    from app.utils.reader.billing import strip_venue_at
    assert strip_venue_at(raw) == out


def test_an_at_venue_line_reads_without_the_at():
    from app.utils.ingest import parse_info_file
    r = parse_info_file(None, text="Go Kurosawa\nAt Barley's Tap Room\nKnoxville, TN\n3/28/2026\n\nSource: SBD\n\n01. a\n02. b\n")
    assert r["venue"] == "Barley's Tap Room"


# ── event from the folder name ───────────────────────────────────────────────

def _kurosawa(folder, event=None):
    sc = _artist_scan(info="Go Kurosawa", folder_name=folder)
    sc["suggestions"]["from_info_file"]["venue"] = "Barley's Tap Room"
    if event:
        sc["suggestions"]["from_info_file"]["event"] = event
    return resolve(sc)


def test_a_festival_after_the_artist_in_the_folder_name_is_a_tentative_event():
    r = _kurosawa("Go Kurosawa Big Ears Festival (Barley's Tap Room) 3-28-26")
    assert r.event.value == "Big Ears Festival"
    assert r.event.confidence == "tentative" and r.event.source == "folder"
    assert r.venue.value == "Barley's Tap Room"


def test_the_info_texts_event_wins_over_the_folder():
    assert _kurosawa("Go Kurosawa Big Ears Festival (Barley's Tap Room) 3-28-26", event="Other Fest").event.value == "Other Fest"


@pytest.mark.parametrize("folder", [
    "Go Kurosawa Spring Tour (Barley's Tap Room) 3-28-26",
    "Go Kurosawa (Barley's Tap Room) 3-28-26",
    "Go Kurosawa - 2026-03-28 - Barley's Tap Room - Knoxville, TN",
    "Go Kurosawa Opened for Strokes Festival 3-28-26",
])
def test_not_an_event_gives_none(folder):
    assert _kurosawa(folder).event.value is None


@pytest.mark.parametrize("raw", ["AT&T Park", "At-Large Hall", "At. Louis", "Atlanta Civic Center", "AT&T Stadium"])
def test_strip_venue_at_needs_whitespace_after_at(raw):
    from app.utils.reader.billing import strip_venue_at
    assert strip_venue_at(raw) == raw


def test_strip_venue_at_at_sign_forms():
    from app.utils.reader.billing import strip_venue_at
    assert strip_venue_at("@ Fillmore") == "Fillmore"
    assert strip_venue_at("@Fillmore") == "Fillmore"


# ── studio records, stored rows, the one-off recompute ───────────────────────

def test_a_studio_record_counts_only_artist_year_and_tracks():
    r = _res(venue=None, city=None, state=None, date=(1999, None, None))
    r.kind = "studio"
    assert completeness(r)["missing"] == [] and completeness_band(r) == "green"
    r.artist.value = None
    assert completeness_band(r) == "red"


def test_band_from_meta():
    from app.utils.completeness import band_from_meta
    full = {"artist": "A", "date_text": "2014-07-25", "venue": "V", "city": "C", "state": "OH",
            "track_count": 2, "tracks": [{"n": 1, "title": "One"}, {"n": 2, "title": "Two"}]}
    assert band_from_meta(full) == "green"
    assert band_from_meta(dict(full, venue=None)) == "yellow"
    assert band_from_meta(dict(full, date_text="2014-07")) == "red"
    assert band_from_meta(dict(full, tracks=[{"n": 1, "title": "Track 1"}])) == "yellow"
    assert band_from_meta({}) is None
    assert band_from_meta({"artist": "A", "date_text": "1999", "track_count": 1,
                           "tracks": [{"n": 1, "title": "One"}]}, "studio") == "green"


def test_the_one_off_recompute_is_idempotent_and_leaves_new_rows_alone(tmp_path):
    import json, sqlite3
    from sqlalchemy import create_engine
    from app.utils.schema_upgrades import ensure_bulk_meta_band
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("create table bulk_ingest_item (id integer primary key, meta text, kind text)")
    old = {"artist": "A", "date_text": "2014-07-25", "venue": "V", "city": "C", "state": "OH",
           "track_count": 1, "tracks": [{"n": 1, "title": "One"}], "meta_band": "red"}
    new = dict(old, meta_band="yellow", meta_band_v=2)
    for i, m in enumerate((old, new, None, {}), 1):
        con.execute("insert into bulk_ingest_item values (?,?,?)", (i, None if m is None else json.dumps(m), "live"))
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{p}")
    assert ensure_bulk_meta_band(eng) == 2          # the old row and the empty one
    assert ensure_bulk_meta_band(eng) is None       # nothing left to do
    rows = {r[0]: json.loads(r[1]) if r[1] else None
            for r in sqlite3.connect(p).execute("select id, meta from bulk_ingest_item")}
    assert rows[1]["meta_band"] == "green" and rows[1]["meta_band_v"] == 2
    assert rows[2]["meta_band"] == "yellow"         # a new row is not touched
    assert rows[3] is None and rows[4]["meta_band"] is None


@pytest.mark.parametrize("folder,event", [
    ("Go Kurosawa Live at Big Ears Festival 3-28-26", "Big Ears Festival"),
    ("Go Kurosawa At Big Ears Festival Main Stage 3-28-26", "Big Ears Festival"),
    ("Go Kurosawa Big Ears Festival Downstairs 3-28-26", "Big Ears Festival"),
    ("Go Kurosawa Festival of the Sun 2026-03-28", "Festival of the Sun"),
    ("Go Kurosawa Festivals Greatest Hits 3-28-26", None),
    ("Go Kurosawa Radio Festival Special 3-28-26", None),
    ("Go Kurosawa & Friends Big Ears Festival 2026-03-28", None),
    ("Go Kurosawa and Big Ears Festival 2026-03-28", None),
])
def test_event_from_folder_probes(folder, event):
    assert _kurosawa(folder).event.value == event


def test_no_event_from_the_folder_when_the_artist_extension_fired():
    r = resolve(_artist_scan(info="Ella Fitzgerald", folder_name="Ella Fitzgerald & Joe Pass - 1984-05-03 - Newport Jazz Festival"))
    assert r.artist.value == "Ella Fitzgerald & Joe Pass" and r.event.value is None


# ── the one-off recompute must never stop the app from starting ─────────────

def _db_with(tmp_path, rows, cols="id integer primary key, meta text, kind text"):
    import sqlite3
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute(f"create table bulk_ingest_item ({cols})")
    for r in rows:
        con.execute("insert into bulk_ingest_item (id, meta, kind) values (?,?,?)" if "meta" in cols else
                    "insert into bulk_ingest_item (id) values (?)", r if "meta" in cols else r[:1])
    con.commit(); con.close()
    return p


def test_recompute_without_a_meta_column_or_table_is_a_no_op(tmp_path):
    from sqlalchemy import create_engine
    from app.utils.schema_upgrades import ensure_bulk_meta_band
    p = _db_with(tmp_path, [(1,)], cols="id integer primary key, kind text")
    assert ensure_bulk_meta_band(create_engine(f"sqlite:///{p}")) is None
    import sqlite3
    q = tmp_path / "empty.db"; sqlite3.connect(q).close()
    assert ensure_bulk_meta_band(create_engine(f"sqlite:///{q}")) is None


def test_recompute_skips_bad_row_shapes(tmp_path):
    import json, sqlite3
    from sqlalchemy import create_engine
    from app.utils.schema_upgrades import ensure_bulk_meta_band
    good = {"artist": "A", "date_text": "2014-07-25", "venue": "V", "city": "C", "state": "OH",
            "track_count": 1, "tracks": [{"n": 1, "title": "One"}]}
    rows = [(1, json.dumps({"track_count": "12", "artist": "A"}), "live"),
            (2, json.dumps({"tracks": ["x"], "artist": "A", "track_count": 1}), "live"),
            (3, "not json", "live"), (4, json.dumps([1, 2]), "live"), (5, json.dumps(None), "live"),
            (6, json.dumps(good), "live")]
    p = _db_with(tmp_path, rows)
    assert ensure_bulk_meta_band(create_engine(f"sqlite:///{p}")) is not None
    got = {r[0]: r[1] for r in sqlite3.connect(p).execute("select id, meta from bulk_ingest_item")}
    assert json.loads(got[6])["meta_band"] == "green"
    assert json.loads(got[1])["meta_band_v"] == 2 and json.loads(got[2])["meta_band_v"] == 2
    assert got[3] == "not json"


def test_recompute_on_a_locked_database_returns_without_raising(tmp_path, monkeypatch):
    import json, sqlite3
    from sqlalchemy import create_engine
    import app.utils.schema_upgrades as su
    p = _db_with(tmp_path, [(1, json.dumps({"artist": "A"}), "live")])
    monkeypatch.setattr(su, "_BUSY_TIMEOUT", 0.2)
    lock = sqlite3.connect(p, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        assert su.ensure_bulk_meta_band(create_engine(f"sqlite:///{p}")) is None
    finally:
        lock.execute("ROLLBACK"); lock.close()
    assert "meta_band_v" not in json.loads(sqlite3.connect(p).execute("select meta from bulk_ingest_item").fetchone()[0])


# ── second review: the venue "at" stopwords, the festival name, the lead guard ──

@pytest.mark.parametrize("raw,out", [
    ("At the", ""), ("at The", ""), ("At Last", "At Last"), ("At Home", "At Home"),
    ("At The Door Bar", "The Door Bar"), ("At The Gate", "At The Gate"),
])
def test_strip_venue_at_stopwords(raw, out):
    from app.utils.reader.billing import strip_venue_at
    assert strip_venue_at(raw) == out


@pytest.mark.parametrize("folder", [
    "Phish Greatest Hits Festival 1997-11-22", "Phish Best Of Festival 1997-11-22",
    "Phish Radio Festival 1997-11-22", "Phish Live Songs Festival 1997-11-22",
])
def test_a_record_title_before_festival_is_not_an_event(folder):
    assert resolve(_artist_scan(info="Phish", folder_name=folder)).event.value is None


def test_a_real_festival_name_still_gives_an_event():
    assert resolve(_artist_scan(info="Phish", folder_name="Phish Newport Folk Festival 2019-07-26")).event.value == "Newport Folk Festival"


class _AtlasLeads:
    """Exact place hits for city-named acts, and a venue known by a longer name."""
    def __init__(self, acts=()):
        self.acts = set(acts)

    def venue(self, text, **k):
        if text in ("Boston", "Chicago", "Rush", "Santana", "Hampton Coliseum"):
            return [NS(name=text, score=1.0)]
        if text == "Red Rocks" and k.get("min_score"):
            return [NS(name="Red Rocks Amphitheatre", score=0.6)]
        return []

    def event(self, *a, **k): return []
    def area(self, text, **k): return [NS()] if text in ("Boston", "Chicago", "Kansas") else []
    def artist(self, text, **k): return [NS(extra={"act_kind": "Group"})] if text in self.acts else []


@pytest.mark.parametrize("lead", ["Boston", "Chicago", "Rush", "Santana"])
def test_an_exact_atlas_act_is_not_blocked_by_a_place_of_the_same_name(lead):
    from app.utils.resolve import _lead_is_not_an_act
    assert _lead_is_not_an_act(lead, None, _AtlasLeads(acts=[lead])) is False


def test_an_exact_library_act_is_not_blocked_by_a_place_of_the_same_name():
    from app.utils.reader.library import LibraryIndex
    from app.utils.resolve import _lead_is_not_an_act
    lib = LibraryIndex.from_dicts(artists=["Kansas", "The Band"], venues=["Kansas", "The Band"])
    assert _lead_is_not_an_act("Kansas", lib, None) is False
    assert _lead_is_not_an_act("The Band", lib, None) is False
    assert _lead_is_not_an_act("Kansas", LibraryIndex.from_dicts(venues=["Kansas"]), None) is True


def test_a_place_that_is_not_an_act_is_still_blocked():
    from app.utils.resolve import _lead_is_not_an_act
    assert _lead_is_not_an_act("Hampton Coliseum", None, _AtlasLeads()) is True
    assert _lead_is_not_an_act("Chicago", None, _AtlasLeads()) is True      # a city, not a known act


def test_red_rocks_is_caught_as_the_start_of_a_venue_name():
    from app.utils.resolve import _lead_is_not_an_act
    assert _lead_is_not_an_act("Red Rocks", None, _AtlasLeads()) is True


@pytest.mark.parametrize("lead", ["Various Artists", "Soundcheck", "Newport Jazz Festival"])
def test_generic_and_festival_leads_stay_blocked_even_for_a_known_act(lead):
    from app.utils.reader.library import LibraryIndex
    from app.utils.resolve import _lead_is_not_an_act
    assert _lead_is_not_an_act(lead, LibraryIndex.from_dicts(artists=[lead]), None) is True
