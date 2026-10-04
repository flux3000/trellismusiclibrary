"""
tests/test_atlas_wikidata.py -- the Wikidata fetch and the Atlas update that uses it
(Resolver v2, chunk 4). HTTP is mocked; nothing is downloaded and the real Atlas is never opened.
"""
import http.client
import io
import json
import socket
import urllib.error
import urllib.parse
from email.message import Message

import pytest

from app.atlas import build as B
from app.atlas import wikidata as WD
from app.atlas.build import StaticSources, build_atlas, update_wikidata
from app.atlas.lookup import Atlas
from tests.fixtures import atlas_fixture as FX


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(WD, "_sleep", waits.append)
    return waits


class _Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None):
    h = Message()
    if retry_after:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "err", h, io.BytesIO(b""))


def _script(monkeypatch, *steps):
    """Each step is a response body or an exception to raise, consumed in order."""
    calls = []

    def fake(url, headers=None, method="GET", timeout=60):
        calls.append(url)
        step = steps[min(len(calls), len(steps)) - 1]
        if isinstance(step, BaseException):
            raise step
        return _Resp(step)
    monkeypatch.setattr(B, "http_open", fake)
    return calls


# ── retry classification ─────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    http.client.IncompleteRead(b"abc", 10), http.client.RemoteDisconnected("closed"), ConnectionResetError(),
    socket.timeout("slow"), TimeoutError(), json.JSONDecodeError("bad", "x", 0), urllib.error.URLError("dns"),
    _http_error(503), _http_error(429), _http_error(504), http.client.BadStatusLine("x"),
])
def test_transient_failures_are_retryable(exc):
    assert WD.is_retryable(exc)


@pytest.mark.parametrize("exc", [_http_error(400), _http_error(404), _http_error(403), B.BuildError("x"),
                                 KeyError("a")])
def test_real_errors_are_not_retryable(exc):
    assert not WD.is_retryable(exc)


def test_get_json_retries_every_kind_of_dropped_answer_then_succeeds(monkeypatch, _no_sleep):
    calls = _script(monkeypatch, http.client.IncompleteRead(b"x", 5), http.client.RemoteDisconnected("gone"),
                    ConnectionResetError(), socket.timeout("t"), b"{not json", {"ok": 1})
    assert WD.get_json("http://x", "application/json", tries=6) == {"ok": 1}
    assert len(calls) == 6 and len(_no_sleep) == 5


def test_a_truncated_body_during_read_is_retried_not_fatal(monkeypatch):
    class Cut(_Resp):
        def read(self):
            raise http.client.IncompleteRead(b"partial", 100)

    seq = [Cut({}), _Resp({"ok": 2})]
    monkeypatch.setattr(B, "http_open", lambda *a, **k: seq.pop(0))
    assert WD.get_json("http://x", "application/json") == {"ok": 2}


def test_retry_after_is_honoured(monkeypatch, _no_sleep):
    _script(monkeypatch, _http_error(429, retry_after=7), {"ok": 1})
    WD.get_json("http://x", "application/json")
    assert _no_sleep == [7]


def test_a_400_stops_at_once_and_exhausted_retries_raise_wikidata_error(monkeypatch):
    calls = _script(monkeypatch, _http_error(400))
    with pytest.raises(WD.WikidataError):
        WD.get_json("http://x", "application/json")
    assert len(calls) == 1
    calls = _script(monkeypatch, http.client.IncompleteRead(b"", 1))
    with pytest.raises(WD.WikidataError, match="after 3 tries"):
        WD.get_json("http://x", "application/json", tries=3)
    assert len(calls) == 3


def test_maxlag_backs_off_and_the_api_delay_is_never_under_a_second(monkeypatch, _no_sleep):
    _script(monkeypatch, {"error": {"code": "maxlag", "lag": 4}}, {"entities": {"Q1": {"id": "Q1"}}})
    assert list(WD.get_entities(["Q1"], "labels", 0.1, "t")) == ["Q1"]
    assert _no_sleep == [4, 1.0]


# ── the two-pass parser ──────────────────────────────────────────────────────

def _snak(value, kind="wikibase-entityid"):
    return {"snaktype": "value", "datavalue": {"value": value, "type": kind}}


def _claim(value, rank="normal", quals=None, kind="wikibase-entityid"):
    c = {"mainsnak": _snak(value, kind), "rank": rank}
    if quals:
        c["qualifiers"] = {k: [_snak({"time": v}, "time")] for k, v in quals.items()}
    return c


