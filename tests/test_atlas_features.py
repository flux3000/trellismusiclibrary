"""
tests/test_atlas_features.py -- the Atlas as evidence in the role decoder (Resolver v2, chunk 4).

The rule under test: the Atlas is weaker evidence than the library, always. It never
speaks for a unit the library has spoken for, adds nothing of a kind the library has
already named in the file, and every one of its weights is below the library's for the
same role. Uses the tiny fixture Atlas; the real Atlas is never opened.
"""
import pytest

from app.atlas import lookup
from app.atlas.build import StaticSources, build_atlas
from app.atlas.lookup import Atlas
from app.utils.reader import weights as W
from app.utils.reader.decode import decode_text
from app.utils.reader.features import build_doc
from app.utils.reader.library import LibraryIndex
from tests.fixtures import atlas_fixture as FX


@pytest.fixture(scope="module")
def atlas(tmp_path_factory):
    p = tmp_path_factory.mktemp("atlas") / "atlas.sqlite"
    FX.build_fixture(p)
    a = Atlas.open(p)
    yield a
    a.close()


@pytest.fixture(autouse=True)
def _reset():
    lookup.reset_atlas()
    yield
    lookup.reset_atlas()


def _feats(text, library=None, atlas=None, hints=None):
    doc = build_doc(text, library, None, hints, atlas)
    return {u.text: u.feats for u in doc.units if u.kind == "seg"}


def _roles(text, library=None, atlas=None, hints=None):
    _, dec = decode_text(text, library, None, hints, atlas)
    return {d.unit.text: d.role for d in dec if d.unit.kind == "seg"}


TEXT = "Al DiMeola\n1983-06-14\nHammersmith Odeon\nLondon, England\n"


# ── weights ──────────────────────────────────────────────────────────────────

_FAMILIES = {      # atlas feature -> the library features it is the weaker twin of
    "atl_artist": ("lib_artist_exact", "lib_artist_core"), "atl_artist_fz": ("lib_artist_exact", "lib_artist_core"),
    "atl_musician": ("lib_musician",),
    "atl_venue": ("lib_venue", "lib_venue_in"), "atl_venue_fz": ("lib_venue", "lib_venue_in"),
    # exact artist + exact day is stronger than a library venue merely appearing inside a segment
    # (lib_venue_in), so the show features answer to lib_venue; they are off wherever the library names one
    "atl_show_place": ("lib_venue",),
    "atl_show_event": ("lib_event",),
    "atl_event": ("lib_event",), "atl_event_fz": ("lib_event",),
}


def test_every_atlas_weight_is_below_the_librarys_for_the_same_role():
    weights = {}
    for feat, role, w in W.EMISSION:
        weights[(feat, role)] = w
    checked = 0
    for (feat, role), w in weights.items():
        if not feat.startswith("atl_") or w <= 0:
            continue
        twins = [weights[(l, role)] for l in _FAMILIES[feat] if (l, role) in weights and weights[(l, role)] > 0]
        assert twins, f"{feat} favours {role}, which its library twin {_FAMILIES[feat]} does not"
        assert w < min(twins), f"{feat} ({w}) must stay below the library's {min(twins)} for {role}"
        checked += 1
    assert checked >= 8


def test_every_atlas_feature_the_code_sets_has_a_weight():
    import inspect
    from app.utils.reader import features
    src = inspect.getsource(features._atlas_features) + inspect.getsource(features._atlas_show_place_keys)
    used = {w for w in ("atl_artist", "atl_artist_fz", "atl_musician", "atl_venue", "atl_venue_fz", "atl_event",
                        "atl_event_fz", "atl_show_place", "atl_show_event") if f'"{w}"' in src}
    weighted = {f for f, _, _ in W.EMISSION if f.startswith("atl_")}
    assert used == weighted


# ── features ─────────────────────────────────────────────────────────────────

def test_atlas_features_appear_for_known_names(atlas):
    f = _feats(TEXT, atlas=atlas)
    assert f["Al DiMeola"].get("atl_artist") == 1.0
    assert f["Hammersmith Odeon"].get("atl_venue") == 1.0


def test_without_an_atlas_there_are_no_atlas_features_even_when_the_app_has_one(atlas):
    lookup.set_atlas(atlas)                       # the app's Atlas is on...
    f = _feats(TEXT)                              # ...but the reader is pure unless handed one
    assert not any(k.startswith("atl_") for fs in f.values() for k in fs)


