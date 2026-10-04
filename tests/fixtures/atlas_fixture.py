"""
A tiny Atlas for tests (Resolver v2, chunk 4).

Built from hand-written rows in the shape of the MusicBrainz JSON dumps, through the
real builder (app/atlas/build.py), so the tests exercise the same code that builds the
shipped file. Nothing is downloaded and the real Atlas is never opened.

    from tests.fixtures.atlas_fixture import build_fixture
    build_fixture(tmp_path / "atlas.sqlite")                 # FTS5 trigram
    build_fixture(tmp_path / "atlas.sqlite", force_bigram=True)

What is in it (names are real, the rows are hand-made and incomplete):
    Hammersmith Odeon   one place, "Eventim Apollo" today; "Hammersmith Odeon" 1962-1992,
                        "Hammersmith Apollo" 1992-2013, in London
    Al Di Meola         an act (alias "Al DiMeola"), with a 1983 show at the Odeon
    Pat Metheny         an act (search hint "Pat Metheney"), and Pat Metheny Group
    Return to Forever   a group, members Al Di Meola and Chick Corea with dates
    Doc Watson          an act with a festival event (MerleFest 1999)
    left out on purpose a studio, a Character, "[unknown]", an act with no events or releases
"""
import zlib

from app.atlas.build import StaticSources, build_atlas


def _id(n):
    return f"{n:08x}-0000-4000-8000-000000000000"


UK, ENG, LON, HF, US, NC, WILKES, CA, SF, NY, NYC = (_id(i) for i in range(1, 12))
P_APOLLO, P_FILLMORE, P_WCC, P_STUDIO = (_id(i) for i in range(100, 104))
A_DIMEOLA, A_RTF, A_METHENY, A_PMG, A_WATSON, A_COREA, A_MAYS = (_id(i) for i in range(200, 207))
A_CHAR, A_UNKNOWN, A_OBSCURE = (_id(i) for i in range(207, 210))
E_ODEON, E_MERLE = _id(300), _id(301)


def _part_of(parent):
    return [{"type": "part of", "target-type": "area", "direction": "backward", "area": {"id": parent}}]


AREAS = [
    {"id": UK, "name": "United Kingdom", "type": "Country", "iso-3166-1-codes": ["GB"]},
    {"id": ENG, "name": "England", "type": "Subdivision", "relations": _part_of(UK)},
    {"id": LON, "name": "London", "type": "City", "relations": _part_of(ENG),
     "aliases": [{"name": "Greater London", "type": "Area name"}]},
    {"id": HF, "name": "London Borough of Hammersmith and Fulham", "type": "District", "relations": _part_of(LON)},
    {"id": US, "name": "United States", "type": "Country", "iso-3166-1-codes": ["US"],
     "aliases": [{"name": "USA", "type": "Area name"}]},
    {"id": NC, "name": "North Carolina", "type": "Subdivision", "relations": _part_of(US)},
    {"id": WILKES, "name": "Wilkesboro", "type": "Municipality", "relations": _part_of(NC)},
    {"id": CA, "name": "California", "type": "Subdivision", "relations": _part_of(US)},
    {"id": SF, "name": "San Francisco", "type": "City", "relations": _part_of(CA)},
    {"id": NY, "name": "New York", "type": "Subdivision", "relations": _part_of(US)},
    {"id": NYC, "name": "New York City", "type": "City", "relations": _part_of(NY)},
]

PLACES = [
    {"id": P_APOLLO, "name": "Eventim Apollo", "type": "Venue", "area": {"id": HF},
     "coordinates": {"latitude": "51.4905", "longitude": "-0.2252"},
     "life-span": {"begin": "1932"},
     "aliases": [
         {"name": "Hammersmith Odeon", "type": "Place name", "begin": "1962", "end": "1992", "ended": True},
         {"name": "Hammersmith Apollo", "type": "Place name", "begin": "1992", "end": "2013", "ended": True},
     ],
     "relations": [{"type": "wikidata", "target-type": "url",
                    "url": {"resource": "https://www.wikidata.org/wiki/Q1"}}]},
    {"id": P_FILLMORE, "name": "The Fillmore", "type": "Venue", "area": {"id": SF},
     "aliases": [{"name": "Fillmore Auditorium", "type": "Place name"}]},
    {"id": P_WCC, "name": "Wilkes Community College", "type": "Other", "area": {"id": WILKES}},
    {"id": P_STUDIO, "name": "Sound City Studios", "type": "Studio", "area": {"id": LON}},
]

EVENTS = [
    {"id": E_ODEON, "name": "Al Di Meola at Hammersmith Odeon", "type": "Concert",
     "life-span": {"begin": "1983-06-14", "end": "1983-06-14"},
     "relations": [
         {"type": "held at", "target-type": "place", "place": {"id": P_APOLLO}},
         {"type": "main performer", "target-type": "artist", "artist": {"id": A_DIMEOLA}}]},
    {"id": E_MERLE, "name": "MerleFest 1999", "type": "Festival",
     "life-span": {"begin": "1999-04-29", "end": "1999-05-02"},
     "relations": [
         {"type": "held at", "target-type": "place", "place": {"id": P_WCC}},
         {"type": "main performer", "target-type": "artist", "artist": {"id": A_WATSON}}]},
]


def _member(person_id, name, begin=None, end=None, attrs=()):
    return {"type": "member of band", "target-type": "artist", "direction": "backward",
            "begin": begin, "end": end, "attributes": list(attrs), "artist": {"id": person_id, "name": name}}