ENTITY = {
    "id": "Q1", "labels": {"en": {"language": "en", "value": "Eventim Apollo"}},
    "aliases": {"en": [{"language": "en", "value": "Hammersmith Apollo"}], "de": [{"language": "de", "value": "Apollo"}]},
    "claims": {
        "P625": [_claim({"latitude": 51.49, "longitude": -0.225}, kind="globecoordinate")],
        "P131": [_claim({"id": "Q84"})],
        "P17": [_claim({"id": "Q145"})],
        "P571": [_claim({"time": "+1932-00-00T00:00:00Z"}, kind="time")],
        "P1448": [_claim({"text": "Hammersmith Odeon", "language": "en"}, quals={"P580": "+1962-00-00T00:00:00Z", "P582": "+1992-00-00T00:00:00Z"}, kind="monolingualtext"),
                  _claim({"text": "Odeon", "language": "en"}, kind="monolingualtext"),
                  _claim({"text": "Apolo", "language": "es"}, kind="monolingualtext"),
                  _claim({"text": "Wrong Name", "language": "en"}, rank="deprecated", kind="monolingualtext")],
        "P2561": [_claim({"text": "Hammersmith Palais", "language": "en"}, kind="monolingualtext")],
    },
}


def test_an_entity_becomes_an_item_and_its_names():
    rec = WD.extract_entity(ENTITY)
    it = rec["item"]
    assert (it["label"], it["lat"], it["lon"], it["inception"], it["_loc"], it["_ctry"]) == \
        ("Eventim Apollo", 51.49, -0.225, 1932, "Q84", "Q145")
    names = {tuple(n) for n in rec["names"]}
    assert ("Hammersmith Odeon", "former", 1962, 1992) in names
    assert ("Hammersmith Apollo", "alias", None, None) in names
    assert ("Odeon", "alias", None, None) in names               # undated official name is only an alias
    assert ("Hammersmith Palais", "alias", None, None) in names
    assert not any(n[0] in ("Apolo", "Wrong Name", "Apollo") for n in rec["names"])   # other language, deprecated


def test_an_entity_with_no_english_label_is_dropped():
    assert WD.extract_entity({"id": "Q2", "labels": {"fr": {"language": "fr", "value": "Salle"}}})["item"] is None


def _wikidata_http(monkeypatch, members=("Q1", "Q2"), counters=None):
    """A fake Wikidata: the class check, subclass and member queries, and wbgetentities."""
    counters = counters if counters is not None else {"sparql": 0, "api": 0}

    def fake(url, headers=None, method="GET", timeout=60):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "query" in q:
            counters["sparql"] += 1
            sq = q["query"][0]
            uri = lambda x: {"value": "http://www.wikidata.org/entity/" + x}
            if "rdfs:label" in sq:
                rows = [{"c": uri(c), "l": {"value": lab}} for c, lab in
                        [("Q1060829", "concert hall"), ("Q8719053", "music venue"), ("Q24354", "theatre"),
                         ("Q483110", "stadium"), ("Q641226", "arena"), ("Q622425", "nightclub"),
                         ("Q868557", "music festival")]]
            elif "wdt:P279*" in sq:
                rows = [{"c": uri("Q1060829")}]
            elif "Q1060829" in sq and "wdt:P31" in sq:
                assert "LIMIT" in sq and "SERVICE" not in sq and "P1448" not in sq      # ids only
                rows = [{"item": uri(m)} for m in members]
            else:
                rows = []
            return _Resp({"results": {"bindings": rows}})
        counters["api"] += 1
        ids = q["ids"][0].split("|")
        assert len(ids) <= 50 and q["maxlag"] == ["5"]
        ents = {}
        for i in ids:
            if i == "Q1":
                ents[i] = ENTITY
            elif i == "Q2":
                ents[i] = {"id": "Q2", "labels": {"en": {"value": "Beacon Theatre"}}, "claims": {
                    "P131": [_claim({"id": "Q84"})], "P17": [_claim({"id": "Q30"})]}}
            elif i == "Q84":
                ents[i] = {"id": "Q84", "labels": {"en": {"value": "London"}}}
            elif i == "Q145":
                ents[i] = {"id": "Q145", "claims": {"P297": [_claim("GB", kind="string")]}}
            elif i == "Q30":
                ents[i] = {"id": "Q30", "claims": {"P297": [_claim("US", kind="string")]}}
            else:
                ents[i] = {"id": i, "missing": ""}
        return _Resp({"entities": ents})
    monkeypatch.setattr(B, "http_open", fake)
    return counters