def test_a_misspelled_name_gets_the_fuzzy_feature(atlas):
    f = _feats("Pat Matheny Group\n1979-06-14\nStars\nPhiladelphia, PA\n", atlas=atlas)
    fz = f["Pat Matheny Group"].get("atl_artist_fz")
    assert fz and 0.85 <= fz < 1.0
    assert "atl_artist" not in f["Pat Matheny Group"]


def test_the_atlas_raises_the_score_of_the_venue_it_knows(atlas):
    _, plain = decode_text(TEXT)
    _, with_atlas = decode_text(TEXT, None, None, None, atlas)
    s = lambda dec: {d.unit.text: d for d in dec if d.unit.kind == "seg"}
    assert s(with_atlas)["Hammersmith Odeon"].role == "VENUE"
    assert s(with_atlas)["Hammersmith Odeon"].score > s(plain)["Hammersmith Odeon"].score
    assert s(with_atlas)["Al DiMeola"].role == "ARTIST"


def test_a_known_name_with_no_venue_word_reads_as_a_venue(atlas):
    text = "Doc Watson\n1999-04-30\nThe Fillmore\nSan Francisco, CA\n"
    assert _feats(text, atlas=atlas)["The Fillmore"].get("atl_venue") == 1.0
    assert _roles(text, atlas=atlas)["The Fillmore"] == "VENUE"


def test_one_word_acts_need_some_history(tmp_path):
    def build(n_rg):
        rgs = [{"id": FX._id(900 + i), "artist-credit": [{"artist": {"id": FX._id(800), "name": "Phish"}}]}
               for i in range(n_rg)]
        art = [{"id": FX._id(800), "name": "Phish", "type": "Group"}]
        p = tmp_path / f"p{n_rg}.sqlite"
        build_atlas(p, StaticSources(artists=art, release_groups=rgs), min_release_groups=1, log=lambda *_: None)
        return Atlas.open(p)
    thin, rich = build(1), build(3)
    assert "atl_artist" not in _feats("Phish\n1995-12-31\n", atlas=thin)["Phish"]
    assert _feats("Phish\n1995-12-31\n", atlas=rich)["Phish"].get("atl_artist") == 1.0


def test_notes_and_track_lines_are_not_looked_up(atlas):
    text = "Al DiMeola\n1983-06-14\n\nRecorded with Hammersmith Odeon mics > DAT\n\n01. Al DiMeola\n02. Hammersmith Odeon\n"
    doc = build_doc(text, None, None, None, atlas)
    for u in doc.units:
        if u.kind != "seg" or u.nb > 0:
            assert not any(k.startswith("atl_") for k in u.feats), u.text


def test_the_show_place_feature_needs_the_date_the_atlas_has(atlas):
    hit = _feats("Hammersmith Odeon\n", atlas=atlas, hints={"artist": "Al Di Meola", "date": (1983, 6, 14)})
    miss = _feats("Hammersmith Odeon\n", atlas=atlas, hints={"artist": "Al Di Meola", "date": (1984, 6, 14)})
    none = _feats("Hammersmith Odeon\n", atlas=atlas)
    assert hit["Hammersmith Odeon"].get("atl_show_place") == 1.0
    assert "atl_show_place" not in miss["Hammersmith Odeon"] and "atl_show_place" not in none["Hammersmith Odeon"]


# ── the library outranks the Atlas ───────────────────────────────────────────

def test_a_unit_the_library_knows_gets_no_atlas_feature(atlas):
    lib = LibraryIndex.from_dicts(artists=["Hammersmith Odeon"])         # the library says it is an act
    f = _feats(TEXT, library=lib, atlas=atlas)["Hammersmith Odeon"]
    assert f.get("lib_artist_exact") == 1.0
    assert not any(k.startswith("atl_") for k in f)


def test_library_venue_in_the_file_silences_atlas_venues_everywhere(atlas):
    lib = LibraryIndex.from_dicts(venues=["Eventim Apollo"])
    text = "Al DiMeola\n1983-06-14\nEventim Apollo\nHammersmith Odeon\nLondon, England\n"
    f = _feats(text, library=lib, atlas=atlas)
    assert f["Eventim Apollo"].get("lib_venue") == 1.0
    assert not any("atl_venue" in fs or "atl_show_place" in fs for fs in f.values())
    assert f["Al DiMeola"].get("atl_artist") == 1.0          # the library said nothing about artists


def test_library_artist_silences_atlas_artists_everywhere(atlas):
    lib = LibraryIndex.from_dicts(artists=["Doc Watson"])
    text = "Al DiMeola\nDoc Watson\n1999-04-30\n"
    f = _feats(text, library=lib, atlas=atlas)
    assert f["Doc Watson"].get("lib_artist_exact") == 1.0
    assert not any(k.startswith("atl_artist") for fs in f.values() for k in fs)


