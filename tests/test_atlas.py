"""
tests/test_atlas.py -- the Atlas: its builder, its schema and its reader (Resolver v2, chunk 4).

Every test here runs against the tiny fixture Atlas (tests/fixtures/atlas_fixture.py), built
from hand rows through the real builder. Nothing is downloaded and the real Atlas
(assets/atlas/atlas.sqlite) is never opened: tests/conftest.py turns it off.
"""
import io
import json
import logging
import sqlite3
import tarfile

import pytest

from app.atlas import lookup
from app.atlas import schema as S
from app.atlas.build import (
    BuildError, StaticSources, build_atlas, discover_mb, iter_geonames_alt, iter_tar_json, parse_admin1,
    parse_geonames_city, parse_listing, run_checks,
)
from app.atlas.lookup import Atlas
from tests.fixtures import atlas_fixture as FX


@pytest.fixture(scope="module", params=["trigram", "bigram"])
def atlas_file(request, tmp_path_factory):
    p = tmp_path_factory.mktemp("atlas") / f"{request.param}.sqlite"
    FX.build_fixture(p, force_bigram=request.param == "bigram")
    return p


@pytest.fixture(scope="module")
def atlas(atlas_file):
    a = Atlas.open(atlas_file)
    yield a
    a.close()


@pytest.fixture(autouse=True)
def _reset_app_atlas():
    lookup.reset_atlas()
    yield
    lookup.reset_atlas()


# ── artists ──────────────────────────────────────────────────────────────────

def test_dimeola_finds_al_di_meola(atlas):
    c = atlas.artist("DiMeola")
    assert c and c[0].name == "Al Di Meola"
    assert c[0].score >= 0.85 and c[0].how in ("contained", "squashed", "fuzzy")


def test_pat_matheny_finds_pat_metheny(atlas):
    c = atlas.artist("Pat Matheny")
    assert c and c[0].name == "Pat Metheny"
    assert 0.85 <= c[0].score < 1.0 and c[0].how == "fuzzy"


def test_search_hint_alias_is_an_exact_match(atlas):
    c = atlas.artist("Pat Metheney")
    assert c[0].name == "Pat Metheny" and c[0].how == "exact" and c[0].name_kind == "search_hint"


def test_legal_names_are_not_in_the_atlas(atlas):
    assert atlas.artist("Albert Laurence Di Meola", fuzzy=False) == []
    assert atlas.artist("Patrick Bruce Metheny", fuzzy=False) == []
    names = [r[0] for r in atlas._q("select name from act_name union select name from musician_name")]
    assert not any("Legal" in n or n in ("Armando Anthony Corea",) for n in names)


def test_exact_name_outranks_the_act_core_match(atlas):
    c = atlas.artist("Pat Metheny")
    assert [x.name for x in c][:2] == ["Pat Metheny", "Pat Metheny Group"]
    assert c[0].how == "exact" and c[1].how == "core" and c[0].score > c[1].score


def test_acts_that_do_not_qualify_are_left_out(atlas):
    for name in ("Sherlock Holmes", "[unknown]", "Obscure Garage Band"):
        assert atlas.artist(name, fuzzy=False) == [], name
    assert atlas._q("select count(*) from act")[0][0] == 5


def test_musicians_carry_search_hints_but_not_legal_names(atlas):
    c = atlas.musician("Chic Corea")
    assert c and c[0].name == "Chick Corea"
    assert atlas.musician("Armando Anthony Corea") == []


def test_members_come_with_years_and_instruments_without_modifiers(atlas):
    rtf = atlas.artist("Return to Forever")[0]
    assert atlas.members(rtf.id) == [("Chick Corea", 1971, 1977, "keyboard"), ("Al Di Meola", 1974, 1976, "guitar")]


def test_a_common_word_finds_nothing(atlas):
    assert atlas.artist("Notes", fuzzy=False) == []
    assert atlas.venue("Source", fuzzy=False) == []


# ── places ───────────────────────────────────────────────────────────────────