def test_fetch_runs_the_three_passes_and_assembles_items(monkeypatch, tmp_path):
    c = _wikidata_http(monkeypatch)
    items, names, info = WD.fetch(tmp_path, log=lambda *_: None)
    assert sorted(items) == ["Q1", "Q2"]
    assert (items["Q1"]["loc"], items["Q1"]["cc"], items["Q1"]["kind"]) == ("London", "GB", "venue")
    assert items["Q2"]["cc"] == "US" and "_loc" not in items["Q1"]
    assert ("Q1", "Hammersmith Odeon", "former", 1962, 1992) in names
    assert info["ids"] == 2 and info["items"] == 2 and info["failed"] == []
    assert c["api"] >= 3


def test_a_rerun_resumes_from_the_cache_and_asks_for_nothing_new(monkeypatch, tmp_path):
    _wikidata_http(monkeypatch)
    first = WD.fetch(tmp_path, log=lambda *_: None)
    c = _wikidata_http(monkeypatch)
    again = WD.fetch(tmp_path, log=lambda *_: None)
    assert c["api"] == 0                                           # details and place labels came from the cache
    assert again[0] == first[0] and again[1] == first[1]
    assert WD.cached_estimate(tmp_path, 1.0).startswith("Wikidata estimate")


def test_a_failed_class_is_reported_and_the_rest_are_kept(monkeypatch, tmp_path):
    _wikidata_http(monkeypatch)
    real = B.http_open

    def flaky(url, headers=None, method="GET", timeout=60):
        if "query=" in url and "Q24354" in urllib.parse.unquote(url) and "wdt%3AP31" in url:
            raise http.client.IncompleteRead(b"", 1)
        return real(url, headers, method, timeout)
    monkeypatch.setattr(B, "http_open", flaky)
    items, names, info = WD.fetch(tmp_path, log=lambda *_: None)
    assert "Q1" in items


def test_nothing_fetched_raises_wikidata_error(monkeypatch, tmp_path):
    _script(monkeypatch, http.client.IncompleteRead(b"", 1))
    with pytest.raises(WD.WikidataError):
        WD.fetch(tmp_path, log=lambda *_: None)


# ── a failed source does not stop the build ──────────────────────────────────

class _BrokenWikidata(StaticSources):
    def wikidata(self):
        raise http.client.IncompleteRead(b"x", 9)


def test_the_build_finishes_and_meta_marks_a_failed_wikidata(tmp_path):
    src = _BrokenWikidata(areas=FX.AREAS, places=FX.PLACES, events=FX.EVENTS, artists=FX.ARTISTS,
                          release_groups=FX.RELEASE_GROUPS, geonames=FX.GEONAMES)
    logged = []
    res = build_atlas(tmp_path / "a.sqlite", src, min_release_groups=1, log=logged.append)
    a = Atlas.open(res["path"])
    assert a.meta["source_wikidata"] == "failed" and "IncompleteRead" in a.meta["source_wikidata_reason"]
    assert a.artist("Al DiMeola") and a.area("Oakland")           # MusicBrainz and GeoNames are in
    assert any("WARNING: Wikidata" in m for m in logged)
    a.close()


def test_meta_says_ok_or_skipped_when_it_is_neither_failed(tmp_path):
    build_atlas(tmp_path / "ok.sqlite", FX.sources(), min_release_groups=1, log=lambda *_: None)
    build_atlas(tmp_path / "no.sqlite", FX.sources(with_wikidata=False), min_release_groups=1, log=lambda *_: None)
    for name, want in (("ok", "ok"), ("no", "skipped")):
        a = Atlas.open(tmp_path / f"{name}.sqlite")
        assert a.meta["source_wikidata"] == want and "source_wikidata_reason" not in a.meta
        a.close()


# ── --wikidata-only ──────────────────────────────────────────────────────────

@pytest.fixture(params=[False, True], ids=["trigram", "bigram"])
def bare(request, tmp_path):
    p = tmp_path / "atlas.sqlite"
    FX.build_fixture(p, force_bigram=request.param, with_wikidata=False)
    return p