def test_library_wins_when_the_two_disagree_on_the_same_text(atlas):
    # the Atlas says "Hammersmith Odeon" is a venue; this library says it is the act
    lib = LibraryIndex.from_dicts(artists=["Hammersmith Odeon"])
    assert _roles("Hammersmith Odeon\n1983-06-14\nLondon, England\n", library=lib, atlas=atlas)["Hammersmith Odeon"] == "ARTIST"


def test_decoder_results_are_unchanged_when_the_atlas_knows_nothing(atlas):
    text = "Some Unknown Band\n2001-06-02\nPreston Arena\nPreston, CT\n\n01. Song One\n02. Song Two\n"
    assert _roles(text, atlas=atlas) == _roles(text)


# ── chunk 4b: the show index, the fit rule, the country fill ─────────────────

def test_show_index_names_the_place_from_the_texts_own_artist_and_date(atlas):
    from app.utils.ingest import parse_info_file
    text = "Al Di Meola\nHammersmith Odeon\n1983-06-14\n"
    r = parse_info_file(None, text=text, atlas=atlas)
    assert r["venue"] == "Hammersmith Odeon"
    assert r["city"] == "London" and r["country"] == "UK"        # filled from the show's place
    # a partial date asks nothing of the index
    r2 = parse_info_file(None, text="Al Di Meola\nHammersmith Odeon\n1983\n", atlas=atlas)
    assert r2["city"] is None


def test_show_index_features_need_an_exact_day(atlas):
    h = {"show_artist": "Al Di Meola", "show_date": (1983, 6, 14)}
    f = _feats("Al Di Meola\nHammersmith Odeon\n", atlas=atlas, hints=h)
    assert f["Hammersmith Odeon"].get("atl_show_place") == 1.0
    f = _feats("Al Di Meola\nHammersmith Odeon\n", atlas=atlas, hints={"show_artist": "Al Di Meola", "show_date": (1983, 6, None)})
    assert "atl_show_place" not in f["Hammersmith Odeon"]


def test_an_atlas_place_in_another_town_is_no_evidence(atlas):
    f = _feats("Some Band\nHammersmith Odeon\nBoston, MA\n", atlas=atlas)
    assert "atl_venue" not in f["Hammersmith Odeon"]
    f = _feats("Some Band\nHammersmith Odeon\nLondon, England\n", atlas=atlas)
    assert f["Hammersmith Odeon"].get("atl_venue") == 1.0


def test_bare_city_gets_a_country_only_when_it_is_not_a_guess(tmp_path):
    from app.utils.reader.atlas_fill import fill_place
    gn = {"admin1": {"US.CA": "California", "FR.A8": "Ile-de-France", "US.TX": "Texas", "GB.ENG": "England"},
          "cities": [
              {"geonameid": 1, "name": "Paris", "ascii": "Paris", "alts": "", "lat": 48.8, "lon": 2.3, "cc": "FR",
               "admin1": "A8", "population": 2100000},
              {"geonameid": 2, "name": "Paris", "ascii": "Paris", "alts": "", "lat": 33.6, "lon": -95.5, "cc": "US",
               "admin1": "TX", "population": 25000},
              {"geonameid": 3, "name": "Oakland", "ascii": "Oakland", "alts": "", "lat": 37.8, "lon": -122.2,
               "cc": "US", "admin1": "CA", "population": 400000},
              {"geonameid": 4, "name": "Springfield", "ascii": "Springfield", "alts": "", "lat": 1, "lon": 1,
               "cc": "US", "admin1": "TX", "population": 30000},
              {"geonameid": 5, "name": "Springfield", "ascii": "Springfield", "alts": "", "lat": 2, "lon": 2,
               "cc": "GB", "admin1": "ENG", "population": 20000}]}
    build_atlas(tmp_path / "a.sqlite", StaticSources(geonames=gn), log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    assert fill_place(a, "Paris") == (None, "France")           # 2,100,000 against 25,000
    assert fill_place(a, "Oakland") == ("CA", "US")             # one country, one state
    assert fill_place(a, "Springfield") == (None, None)         # two countries of a size: no guess
    assert fill_place(a, "Nowhere") == (None, None) and fill_place(None, "Paris") == (None, None)
    a.close()


def test_bare_city_with_an_atlas_venue_there_takes_that_places_country(atlas):
    from app.utils.reader.atlas_fill import fill_place
    assert fill_place(atlas, "London", "Hammersmith Odeon") == (None, "UK")