def test_hammersmith_odeon_is_the_london_place_and_keeps_its_1983_name(atlas):
    c = atlas.venue("Hammersmith Odeon")
    assert len(c) == 1
    v = c[0]
    assert v.name == "Eventim Apollo" and v.matched == "Hammersmith Odeon" and v.name_kind == "former"
    assert (v.start_year, v.end_year) == (1962, 1992)
    assert v.extra["city"] == "London" and v.extra["country"] == "GB"
    assert atlas.place_name_in_year(v.id, 1983) == "Hammersmith Odeon"
    assert atlas.place_name_in_year(v.id, 2000) == "Hammersmith Apollo"
    assert atlas.place_name_in_year(v.id, 2020) == "Eventim Apollo"
    areas = [r[0] for r in atlas._q("select a.name from place_area pa join area a on a.id = pa.area_id "
                                    "where pa.place_id = ?", (v.id,))]
    assert "London" in areas and "United Kingdom" in areas


def test_every_name_a_place_carried_is_found_and_the_place_is_one_row(atlas):
    ids = {atlas.venue(n)[0].id for n in ("Eventim Apollo", "Hammersmith Apollo", "Hammersmith Odeon")}
    assert len(ids) == 1
    # the MusicBrainz place and the Wikidata item (same QID) merged into one row
    assert atlas._q("select count(*) from place where wikidata = 'Q1'")[0][0] == 1
    # the dated Odeon row exists once, from either source
    rows = atlas._q("select start_year, end_year from place_name where key = 'hammersmith odeon'")
    assert rows == [(1962, 1992)]
    # a Wikidata-only alias joined the same place
    assert atlas.venue("Hammersmith Palais Odeon", fuzzy=False)[0].id in ids


def test_wikidata_venue_not_in_musicbrainz_is_added(atlas):
    c = atlas.venue("Beacon Theatre")
    assert c and c[0].extra["city"] == "New York City" and c[0].extra["country"] == "US"


def test_studios_are_not_venues(atlas):
    assert atlas.venue("Sound City Studios", fuzzy=False) == []


def test_place_name_with_a_misspelling_is_found(atlas):
    c = atlas.venue("Hammersmith Odean")
    assert c and c[0].name == "Eventim Apollo" and c[0].how == "fuzzy"


# ── events and areas ─────────────────────────────────────────────────────────

def test_event_place_for_an_act_and_a_day(atlas):
    for who in ("Al Di Meola", "Al DiMeola"):
        r = atlas.event_place(who, (1983, 6, 14))
        assert len(r) == 1 and r[0]["place"] == "Eventim Apollo" and r[0]["city"] == "London"
        assert r[0]["exact"] is True
    assert atlas.event_place("Al Di Meola", {"year": 1983, "month": 6, "day": 14})
    assert atlas.event_place("Al Di Meola", "1983-06-14")
    assert atlas.event_place("Al Di Meola", (1983, 6, 15)) == []
    assert atlas.event_place("Nobody At All", (1983, 6, 14)) == []


def test_event_place_with_a_partial_date_reports_it_is_not_exact(atlas):
    r = atlas.event_place("Al Di Meola", (1983, None, None))
    assert len(r) == 1 and r[0]["exact"] is False
    assert atlas.event_place("Al Di Meola", (1984, None, None)) == []


def test_a_festival_run_covers_each_day_in_it(atlas):
    assert atlas.event_place("Doc Watson", (1999, 5, 1))[0]["place"] == "Wilkes Community College"
    assert atlas.event_place("Doc Watson", (1999, 5, 3)) == []


def test_festival_series_rows_carry_their_series_name(atlas):
    c = atlas.event("Newport Folk")
    assert c and c[0].extra["series"] == "Newport Folk Festival" and c[0].how == "exact"
    assert atlas.event("MerleFest 1999")[0].extra["city"] == "Wilkesboro"


def test_areas_match_by_key_only(atlas):
    c = atlas.area("oakland")
    assert c and c[0].extra["region"] == "California" and c[0].extra["country"] == "US"
    assert atlas.area("Oaktown")[0].name == "Oakland"
    assert atlas.area("Oaklnd") == []          # areas are never fuzzy


# ── the file and the reader ──────────────────────────────────────────────────

def test_the_file_is_read_only(atlas):
    with pytest.raises(sqlite3.OperationalError):
        atlas._con.execute("insert into meta values ('x', 'y')")