def _rows(path, sql):
    con = sqlite3_ro(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def sqlite3_ro(path):
    import sqlite3
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _top_name_id(path):
    return _rows(path, "select max(id) from place_name")[0][0]


def _mb_state(path, upto):
    """Everything that is not Wikidata's: MB places, their names up to id `upto`, acts, MB events."""
    return (_rows(path, "select * from place where source = 'musicbrainz' order by id"),
            _rows(path, f"select * from place_name where id <= {upto} order by id"),
            _rows(path, "select * from act order by id"), _rows(path, "select * from event where mbid is not null order by id"))


def test_wikidata_only_adds_wikidata_to_an_atlas_built_without_it(bare):
    top = _top_name_id(bare)
    before = _mb_state(bare, top)
    assert Atlas.open(bare).meta["source_wikidata"] == "skipped"
    items, names = FX.WIKIDATA_ITEMS, FX.WIKIDATA_NAMES
    res = update_wikidata(bare, (items, names), status="ok", log=lambda *_: None)
    a = Atlas.open(bare)
    assert a.meta["source_wikidata"] == "ok" and "wikidata_updated" in a.meta
    assert a.venue("Beacon Theatre") and a.event("Newport Folk Festival")
    assert a.venue("Hammersmith Palais Odeon")                       # a name merged into an MB place
    assert json.loads(a.meta["counts"]) == res["counts"]
    a.close()
    assert _mb_state(bare, top) == before


def test_a_second_run_replaces_only_the_wikidata_rows(bare):
    top = _top_name_id(bare)
    update_wikidata(bare, (FX.WIKIDATA_ITEMS, FX.WIKIDATA_NAMES), log=lambda *_: None)
    mb = _mb_state(bare, top)
    items = {"Q9": {"qid": "Q9", "label": "Brand New Hall", "kind": "venue", "lat": None, "lon": None,
                    "loc": "Leeds", "cc": "GB", "inception": None, "dissolved": None},
             "Q1": FX.WIKIDATA_ITEMS["Q1"]}
    names = [("Q1", "Hammersmith Odeon", "former", 1962, 1992), ("Q1", "The Apollo Hammersmith", "alias", None, None)]
    update_wikidata(bare, (items, names), status="partial", reason=None, log=lambda *_: None)
    a = Atlas.open(bare)
    assert a.meta["source_wikidata"] == "partial"
    assert a.venue("Brand New Hall")
    assert not a.venue("Beacon Theatre") and not a.event("Newport Folk Festival")          # the old run's rows are gone
    assert not [c for c in a.venue("Hammersmith Palais Odeon") if c.score >= 0.95]         # its merged alias too
    assert a.venue("The Apollo Hammersmith")
    a.close()
    assert _mb_state(bare, top) == mb
    assert _rows(bare, "select count(*) from place where source='wikidata'") == [(2 - 1,)]    # Q9 only (Q1 merged into MB)


def test_wikidata_only_leaves_no_temp_file_and_works_with_the_fuzzy_index(bare):
    update_wikidata(bare, (FX.WIKIDATA_ITEMS, FX.WIKIDATA_NAMES), log=lambda *_: None)
    assert not list(bare.parent.glob("*.updating"))
    a = Atlas.open(bare)
    assert a.venue("Beacon Theatr")                                  # a fuzzy hit reaches the new row
    a.close()


def test_a_failed_update_leaves_the_atlas_as_it_was(bare):
    raw = bare.read_bytes()
    bad = {"Q9": {"qid": "Q9", "label": "X"}}                        # missing fields: the add fails
    with pytest.raises(Exception):
        update_wikidata(bare, (bad, []), log=lambda *_: None)
    assert bare.read_bytes() == raw and not list(bare.parent.glob("*.updating"))


def test_wikidata_only_refuses_without_an_atlas_or_during_a_build(tmp_path):
    with pytest.raises(B.BuildError, match="run a build first"):
        update_wikidata(tmp_path / "none.sqlite", ({}, []), log=lambda *_: None)
    p = tmp_path / "atlas.sqlite"
    FX.build_fixture(p, with_wikidata=False)
    (tmp_path / "atlas.sqlite.building").write_bytes(b"")
    with pytest.raises(B.BuildError, match="still writing"):
        update_wikidata(p, ({}, []), log=lambda *_: None)


def test_the_command_stops_cleanly_when_wikidata_cannot_be_fetched(monkeypatch, tmp_path):
    p = tmp_path / "atlas.sqlite"
    FX.build_fixture(p, with_wikidata=False)
    raw = p.read_bytes()

    def boom(*a, **k):
        raise WD.WikidataError("no answer")
    monkeypatch.setattr(WD, "fetch", boom)
    assert B.main(["--wikidata-only", "--out", str(p), "--cache", str(tmp_path / "c")]) == 1
    assert p.read_bytes() == raw
    assert B.main(["--wikidata-only", "--out", str(tmp_path / "missing.sqlite")]) == 1