ARTISTS = [
    {"id": A_DIMEOLA, "name": "Al Di Meola", "sort-name": "Di Meola, Al", "type": "Person",
     "life-span": {"begin": "1954-07-22"}, "country": "US",
     "aliases": [{"name": "Al DiMeola", "type": "Artist name"},
                 {"name": "Albert Laurence Di Meola", "type": "Legal name"}],
     "tags": [{"name": "jazz fusion", "count": 5}]},
    {"id": A_RTF, "name": "Return to Forever", "sort-name": "Return to Forever", "type": "Group",
     "relations": [_member(A_DIMEOLA, "Al Di Meola", "1974", "1976", ["guitar"]),
                   _member(A_COREA, "Chick Corea", "1971", "1977", ["keyboard", "original"])]},
    {"id": A_METHENY, "name": "Pat Metheny", "sort-name": "Metheny, Pat", "type": "Person",
     "aliases": [{"name": "Pat Metheney", "type": "Search hint"},
                 {"name": "Patrick Bruce Metheny", "type": "Legal name"}],
     "relations": [{"type": "member of band", "target-type": "artist", "direction": "forward",
                    "artist": {"id": A_PMG, "name": "Pat Metheny Group"}}]},
    {"id": A_PMG, "name": "Pat Metheny Group", "sort-name": "Pat Metheny Group", "type": "Group",
     "relations": [_member(A_METHENY, "Pat Metheny", "1977", None, ["guitar"]),
                   _member(A_MAYS, "Lyle Mays", "1977", "2020", ["keyboard"])]},
    {"id": A_WATSON, "name": "Doc Watson", "sort-name": "Watson, Doc", "type": "Person"},
    {"id": A_COREA, "name": "Chick Corea", "sort-name": "Corea, Chick", "type": "Person",
     "aliases": [{"name": "Chic Corea", "type": "Search hint"},
                 {"name": "Armando Anthony Corea", "type": "Legal name"}],
     "relations": [{"type": "member of band", "target-type": "artist", "direction": "forward",
                    "artist": {"id": A_RTF, "name": "Return to Forever"}}]},
    {"id": A_MAYS, "name": "Lyle Mays", "sort-name": "Mays, Lyle", "type": "Person",
     "relations": [{"type": "member of band", "target-type": "artist", "direction": "forward",
                    "artist": {"id": A_PMG, "name": "Pat Metheny Group"}}]},
    {"id": A_CHAR, "name": "Sherlock Holmes", "type": "Character"},
    {"id": A_UNKNOWN, "name": "[unknown]", "type": None},
    {"id": A_OBSCURE, "name": "Obscure Garage Band", "type": "Group"},
]


def _rg(title, *artists):
    return {"id": _id(zlib.crc32(title.encode()) % 10 ** 6 + 1000), "title": title,
            "artist-credit": [{"name": n, "joinphrase": "", "artist": {"id": i, "name": n}} for i, n in artists]}


RELEASE_GROUPS = [
    _rg("Romantic Warrior", (A_RTF, "Return to Forever")),
    _rg("Where Have I Known You Before", (A_RTF, "Return to Forever")),
    _rg("Elegant Gypsy", (A_DIMEOLA, "Al Di Meola")),
    _rg("Bright Size Life", (A_METHENY, "Pat Metheny")),
    _rg("Still Life (Talking)", (A_PMG, "Pat Metheny Group")),
    _rg("The Adventures", (A_CHAR, "Sherlock Holmes")),
    _rg("Untitled", (A_UNKNOWN, "[unknown]")),
]

WIKIDATA_ITEMS = {
    "Q1": {"qid": "Q1", "label": "Eventim Apollo", "kind": "venue", "lat": 51.49, "lon": -0.225,
           "loc": "London", "cc": "GB", "inception": 1932, "dissolved": None},
    "Q2": {"qid": "Q2", "label": "Beacon Theatre", "kind": "venue", "lat": 40.78, "lon": -73.98,
           "loc": "New York City", "cc": "US", "inception": 1929, "dissolved": None},
    "Q3": {"qid": "Q3", "label": "Newport Folk Festival", "kind": "festival", "lat": None, "lon": None,
           "loc": "Newport", "cc": "US", "inception": 1959, "dissolved": None},
}
WIKIDATA_NAMES = [
    ("Q1", "Hammersmith Odeon", "former", 1962, 1992),
    ("Q1", "Hammersmith Palais Odeon", "alias", None, None),
    ("Q3", "Newport Folk", "alias", None, None),
]

GEONAMES = {
    "cities": [
        {"geonameid": 5378538, "name": "Oakland", "ascii": "Oakland", "alts": "Oaktown", "lat": 37.8044,
         "lon": -122.2712, "cc": "US", "admin1": "CA", "population": 440646},
        {"geonameid": 2643743, "name": "London", "ascii": "London", "alts": "", "lat": 51.5085,
         "lon": -0.1257, "cc": "GB", "admin1": "ENG", "population": 8961989},
    ],
    "admin1": {"US.CA": "California", "GB.ENG": "England"},
    "alt": None,
}


def sources(*, with_wikidata=True, with_geonames=True, release_groups=True):
    return StaticSources(
        areas=AREAS, places=PLACES, events=EVENTS, artists=ARTISTS,
        release_groups=RELEASE_GROUPS if release_groups else None,
        wikidata=(WIKIDATA_ITEMS, WIKIDATA_NAMES) if with_wikidata else None,
        geonames=GEONAMES if with_geonames else None)


def build_fixture(path, *, force_bigram=False, **kw):
    """Build the fixture Atlas at `path`. Returns build_atlas()'s result dict."""
    return build_atlas(path, sources(**kw), min_release_groups=1, force_bigram=force_bigram, log=lambda *_: None)