def test_meta_records_sources_versions_and_licences(atlas):
    m = atlas.meta
    assert m["schema_version"] == str(S.SCHEMA_VERSION)
    assert m["build_date"].endswith("Z")
    lic = json.loads(m["licences"])
    assert set(lic) == {"musicbrainz", "wikidata", "geonames"}
    assert "CC0" in lic["musicbrainz"] and "CC BY 4.0" in lic["geonames"]
    assert m["fuzzy_mode"] in ("trigram", "bigram")
    assert "Place data from GeoNames (geonames.org), CC BY 4.0." == m["geonames_credit"]
    assert json.loads(m["counts"])["act"] == 5


def test_no_tags_genres_or_ratings_are_stored(atlas):
    tables = {r[0] for r in atlas._q("select name from sqlite_master where type = 'table'")}
    cols = {c[1] for t in tables if not t.startswith(("fts_", "vocab_", "gram")) for c in atlas._q(f"pragma table_info({t})")}
    assert not ({"tag", "tags", "genre", "genres", "rating", "annotation"} & cols)
    assert "jazz fusion" not in json.dumps(atlas._q("select name from act_name"))


def test_sanity_checks_pass_on_the_fixture(atlas_file):
    lines = []
    assert run_checks(atlas_file, printer=lines.append) == 0
    assert any("DiMeola" in ln and "ok" in ln for ln in lines)
    assert any("Hammersmith Odeon" in ln and "ok" in ln for ln in lines)


def test_trigram_and_bigram_files_answer_the_same_lookups(tmp_path):
    a1, a2 = tmp_path / "t.sqlite", tmp_path / "b.sqlite"
    FX.build_fixture(a1)
    FX.build_fixture(a2, force_bigram=True)
    t, b = Atlas.open(a1), Atlas.open(a2)
    for q in ("DiMeola", "Pat Matheny", "Pat Metheny", "Return to Forver", "Doc Watsen", "Nothing Here"):
        assert [(c.name, round(c.score, 3)) for c in t.artist(q)] == [(c.name, round(c.score, 3)) for c in b.artist(q)], q
    assert t.fuzzy_mode == "trigram" and b.fuzzy_mode == "bigram"
    t.close()
    b.close()


def test_two_builds_of_the_same_rows_are_identical_but_for_the_date(tmp_path):
    a1, a2 = tmp_path / "1.sqlite", tmp_path / "2.sqlite"
    FX.build_fixture(a1)
    FX.build_fixture(a2)
    dump = lambda p: [(t, sqlite3.connect(str(p)).execute(f"select * from {t} order by 1, 2").fetchall())
                      for t in S.TABLES if t != "meta"]
    assert dump(a1) == dump(a2)


# ── missing, disabled, unreadable: the reader still works ────────────────────

def test_missing_atlas_returns_nothing_and_logs_once(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv(lookup.ENV_VAR, str(tmp_path / "nope.sqlite"))
    lookup.reset_atlas()
    with caplog.at_level(logging.WARNING, logger="trellis.atlas"):
        assert lookup.current_atlas() is None
        assert lookup.current_atlas() is None
        assert lookup.current_atlas() is None
    assert len([r for r in caplog.records if "not found" in r.getMessage()]) == 1
    state, line = lookup.status()
    assert state == "missing" and line.startswith("atlas missing")


def test_env_off_disables_the_atlas(monkeypatch):
    monkeypatch.setenv(lookup.ENV_VAR, "off")
    assert lookup.atlas_path() is None and lookup.current_atlas() is None
    assert lookup.status()[0] == "disabled"


def test_env_path_is_used(monkeypatch, atlas_file):
    monkeypatch.setenv(lookup.ENV_VAR, str(atlas_file))
    a = lookup.current_atlas()
    assert a is not None and a.artist("Doc Watson")
    state, line = lookup.status()
    assert state == "ok" and line.startswith("atlas ok") and "5 acts" in line


def test_a_file_that_is_not_an_atlas_is_unreadable_not_fatal(tmp_path, caplog, monkeypatch):
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 50)
    monkeypatch.setenv(lookup.ENV_VAR, str(junk))
    lookup.reset_atlas()
    with caplog.at_level(logging.WARNING, logger="trellis.atlas"):
        assert lookup.current_atlas() is None
    assert lookup.status()[0] == "unreadable"


def test_a_newer_schema_is_refused(tmp_path):
    p = tmp_path / "v.sqlite"
    FX.build_fixture(p)
    con = sqlite3.connect(str(p))
    con.execute("update meta set value = '999' where key = 'schema_version'")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        Atlas.open(p)


def test_set_atlas_override_and_reset(atlas_file):
    lookup.set_atlas(atlas_file)
    assert lookup.current_atlas().artist("Doc Watson")
    lookup.set_atlas(None)
    assert lookup.current_atlas() is None
    lookup.reset_atlas()
    assert lookup.current_atlas() is None       # conftest sets TRELLIS_ATLAS=off


# ── builder: parsing and rules, with hand-made inputs ────────────────────────

def _tar_xz(entity, lines, other=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in [("COPYING", b"licence text\n"), (f"mbdump/{entity}", "\n".join(lines).encode())] + (other or []):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_dump_reader_streams_the_entity_file_and_skips_bad_lines():
    iter_tar_json.bad = 0
    recs = list(iter_tar_json(_tar_xz("artist", ['{"id": "a", "name": "A"}', "", "garbage", "{broken", '{"id": "b"}'],
                                      other=[("mbdump/artist-extra", b'{"id": "no"}\n')]), "artist"))
    assert [r["id"] for r in recs] == ["a", "b"] and iter_tar_json.bad == 1


def test_dump_reader_honours_a_limit():
    lines = [json.dumps({"id": str(i)}) for i in range(50)]
    assert len(list(iter_tar_json(_tar_xz("event", lines), "event", limit=7))) == 7


def test_dump_reader_reads_a_path(tmp_path):
    p = tmp_path / "area.tar.xz"
    p.write_bytes(_tar_xz("area", ['{"id": "x"}']).read())
    assert [r["id"] for r in iter_tar_json(p, "area")] == ["x"]


_ROOT = ('<html><body><h1>Index</h1><pre><a href="../">../</a>\n<a href="20260930-001001/">20260930-001001/</a>\n'
         '<a href="20261003-001002/">20261003-001002/</a>\n<a href="LATEST">LATEST</a>\n</pre></body></html>')
_DIR = ('<pre><a href="../">../</a><a href="area.tar.xz">area.tar.xz</a><a href="artist.tar.xz">artist.tar.xz</a>'
        '<a href="event.tar.xz">event.tar.xz</a><a href="place.tar.xz">place.tar.xz</a>'
        '<a href="release-group.tar.xz">release-group.tar.xz</a><a href="SHA256SUMS">SHA256SUMS</a></pre>')


def _fetch(pages):
    def f(url):
        if url not in pages:
            raise BuildError("404 " + url)
        return pages[url]
    return f


def test_discovery_reads_latest_and_lists_the_files():
    base = "https://example.test/json-dumps/"
    pages = {base: _ROOT, base + "LATEST": "20261003-001002\n", base + "20261003-001002/": _DIR}
    rel, files, sums = discover_mb(base, fetch=_fetch(pages))
    assert rel == "20261003-001002" and files["artist"].endswith("20261003-001002/artist.tar.xz")
    assert "release-group" in files and sums.endswith("SHA256SUMS")


def test_discovery_falls_back_to_the_newest_dated_folder():
    base = "https://example.test/json-dumps/"
    pages = {base: _ROOT, base + "20261003-001002/": _DIR}
    assert discover_mb(base, fetch=_fetch(pages))[0] == "20261003-001002"


def test_discovery_stops_clearly_when_the_layout_changed():
    base = "https://example.test/json-dumps/"
    pages = {base: _ROOT, base + "LATEST": "20261003-001002", base + "20261003-001002/": '<a href="artist.json.gz">artist.json.gz</a>'}
    with pytest.raises(BuildError) as e:
        discover_mb(base, fetch=_fetch(pages))
    assert "layout changed" in str(e.value) and "artist.json.gz" in str(e.value)
    with pytest.raises(BuildError):
        discover_mb(base, fetch=_fetch({base: "<html>nothing</html>"}))


def test_listing_parser_ignores_parent_and_query_links():
    assert parse_listing('<a href="?C=N;O=D">Name</a><a href="/pub/">Parent</a><a href="x.tar.xz">x</a>') == ["x.tar.xz"]


def test_geonames_rows_parse():
    cols = ["5378538", "Oakland", "Oakland", "Oaktown", "37.8", "-122.27", "P", "PPLA2", "US", "", "CA", "001",
            "", "", "440646", "", "", "", ""]
    c = parse_geonames_city(cols)
    assert c["name"] == "Oakland" and c["cc"] == "US" and c["admin1"] == "CA" and c["population"] == 440646
    assert parse_geonames_city(["short"]) is None
    assert parse_admin1(["US.CA\tCalifornia\tCalifornia\t5332921\n", "\n"]) == {"US.CA": "California"}
    alt = io.BytesIO(("1\t5378538\ten\tOaktown\t\t\t\t\t\t\n2\t5378538\tfr\tOakland fr\t\n3\t999\ten\tNope\t\n"
                      "4\t5378538\ten\tOld Oakland\t\t\t\t1\t\t\n").encode())
    assert list(iter_geonames_alt(alt, {5378538})) == [(5378538, "Oaktown", "alias"), (5378538, "Old Oakland", "former")]


def _hex(n):
    return f"{n:08x}-0000-4000-8000-000000000000"


def _act_names(path):
    a = Atlas.open(path)
    try:
        return sorted(r[0] for r in a._q("select name from act"))
    finally:
        a.close()


def test_release_group_threshold_shrinks_the_act_set(tmp_path):
    build_atlas(tmp_path / "a.sqlite", FX.sources(), min_release_groups=2, log=lambda *_: None)
    # Return to Forever has two release groups; Pat Metheny and the Group have one each
    assert _act_names(tmp_path / "a.sqlite") == ["Al Di Meola", "Doc Watson", "Return to Forever"]


def test_without_release_groups_acts_qualify_on_events_alone(tmp_path):
    build_atlas(tmp_path / "a.sqlite", FX.sources(release_groups=False), log=lambda *_: None)
    assert _act_names(tmp_path / "a.sqlite") == ["Al Di Meola", "Doc Watson"]


def test_a_sample_build_keeps_every_sampled_artist(tmp_path):
    src = StaticSources(areas=[], places=[], events=[], artists=FX.ARTISTS[:4], sample=4)
    build_atlas(tmp_path / "a.sqlite", src, log=lambda *_: None)
    assert _act_names(tmp_path / "a.sqlite") == ["Al Di Meola", "Pat Metheny", "Pat Metheny Group", "Return to Forever"]


def test_group_without_direction_uses_the_artist_type(tmp_path):
    g, p = _hex(1), _hex(2)
    artists = [{"id": g, "name": "The Test Trio", "type": "Group",
                "relations": [{"type": "member of band", "target-type": "artist", "begin": "1990",
                               "artist": {"id": p, "name": "Pia Test"}}]}]
    build_atlas(tmp_path / "a.sqlite", StaticSources(artists=artists, sample=5), log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    assert a.members(a.artist("The Test Trio")[0].id) == [("Pia Test", 1990, None, None)]
    a.close()


def test_place_types_and_kinds(tmp_path):
    areas = [{"id": _hex(1), "name": "Testland", "type": "Country", "iso-3166-1-codes": ["TL"]}]
    places = [{"id": _hex(10 + i), "name": f"Place {t}", "type": t, "area": {"id": _hex(1)}}
              for i, t in enumerate(["Venue", "Stadium", "Arena", "Indoor arena", "Religious building", "Other", None,
                                     "Studio", "Pressing plant"])]
    build_atlas(tmp_path / "a.sqlite", StaticSources(areas=areas, places=places), log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    kinds = dict(a._q("select name, kind from place"))
    assert "Place Studio" not in kinds and "Place Pressing plant" not in kinds
    assert kinds["Place Stadium"] == "stadium" and kinds["Place Indoor arena"] == "arena" and kinds["Place None"] == "venue"
    assert len(kinds) == 7
    a.close()


def test_area_hierarchy_is_found_with_or_without_direction(tmp_path):
    c, s, t = _hex(1), _hex(2), _hex(3)
    areas = [{"id": c, "name": "Testland", "type": "Country", "iso-3166-1-codes": ["TL"]},
             {"id": s, "name": "Teststate", "type": "Subdivision",
              "relations": [{"type": "part of", "target-type": "area", "area": {"id": c}}]},       # no direction
             {"id": t, "name": "Testville", "type": "City",
              "relations": [{"type": "part of", "target-type": "area", "direction": "backward", "area": {"id": s}}]}]
    places = [{"id": _hex(9), "name": "The Hall", "type": "Venue", "area": {"id": t}}]
    build_atlas(tmp_path / "a.sqlite", StaticSources(areas=areas, places=places), log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    v = a.venue("The Hall")[0]
    assert (v.extra["city"], v.extra["region"], v.extra["country"]) == ("Testville", "Teststate", "TL")
    a.close()


def test_failed_build_leaves_no_half_file(tmp_path):
    class Boom(StaticSources):
        def mb(self, entity):
            if entity == "artist":
                raise BuildError("boom")
            return super().mb(entity)
    out = tmp_path / "atlas.sqlite"
    with pytest.raises(BuildError):
        build_atlas(out, Boom(), log=lambda *_: None)
    assert not out.exists()


def test_the_builder_and_reader_share_one_normaliser():
    from app.utils.reader.library import norm_key
    assert S.norm_key is norm_key
    assert S.squash("Al Di Meola") == S.squash("AL DIMEOLA") == "aldimeola"
    assert S.score_keys("aldimeola", "aldimeola") == (0.97, "squashed")


def test_the_spec_bundles_the_atlas_and_the_smoke_start_reads_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = (root / "trellis.spec").read_text()
    assert "assets" in spec and "atlas.sqlite" in spec and 'datas += [(str(_atlas), "assets/atlas")]' in spec
    assert "_atlas_lookup.status()" in (root / "run.py").read_text()
    assert "atlas ok" in (root / "tools" / "build_macos.sh").read_text()
    assert "assets/atlas/atlas.sqlite" in (root / ".gitignore").read_text()


def test_the_synthetic_atlas_and_the_timing_report_build(tmp_path):
    from app.atlas import synth
    from app.utils import resolver_eval as R
    res = synth.make(tmp_path / "s.sqlite", acts=60, places=5, events=5, log=lambda *_: None)
    assert res["counts"]["act"] == 60
    rep = R.run_tier("G4", "reader")
    text = R.format_timing(rep)
    assert "p95" in text and "slowest" in text


# ── size and rebuild work (chunk 4b) ─────────────────────────────────────────

def test_place_kind_keeps_every_type_but_studios_and_pressing_plants():
    from app.atlas.build import place_kind
    assert place_kind("Concert hall / Theatre") == "hall" and place_kind("Club") == "club"
    assert place_kind("Festival stage") == "stage" and place_kind("Educational institution") == "school"
    assert place_kind(None) == "venue" and place_kind("") == "venue"
    assert place_kind("Studio") is None and place_kind("Pressing plant") is None
    assert place_kind("Some New Type") == "some_new_type"       # a type MusicBrainz adds later is kept


def test_concert_hall_with_a_former_name_is_found_under_it(tmp_path):
    uk, lon, hf = _hex(1), _hex(2), _hex(3)
    areas = [{"id": uk, "name": "United Kingdom", "type": "Country", "iso-3166-1-codes": ["GB"]},
             {"id": lon, "name": "London", "type": "City",
              "relations": [{"type": "part of", "target-type": "area", "direction": "backward", "area": {"id": uk}}]},
             {"id": hf, "name": "Hammersmith", "type": "District",
              "relations": [{"type": "part of", "target-type": "area", "direction": "backward", "area": {"id": lon}}]}]
    places = [{"id": _hex(9), "name": "Eventim Apollo", "type": "Concert hall / Theatre", "area": {"id": hf},
               "aliases": [{"name": "Hammersmith Odeon", "type": "Place name", "begin": "1962", "end": "1992",
                            "ended": True}]},
              {"id": _hex(10), "name": "Some Recording Room", "type": "Studio", "area": {"id": hf}}]
    build_atlas(tmp_path / "a.sqlite", StaticSources(areas=areas, places=places), log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    c = a.venue("Hammersmith Odeon")
    assert c and c[0].name == "Eventim Apollo" and (c[0].start_year, c[0].end_year) == (1962, 1992)
    assert c[0].extra["place_kind"] == "hall" and c[0].extra["country"] == "GB"
    assert a.venue("Some Recording Room") == []
    assert a.place_name_in_year(c[0].id, 1983) == "Hammersmith Odeon"
    a.close()


def test_only_areas_a_kept_place_or_event_sits_in_are_written(tmp_path):
    c, s, t, other, ev_area = (_hex(i) for i in range(1, 6))
    rel = lambda p: [{"type": "part of", "target-type": "area", "direction": "backward", "area": {"id": p}}]
    areas = [{"id": c, "name": "Testland", "type": "Country", "iso-3166-1-codes": ["TL"]},
             {"id": s, "name": "Teststate", "type": "Subdivision", "relations": rel(c)},
             {"id": t, "name": "Testville", "type": "City", "relations": rel(s)},
             {"id": other, "name": "Nobodyville", "type": "City", "relations": rel(s)},
             {"id": ev_area, "name": "Eventburg", "type": "City", "relations": rel(s)}]
    places = [{"id": _hex(9), "name": "The Hall", "type": "Venue", "area": {"id": t}}]
    events = [{"id": _hex(20), "name": "Some Fest", "life-span": {"begin": "1999-07-01"},
               "relations": [{"type": "held in", "target-type": "area", "area": {"id": ev_area}}]}]
    build_atlas(tmp_path / "a.sqlite", StaticSources(areas=areas, places=places, events=events),
                log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    names = {r[0] for r in a._q("select name from area")}
    assert names == {"Testland", "Teststate", "Testville", "Eventburg"}
    assert a.area("Nobodyville") == [] and a.area("Testville")
    assert a.venue("The Hall")[0].extra["city"] == "Testville"
    a.close()


def test_musicians_none_keeps_the_acts_and_empties_the_musician_tables(tmp_path):
    build_atlas(tmp_path / "m.sqlite", FX.sources(), min_release_groups=1, musicians="members", log=lambda *_: None)
    build_atlas(tmp_path / "n.sqlite", FX.sources(), min_release_groups=1, musicians="none", log=lambda *_: None)
    m, n = Atlas.open(tmp_path / "m.sqlite"), Atlas.open(tmp_path / "n.sqlite")
    assert m.counts()["musician"] > 0 and m.counts()["act_member"] > 0
    c = n.counts()
    assert c["musician"] == c["musician_name"] == c["act_member"] == 0
    assert c["act"] == m.counts()["act"] and n.meta["musicians"] == "none"
    assert n.artist("DiMeola") and n.musician("Chick Corea") == []
    m.close()
    n.close()


def test_zero_release_groups_means_events_only(tmp_path):
    build_atlas(tmp_path / "a.sqlite", FX.sources(), min_release_groups=0, log=lambda *_: None)
    assert _act_names(tmp_path / "a.sqlite") == ["Al Di Meola", "Doc Watson"]
    a = Atlas.open(tmp_path / "a.sqlite")
    assert "release groups" not in a.meta["act_rule"]
    a.close()


def test_the_fuzzy_index_is_the_slim_kind_and_still_finds_misspellings(atlas_file):
    con = sqlite3.connect(str(atlas_file))
    sql = {n: s for n, s in con.execute("select name, sql from sqlite_master where name like 'fts_%'")}
    con.close()
    if sqlite3.connect(str(atlas_file)).execute("select value from meta where key='fuzzy_mode'").fetchone()[0] == "trigram":
        assert "detail=none" in sql["fts_act"] and "columnsize=0" in sql["fts_act"]
        assert "fts_act_docsize" not in sql and "fts_musician" not in sql
    a = Atlas.open(atlas_file)
    assert a.artist("Return to Forver")[0].name == "Return to Forever"
    assert a.venue("Hamersmith Odeon") and a.event("MerleFest")
    a.close()


def _offline_cache(root, with_geonames=True):
    import zipfile
    rel = root / "20260101-000000"
    rel.mkdir(parents=True)
    data = {
        "area": [{"id": _hex(1), "name": "Testland", "type": "Country", "iso-3166-1-codes": ["TL"]}],
        "place": [{"id": _hex(2), "name": "The Hall", "type": "Concert hall / Theatre", "area": {"id": _hex(1)}}],
        "event": [{"id": _hex(3), "name": "Test Fest", "life-span": {"begin": "2000-01-01"},
                   "relations": [{"type": "main performer", "target-type": "artist", "artist": {"id": _hex(4)}}]}],
        "artist": [{"id": _hex(4), "name": "Test Band", "type": "Group"}],
    }
    for entity, rows in data.items():
        raw = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
        with tarfile.open(rel / f"{entity}.tar.xz", "w:xz") as tf:
            info = tarfile.TarInfo(f"mbdump/{entity}")
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
    if with_geonames:
        gn = root / "geonames"
        gn.mkdir()
        row = "\t".join(["1", "Testville", "Testville", "", "10.0", "20.0", "P", "PPL", "TL", "", "01", "", "", "",
                         "50000", "", "", "", ""])
        with zipfile.ZipFile(gn / "cities5000.zip", "w") as z:
            z.writestr("cities5000.txt", row + "\n")
        (gn / "admin1CodesASCII.txt").write_text("TL.01\tTeststate\tTeststate\t1\n")
    return root


def test_offline_build_reads_only_the_cache_and_makes_no_request(tmp_path, monkeypatch):
    from app.atlas import build as B
    import urllib.request

    def no_network(*a, **k):
        raise AssertionError("a request was made")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(B, "http_open", no_network)
    cache = _offline_cache(tmp_path / "cache")
    src = B.RemoteSources(cache, offline=True, geonames_alt=False)
    assert src.release == "20260101-000000" and src.wd_status == "skipped" and not src.has_release_groups
    res = build_atlas(tmp_path / "a.sqlite", src, log=lambda *_: None)
    a = Atlas.open(tmp_path / "a.sqlite")
    assert res["counts"]["act"] == 1 and a.artist("Test Band") and a.venue("The Hall")[0].extra["place_kind"] == "hall"
    assert a.area("Testville") and a.meta["source_wikidata"] == "skipped"
    a.close()


def test_offline_stops_and_names_what_the_cache_lacks(tmp_path):
    from app.atlas import build as B
    with pytest.raises(BuildError, match="no dated folder"):
        B.RemoteSources(tmp_path / "empty", offline=True)
    cache = _offline_cache(tmp_path / "cache", with_geonames=False)
    (cache / "20260101-000000" / "place.tar.xz").unlink()
    with pytest.raises(BuildError, match="place.tar.xz"):
        B.RemoteSources(cache, offline=True)
    cache2 = _offline_cache(tmp_path / "cache2", with_geonames=False)
    src = B.RemoteSources(cache2, offline=True, geonames_alt=False)
    with pytest.raises(BuildError, match="cities5000.zip"):
        src.geonames()
    src = B.RemoteSources(cache2, offline=True)
    with pytest.raises(BuildError, match="no-geonames-alt"):
        src.geonames()


def test_a_leftover_building_file_is_emptied_not_a_reason_to_stop(tmp_path, monkeypatch):
    tmp = tmp_path / "a.sqlite.building"
    tmp.write_bytes(b"left over")
    real_unlink = type(tmp).unlink

    def refuse(self, *a, **k):
        if self.name.endswith(".building"):
            raise PermissionError("not permitted")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(type(tmp), "unlink", refuse)
    build_atlas(tmp_path / "a.sqlite", FX.sources(), min_release_groups=1, log=lambda *_: None)
    assert Atlas.open(tmp_path / "a.sqlite").counts()["act"] > 0


def test_trellis_atlas_on_means_the_default_path(monkeypatch):
    for v in ("on", "1", "true"):
        monkeypatch.setenv("TRELLIS_ATLAS", v)
        lookup.reset_atlas()
        assert lookup.atlas_path() == lookup.default_path()
    monkeypatch.setenv("TRELLIS_ATLAS", "off")
    lookup.reset_atlas()
    assert lookup.atlas_path() is None
