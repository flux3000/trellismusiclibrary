"""
tests/test_resolver_corpus.py -- the G2/G3 collectors (app/utils/resolver_corpus) and the
harness rules for those tiers. No network: pages come from tests/fixtures/resolver_corpus/,
the transport and the clock are fakes.
"""
import json
import random
from pathlib import Path

import pytest

from app.utils import resolver_eval as ev
from app.utils.resolver_corpus import bluegrass, common, lma
from app.utils.resolver_corpus import __main__ as cli
from app.utils.resolver_corpus.polite import (BotProtection, FetchError, PoliteFetcher,
                                              RequestCap, retry_after_seconds)

FIX = Path(__file__).parent / "fixtures" / "resolver_corpus"
BASE = "https://gdarchive.net/Bluegrass/"


def fx(name):
    return (FIX / name).read_bytes()


# ── A fake clock, sleep and transport ────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class Net:
    """url -> (status, headers, body); anything else is a 404. Records (time, url)."""
    def __init__(self, clock, pages=None):
        self.clock, self.pages, self.calls = clock, dict(pages or {}), []
        self.script = {}                       # url -> list of (status, headers) to serve first

    def __call__(self, url, headers, timeout, max_bytes):
        self.calls.append((self.clock.now(), url))
        q = self.script.get(url)
        if q:
            st, hd = q.pop(0)
            return st, hd, b""
        st, hd, body = self.pages.get(url, (404, {}, b""))
        return st, hd, body[:max_bytes + 1]


def make(tmp_path, pages=None, **kw):
    clock = Clock()
    net = Net(clock, pages)
    f = PoliteFetcher(tmp_path / "cache", clock=clock.now, sleep=clock.sleep, rng=lambda: 0.0,
                      wall=clock.now, opener=net, user_agent="TestAgent/1", **kw)
    return f, net, clock


def ok(body):
    return (200, {}, body if isinstance(body, bytes) else body.encode())


# ── Catalog parsing ──────────────────────────────────────────────────────────

def catalog():
    return bluegrass.parse_catalog(fx("catalog_sample.htm").decode(), BASE + "blue_AF.htm")


def test_catalog_one_table_per_artist_rows_and_headings():
    shows, stats, _ = catalog()
    by_artist = {}
    for s in shows:
        by_artist.setdefault(s["artist"], []).append(s)
    assert list(by_artist) == ["Tony Rice", "Alison Brown"]       # a one-cell row names the next artist
    first = by_artist["Tony Rice"][0]
    assert first["date_raw"] == "08-29-79"
    assert first["venue_raw"] == "Station Inn, Nashville, TN"
    assert first["source_raw"] == "SBD > Cassette > CD"
    assert first["folder_url"] == "https://gdarchive.net/AppData/Tony%20Rice/79-08-29%20Station%20Inn/"
    assert by_artist["Tony Rice"][1]["venue_raw"].startswith("Grey Fox BGF, Walsh Farm")
    assert stats["rows_no_link"] == 1 and stats["rows_not_folder"] == 1


def test_catalog_header_row_is_not_a_show():
    shows, _, _ = catalog()
    assert all(s["date_raw"] != "Date" for s in shows)
    assert len(shows) == 5          # four distinct folders, one of them listed twice


def _four_pages(first):
    pages = {BASE + "blue_AF.htm": ok(first)}
    for name in ("blue_GL.htm", "blue_MR.htm", "blue_SZ.htm"):
        pages[BASE + name] = ok("<p><a href='https://gdarchive.net/AppData/Zed/'>Zed</a></p><table><tr><td>01-01-80</td>"
                                f"<td>V, C, ST</td><td>AUD</td></tr></table><!-- {name} -->")
    return pages


def test_fetch_catalog_returns_every_dated_row_once(tmp_path):
    page = fx("catalog_sample.htm") + b"<table><tr><td>05-05-85</td><td>No link here, Austin, TX</td><td>AUD</td></tr></table>"
    f, net, _ = make(tmp_path, _four_pages(page))
    rows, st = bluegrass.fetch_catalog(f, log=lambda *_: None)
    assert st["pages"] == 4 and st["duplicates"] >= 1 and st["rows_with_show_link"] == 5
    assert len({bluegrass.row_key(r) for r in rows}) == len(rows)
    assert any(r["folder_url"] is None and r["artist_url"] for r in rows)


def test_catalog_follows_a_frameset_once(tmp_path):
    frame = "<frameset><frame src='inner.htm'></frameset>"
    pages = _four_pages(fx("catalog_sample.htm"))
    pages[BASE + "blue_AF.htm"] = ok(frame)
    pages[BASE + "inner.htm"] = ok(fx("catalog_sample.htm"))
    f, net, _ = make(tmp_path, pages)
    rows, st = bluegrass.fetch_catalog(f, log=lambda *_: None)
    assert any(r["artist"] == "Tony Rice" for r in rows)


# ── Apache listing ───────────────────────────────────────────────────────────

def test_listing_pre_drops_sort_links_and_parent():
    url = "https://gdarchive.net/AppData/Tony%20Rice/79-08-29%20Station%20Inn/"
    ents = bluegrass.parse_listing(url, fx("listing_pre.html").decode())
    names = [e["name"] for e in ents]
    assert names == ["tr79-08-29d1t01.flac", "tr79-08-29d1t02.flac", "tr79-08-29.ffp.txt",
                     "Station Inn info.txt", "huge.txt", "Art"]
    assert not any("?" in e["url"] for e in ents)
    sizes = {e["name"]: e["size"] for e in ents}
    assert sizes["Station Inn info.txt"] == int(2.1 * 1024) and sizes["huge.txt"] == 900 * 1024
    summ = bluegrass.listing_summary(ents)
    assert summ["n_audio"] == 2 and summ["n_subdirs"] == 1 and len(summ["txt"]) == 3


def test_listing_table_format():
    url = "https://gdarchive.net/AppData/Alison%20Brown/92-03-14%20Birchmere/"
    ents = bluegrass.parse_listing(url, fx("listing_table.html").decode())
    assert [e["name"] for e in ents] == ["info.txt", "01.shn"]
    assert ents[0]["size"] == int(3.4 * 1024)


def test_rank_drops_checksum_names_and_oversize():
    url = "https://gdarchive.net/AppData/Tony%20Rice/79-08-29%20Station%20Inn/"
    summ = bluegrass.listing_summary(bluegrass.parse_listing(url, fx("listing_pre.html").decode()))
    ranked = common.rank_text_files(summ["txt"])
    assert [e["name"] for e in ranked] == ["Station Inn info.txt"]     # ffp name and the 900K file are out


def test_checksum_content_is_skipped():
    md5 = "\n".join(f"{i:032x} *{i:02d}.flac" for i in range(1, 9))
    assert common.is_checksum_text(md5)
    assert not common.is_checksum_text("Tony Rice\nStation Inn\nsetlist:\n01 Song\n02 Other\n")
    cands = [{"name": "show.txt", "size": 500}, {"name": "info.txt", "size": 400}]
    texts = {"show.txt": md5, "info.txt": "Tony Rice\nAugust 1979\n"}
    entry, text = common.pick_info_text(cands, lambda e: texts[e["name"]])
    assert entry["name"] == "info.txt" and "Tony Rice" in text


# ── Dates and century ────────────────────────────────────────────────────────

def test_parse_catalog_date():
    assert common.parse_catalog_date("08-29-79") == (8, 29, 79)
    assert common.parse_catalog_date("00-00-76") == (None, None, 76)
    assert common.parse_catalog_date("07-??-04") == (7, None, 4)
    assert common.parse_catalog_date("12-31-1999") == (12, 31, 1999)
    assert common.parse_catalog_date("13-40-79") is None
    assert common.parse_catalog_date("Summer 1976") is None


def test_century_from_folder_then_info_then_rule():
    assert common.resolve_century(79, "tr1979-08-29", "") == (1979, "folder")
    assert common.resolve_century(4, "Grey Fox 2004", "") == (2004, "folder")
    assert common.resolve_century(4, "04-07-16", "Recorded July 16, 1904") == (1904, "info")   # info beats the rule
    assert common.resolve_century(92, "92-03-14 Birchmere", "no year here") == (1992, "inferred")
    assert common.resolve_century(5, "x", "") == (2005, "inferred")        # 00-26 -> 20xx
    assert common.resolve_century(26, "x", "") == (2026, "inferred")
    assert common.resolve_century(27, "x", "") == (1927, "inferred")
    assert common.resolve_century(1999, "x", "") == (1999, "written")


def test_century_ignores_a_year_of_another_two_digits():
    assert common.resolve_century(79, "x", "Transferred 2005, show 1979") == (1979, "info")


# ── Sampling ─────────────────────────────────────────────────────────────────

def test_stable_sample_depends_only_on_the_keys():
    keys = [f"https://gdarchive.net/AppData/A/{i}/" for i in range(500)]
    a = common.stable_sample(keys, 50)
    shuffled = keys[:]
    random.Random(7).shuffle(shuffled)
    assert common.stable_sample(shuffled, 50) == a
    assert common.stable_sample(keys + keys, 50) == a            # duplicates collapse
    assert len(set(a)) == 50
    assert common.stable_sample(keys, 50)[:10] == common.stable_sample(keys, 10)   # a prefix, so --limit is a cut
    assert a != common.stable_sample([k + "x" for k in keys], 50)


# ── Rate limiter ─────────────────────────────────────────────────────────────

def test_one_request_per_interval(tmp_path):
    pages = {f"https://archive.org/metadata/i{n}": ok("{}") for n in range(4)}
    f, net, clock = make(tmp_path, pages)
    for n in range(4):
        f.get(f"https://archive.org/metadata/i{n}")
    times = [t for t, _ in net.calls]
    assert all(b - a >= 2.0 for a, b in zip(times, times[1:]))
    assert f.requests == 4


def test_jitter_is_added(tmp_path):
    pages = {f"https://archive.org/metadata/i{n}": ok("{}") for n in range(3)}
    clock = Clock()
    net = Net(clock, pages)
    f = PoliteFetcher(tmp_path / "c", clock=clock.now, sleep=clock.sleep, rng=lambda: 1.0, opener=net, user_agent="T/1")
    for n in range(3):
        f.get(f"https://archive.org/metadata/i{n}")
    times = [t for t, _ in net.calls]
    assert all(b - a >= 2.5 - 1e-9 for a, b in zip(times, times[1:]))      # 2.0 + 0.5 * rng


def test_retry_after_is_honoured_then_succeeds(tmp_path):
    url = "https://archive.org/metadata/slow"
    f, net, clock = make(tmp_path, {url: ok("{}")})
    net.script[url] = [(429, {"Retry-After": "37"})]
    res = f.get(url)
    assert res.ok and f.requests == 2
    t0, t1 = [t for t, _ in net.calls]
    assert t1 - t0 >= 37


def test_retry_after_http_date():
    clock = Clock()
    assert retry_after_seconds({"retry-after": "Thu, 01 Jan 1970 00:20:10 GMT"}, wall=lambda: 1000.0) == pytest.approx(210.0)
    assert retry_after_seconds({}) is None


def test_5xx_backs_off_and_gives_up(tmp_path):
    url = "https://archive.org/metadata/down"
    f, net, clock = make(tmp_path, {}, max_retries=3)
    net.script[url] = [(503, {})] * 10
    with pytest.raises(FetchError):
        f.get(url)
    assert f.requests == 4                                  # first try + 3 retries
    waits = [s for s in clock.sleeps if s >= 30]
    assert waits == [30.0, 60.0, 120.0]                     # exponential from the base
    assert not f.is_cached(url)                             # a failure is never cached


def test_forbidden_stops_the_run(tmp_path):
    url = "https://gdarchive.net/Bluegrass/blue_AF.htm"
    f, net, _ = make(tmp_path)
    net.script[url] = [(403, {})]
    with pytest.raises(BotProtection):
        f.get(url)
    assert f.requests == 1                                  # no retry, no workaround


def test_challenge_page_stops_the_run(tmp_path):
    url = "https://gdarchive.net/x/"
    f, net, _ = make(tmp_path, {url: ok("<html><title>Just a moment...</title></html>")})
    with pytest.raises(BotProtection):
        f.get(url)


def test_audio_and_foreign_hosts_are_refused(tmp_path):
    f, net, _ = make(tmp_path)
    for bad in ("https://gdarchive.net/AppData/A/x/01.flac", "https://archive.org/download/id/a.SHN",
                "https://gdarchive.net/a/b.mp3", "https://example.com/info.txt", "ftp://archive.org/x"):
        with pytest.raises(ValueError):
            f.get(bad)
    assert net.calls == []


def test_request_cap(tmp_path):
    pages = {f"https://archive.org/metadata/i{n}": ok("{}") for n in range(5)}
    f, net, _ = make(tmp_path, pages, max_requests=2)
    f.get("https://archive.org/metadata/i0")
    f.get("https://archive.org/metadata/i1")
    with pytest.raises(RequestCap):
        f.get("https://archive.org/metadata/i2")


def test_oversize_body_is_not_kept(tmp_path):
    url = "https://gdarchive.net/AppData/A/x/big.txt"
    f, net, _ = make(tmp_path, {url: ok(b"x" * 5000)})
    res = f.get(url, max_bytes=1000)
    assert res.too_large and not res.ok and res.body == b""


def test_resume_skips_everything_cached(tmp_path):
    pages = {"https://archive.org/metadata/a": ok('{"a": 1}'), "https://archive.org/metadata/gone": (404, {}, b"")}
    f1, net1, _ = make(tmp_path, pages)
    f1.get("https://archive.org/metadata/a")
    f1.get("https://archive.org/metadata/gone")
    assert f1.requests == 2
    f2, net2, clock2 = make(tmp_path, pages)                 # a new process, same cache folder
    a = f2.get("https://archive.org/metadata/a")
    g = f2.get("https://archive.org/metadata/gone")
    assert a.ok and a.from_cache and a.body == b'{"a": 1}' and g.status == 404
    assert net2.calls == [] and f2.requests == 0 and f2.cache_hits == 2 and clock2.sleeps == []
    assert f2.cache_size() == 2


def test_refresh_ignores_the_cache(tmp_path):
    pages = {"https://archive.org/metadata/a": ok("{}")}
    f1, net1, _ = make(tmp_path, pages)
    f1.get("https://archive.org/metadata/a")
    f2, net2, _ = make(tmp_path, pages, refresh=True)
    f2.get("https://archive.org/metadata/a")
    assert len(net2.calls) == 1


def test_user_agent_is_sent(tmp_path):
    seen = {}
    clock = Clock()

    def opener(url, headers, timeout, max_bytes):
        seen.update(headers)
        return 200, {}, b"{}"
    f = PoliteFetcher(tmp_path / "c", clock=clock.now, sleep=clock.sleep, opener=opener, user_agent="Agent/9 ( x )")
    f.get("https://archive.org/metadata/a")
    assert seen["User-Agent"] == "Agent/9 ( x )"
    from app.utils.net import USER_AGENT
    assert PoliteFetcher(tmp_path / "d").headers["User-Agent"] == USER_AGENT


# ── G3 end to end on fixtures ────────────────────────────────────────────────

STATION = "https://gdarchive.net/AppData/Tony%20Rice/79-08-29%20Station%20Inn/"
BIRCH = "https://gdarchive.net/AppData/Alison%20Brown/92-03-14%20Birchmere/"


def g3_pages():
    pages = {BASE + "blue_AF.htm": ok(fx("catalog_sample.htm"))}
    for name in ("blue_GL.htm", "blue_MR.htm", "blue_SZ.htm"):
        pages[BASE + name] = ok("<p>nothing here</p>")
    pages[STATION] = ok(fx("listing_pre.html"))
    pages[STATION + "Station%20Inn%20info.txt"] = ok(
        "Tony Rice\nStation Inn, Nashville, TN\nAugust 29, 1979\nSBD\n01 Song\n02 Other\n")
    pages[BIRCH] = ok(fx("listing_table.html"))
    pages[BIRCH + "info.txt"] = ok("Alison Brown\nThe Birchmere\nSetlist\n01 One\n")
    return pages


def test_g3_collect_writes_corpus(tmp_path):
    f, net, _ = make(tmp_path, g3_pages())
    logs = []
    meta = bluegrass.collect(f, tmp_path / "out", log=logs.append)
    lines = [json.loads(l) for l in (tmp_path / "out" / "g3.jsonl").read_text().splitlines()]
    by_folder = {i["folder_name"]: i for i in lines}
    assert set(by_folder) == {"79-08-29 Station Inn", "92-03-14 Birchmere"}
    st = by_folder["79-08-29 Station Inn"]
    assert st["gold"] == {"artist": "Tony Rice", "date": {"y": 1979, "m": 8, "d": 29},
                          "venue_raw": "Station Inn, Nashville, TN", "source": "SBD > Cassette > CD"}
    assert st["n_audio"] == 2 and st["archive_meta"]["century"] == "info"
    assert st["archive_meta"]["info_file"] == "Station Inn info.txt"
    bi = by_folder["92-03-14 Birchmere"]
    assert bi["gold"]["date"] == {"y": 1992, "m": 3, "d": 14} and bi["archive_meta"]["century_inferred"]
    assert bi["gold"]["artist"] == "Alison Brown"
    assert meta["kept"] == 2 and meta["skipped"] == {"no_listing": 2, "duplicate_folder": 1, "no_artist_folder": 2} and meta["complete"] is True
    assert meta["century_inferred"] == 1 and "smallest SHA-1" in meta["sample_rule"]
    saved = json.loads((tmp_path / "out" / "g3.meta.json").read_text())
    assert saved["n"] == 2 and saved["tier"] == "G3" and saved["date"]
    # never an audio request, only listings and .txt files
    assert not any(u.lower().endswith((".flac", ".shn", ".mp3")) for _, u in net.calls)
    assert any(l.startswith("[") for l in logs) or any("wrote" in l for l in logs)


def test_g3_rerun_is_free_and_identical(tmp_path):
    pages = g3_pages()
    f1, net1, _ = make(tmp_path, pages)
    bluegrass.collect(f1, tmp_path / "out", log=lambda *_: None)
    first = (tmp_path / "out" / "g3.jsonl").read_text()
    f2, net2, _ = make(tmp_path, pages)
    bluegrass.collect(f2, tmp_path / "out", log=lambda *_: None)
    assert net2.calls == [] and (tmp_path / "out" / "g3.jsonl").read_text() == first


def test_g3_request_cap_writes_a_partial_corpus(tmp_path):
    f, net, _ = make(tmp_path, g3_pages(), max_requests=10)
    meta = bluegrass.collect(f, tmp_path / "out", log=lambda *_: None)
    assert meta["complete"] is False and "cap" in meta["stopped"]


def test_g3_limit_cuts_the_sample(tmp_path):
    f, net, _ = make(tmp_path, g3_pages())
    meta = bluegrass.collect(f, tmp_path / "out", limit=1, log=lambda *_: None)
    assert meta["sample_size"] == 1


def test_g3_plan_counts_and_fetches_nothing(tmp_path):
    f, net, _ = make(tmp_path, g3_pages())
    text = bluegrass.plan(f, 1000)
    assert net.calls == []
    assert "network requests still needed" in text and "catalog not fetched yet" in text
    est = 4 + 600 + int(round(1000 * bluegrass.EST_REQUESTS_PER_SHOW))
    assert str(est) in text
    bluegrass.fetch_catalog(f, log=lambda *_: None)
    text2 = bluegrass.plan(f, 1000)
    assert "rows in the catalog" in text2 and "4 of 4" in text2


def test_cli_plan_prints_without_fetching(tmp_path, capsys):
    assert cli.main(["g3", "--plan", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "G3 plan" in out and "network requests still needed" in out
    assert cli.main(["g2", "--plan", "--out", str(tmp_path)]) == 0
    assert "G2 plan" in capsys.readouterr().out
    assert not (tmp_path / "g3.jsonl").exists()


# ── G2 ───────────────────────────────────────────────────────────────────────

IDENT = "gd77-05-08.sbd.miller.97065.flac16"


def g2_pages():
    pages = {lma.scrape_url(): ok(fx("ia_scrape_page1.json")),
             lma.scrape_url("CURSOR2"): ok(fx("ia_scrape_page2.json")),
             lma.metadata_url(IDENT): ok(fx("ia_metadata.json"))}
    pages["https://archive.org/download/" + IDENT + "/gd77-05-08.sbd.miller.97065.txt"] = ok(
        "Grateful Dead\nBarton Hall\nIthaca, NY\n5/8/77\n")
    return pages


def test_g2_enumerate_pages(tmp_path):
    f, net, _ = make(tmp_path, g2_pages())
    ids = lma.enumerate_ids(f, log=lambda *_: None)
    assert len(ids) == 5 and ids[0] == IDENT
    assert "mediatype%3Aetree" in lma.scrape_url()


def test_g2_item_from_metadata_fixture():
    data = json.loads(fx("ia_metadata.json"))
    item = lma.build_item(IDENT, data, "text", "info.txt")
    assert item["gold"] == {"artist": "Grateful Dead", "date": {"y": 1977, "m": 5, "d": 8},
                            "venue": "Barton Hall, Cornell University", "coverage_raw": "Ithaca, NY",
                            "source": "SBD"}
    assert item["n_audio"] == 2 and item["durations"] == [312, 312]
    assert item["folder_name"] == IDENT and item["archive_meta"]["noisy"] is True
    assert item["archive_meta"]["taper"] == "Betty Cantor-Jackson"
    assert lma.info_candidates(data["files"]) == [
        {"name": "gd77-05-08.sbd.miller.97065.ffp.txt", "size": 900},
        {"name": "gd77-05-08.sbd.miller.97065.txt", "size": 2100}]


def test_g2_collect_picks_txt_not_checksum_and_never_audio(tmp_path):
    pages = g2_pages()
    f, net, _ = make(tmp_path, pages)
    meta = lma.collect(f, tmp_path / "out", log=lambda *_: None)
    lines = [json.loads(l) for l in (tmp_path / "out" / "g2.jsonl").read_text().splitlines()]
    assert [i["id"] for i in lines] == [IDENT]
    assert lines[0]["archive_meta"]["info_file"] == "gd77-05-08.sbd.miller.97065.txt"
    assert meta["skipped"]["no_metadata"] == 3 and meta["collection_items"] == 5 and meta["complete"]
    assert not any(u.lower().endswith((".flac", ".shn", ".mp3")) for _, u in net.calls)


def test_g2_plan(tmp_path):
    f, net, _ = make(tmp_path, g2_pages())
    t = lma.plan(f, 1000)
    assert net.calls == [] and "G2 plan" in t and "unknown" in t
    lma.enumerate_ids(f, log=lambda *_: None)
    assert "items in the collection" in lma.plan(f, 1000) and "5" in lma.plan(f, 1000)


# ── Harness: G3 rules ────────────────────────────────────────────────────────

RAW = "Grey Fox BGF, Walsh Farm, Oak Hill, NY"


def test_g3_venue_one_string_and_event_plus_venue():
    j = ev.judge_catalog_venue
    assert j("Station Inn, Nashville, TN", "Station Inn", None, "Nashville", "TN") == "right"
    assert j("Station Inn, Nashville, TN", "The Station Inn", None) == "right"          # one comma part
    assert j(RAW, "Walsh Farm", None) == "right"                                         # venue matches a part
    assert j(RAW, None, "Grey Fox BGF") == "right"                                       # event matches a part
    assert j(RAW, "Grey Fox", None) == "partial"
    assert j(RAW, "Town Park", "Telluride Bluegrass Festival") == "wrong"
    assert j(RAW, None, None) == "empty"
    assert j("", "Anything", None) is None
    assert j("Station Inn, Nashville, TN", "Nashville") == "right"


def test_g3_city_and_state_must_appear_in_the_string():
    j = ev.judge_in_string
    assert j("city", "Nashville", "Station Inn, Nashville, TN") == "right"
    assert j("city", "Franklin", "Station Inn, Nashville, TN") == "wrong"
    assert j("city", None, "Station Inn, Nashville, TN") == "empty"
    assert j("city", "Franklin", "Station Inn") is None            # nothing to check against
    assert j("state", "Tennessee", "Station Inn, Nashville, TN") == "right"
    assert j("state", "TN", "Station Inn, Nashville, TN") == "right"
    assert j("state", "KY", "Station Inn, Nashville, TN") == "wrong"
    assert j("state", "KY", "Station Inn") is None


def test_g3_artist_act_key_and_variants():
    j = ev.judge_artist_archive
    assert j("Tony Rice", "Tony Rice") == "right"
    assert j("Tony Rice Unit", "Tony Rice") == "partial"            # a different billing is not waved through
    assert j("Del McCoury Band", "The Del McCoury Band") == "right"
    assert j("Seldom Scene", "The Seldom Scene Band") == "variant"
    assert j("Bill Monroe & His Blue Grass Boys", "Bill Monroe") == "variant"
    assert j("Tony Rice", "Doc Watson") == "wrong"


def test_g3_date_century_inferred_is_not_scored_on_century_alone():
    g = {"y": 1905, "m": 3, "d": 14}
    assert ev.judge_date_archive(g, (2005, 3, 14), century_inferred=True) is None
    assert ev.judge_date_archive(g, (2005, 3, 14), century_inferred=False) == "wrong"
    assert ev.judge_date_archive(g, (1905, 3, 14), century_inferred=True) == "right"
    assert ev.judge_date_archive(g, (1905, 4, 14), century_inferred=True) == "wrong"
    assert ev.judge_date_archive({"y": 1976, "m": None, "d": None}, (1976, 5, 5)) == "wrong"   # invented month


def test_g2_loose_venue_and_coverage():
    assert ev.judge_loose_venue("Barton Hall, Cornell University", "Barton Hall") == "partial"
    assert ev.judge_loose_venue("Barton Hall", "barton hall") == "right"
    assert ev.judge_loose_venue("Barton Hall", "Winterland") == "wrong"
    assert ev.judge_in_string("city", "Ithaca", "Ithaca, NY", needs_location_part=False) == "right"
    assert ev.judge_in_string("state", "New York", "Ithaca, New York, United States", needs_location_part=False) == "right"


def test_judge_item_routes_by_tier():
    item = {"gold": {"artist": "Tony Rice", "date": {"y": 1979, "m": 8, "d": 29},
                     "venue_raw": "Station Inn, Nashville, TN", "source": "SBD > Cassette"}}
    pred = {"artist": "Tony Rice", "date": (1979, 8, 29), "venue": "Station Inn", "event": None,
            "city": "Nashville", "state": "TN", "country": "US", "source": "SBD", "stage": None}
    j = ev.judge_item("G3", item, pred)
    assert (j["artist"], j["date"], j["venue"], j["city"], j["state"]) == ("right",) * 5
    assert j["country"] is None and j["event"] is None
    # the same item scored as G1 would have no venue key, so G1 behaviour is untouched
    assert ev.judge_item("G1", item, pred)["venue"] is None


def _write_tier(tmp_path, tier, items):
    (tmp_path / f"{tier.lower()}.jsonl").write_text("\n".join(json.dumps(i) for i in items) + "\n")


def test_run_tier_g3_reader_report(tmp_path):
    text = "Tony Rice\nStation Inn, Nashville, TN\nAugust 29, 1979\nSBD\n"
    items = [{"id": "a", "info_text": text, "folder_name": "79-08-29 Station Inn",
              "gold": {"artist": "Tony Rice", "date": {"y": 1979, "m": 8, "d": 29},
                       "venue_raw": "Station Inn, Nashville, TN", "source": "SBD > Cassette"}},
             {"id": "b", "info_text": "nothing useful\n", "folder_name": "x",
              "gold": {"artist": "Alison Brown", "date": {"y": 1992, "m": 3, "d": 14},
                       "venue_raw": "The Birchmere, Alexandria, VA"},
              "archive_meta": {"century_inferred": True}}]
    _write_tier(tmp_path, "G3", items)
    rep = ev.run_tier("G3", "reader", corpus_dir=tmp_path)
    assert rep["tier"] == "G3" and rep["n"] == 2 and rep["errors"] == 0
    assert rep["fields"]["artist"]["n"] == 2
    assert rep["fields"]["country"]["n"] == 0                      # the catalog has no country
    out = ev.format_report(rep)
    assert out.startswith("G3") and "bluegrassarchive.com catalog" in out


def test_run_tier_g2_is_labelled_noisy(tmp_path):
    items = [{"id": IDENT, "info_text": "Grateful Dead\nBarton Hall\nIthaca, NY\n5/8/77\n", "folder_name": IDENT,
              "gold": {"artist": "Grateful Dead", "date": {"y": 1977, "m": 5, "d": 8},
                       "venue": "Barton Hall, Cornell University", "coverage_raw": "Ithaca, NY", "source": "SBD"}}]
    _write_tier(tmp_path, "G2", items)
    rep = ev.run_tier("G2", "reader", corpus_dir=tmp_path)
    assert rep["tier"] == "G2" and "NOISY" in ev.format_report(rep)
    assert rep["fields"]["artist"]["n"] == 1


def test_missing_g3_message_points_at_the_collector(tmp_path):
    with pytest.raises(FileNotFoundError, match="resolver_corpus"):
        ev.run_tier("G3", "reader", corpus_dir=tmp_path)


# ── The real catalog pages (excerpt, saved 2026-10-04) ────────────────────────

def test_real_excerpt_rows_are_found_though_almost_none_link_to_a_folder():
    html = common.decode_text((FIX / "catalog_real_excerpt.htm").read_bytes())    # cp1252, no </body></html>
    rows = bluegrass.catalog_rows(html)
    assert len(rows) == 27
    assert {r["artist"] for r in rows} >= {"Giant Mountains Band", "Gillian Welch / Gillian Welch and Friends"}
    assert all(r["artist_href"].startswith("https://gdarchive.net/AppData/") for r in rows)
    assert sum(1 for r in rows if r["href"]) == 0
    assert "John Hartford Memorial Festival, Bean Blossom, In" in [r["venue_raw"] for r in rows]   # newline in the cell collapsed
    assert any(r["source_raw"] == "SBD>Tascam DR-40>SD>Audacity>Flac" for r in rows)


def test_real_structure_artist_link_text_wins_over_trailing_text():
    html = ('<p><a href="https://gdarchive.net/AppData/0039/"><span><strong>0039</strong></span></a>'
            '<span><strong> (formerly Bononia Grass)</strong></span></p><table><TBODY><tr><td>Date</td><td>Venue</td><td>Source</td></tr>'
            '<tr><td><a href="https://gdarchive.net/AppData/0039/x.flac16/">05-19-12</a></td><td>EWOB, Voorthuizen, Netherlands'
            '<td>Edirol&gt;Flac</tr></TBODY></table>'
            '<p><a href="https://gdarchive.net/AppData/10%20String%20Symphony/">10 \r\n\t\tString Symphony</a></p>'
            '<table><tr><td>Date<td>Venue<td>Source<tr><td>08-18-18<td>Somewhere, TN<td>AUD</table>')
    rows = bluegrass.catalog_rows(html)                          # unclosed <td> and <tr> throughout
    assert [(r["artist"], r["date_raw"], r["venue_raw"]) for r in rows] == [
        ("0039", "05-19-12", "EWOB, Voorthuizen, Netherlands"),
        ("10 String Symphony", "08-18-18", "Somewhere, TN")]


# ── Row -> folder matching (the live pages link almost no row to its folder) ──

def test_folder_dates_read_every_common_spelling():
    fd = bluegrass.folder_dates
    assert (1979, 8, 29) in fd("1979-08-29 Station Inn")
    assert (79, 8, 29) in fd("79-08-29 Station Inn")            # YY-MM-DD
    assert (79, 8, 29) in fd("08-29-79 Station Inn")            # MM-DD-YY
    assert (1979, 8, 29) in fd("tr19790829.sbd.flac16")
    assert (2012, 5, 19) in fd("0039BG2012-05-19.edirol.marc.flac16")
    assert (1979, 8, 29) in fd("Station Inn 08.29.1979")
    assert fd("Marin Bluegrass Festival 74") == set()


def test_folder_matches_row_date():
    m = bluegrass.folder_matches
    assert m("0039BG2012-05-19.edirol", 5, 19, 12)
    assert m("79-08-29 Station Inn", 8, 29, 79)
    assert m("08-29-1979", 8, 29, 79)
    assert not m("79-08-30 Station Inn", 8, 29, 79)
    assert not m("1989-08-29", 8, 29, 79)                      # same month and day, other year
    assert not m("79-08-29", None, None, 79)                    # a year alone never matches


def _row(artist_url, date_raw, venue="V, C, ST", source="AUD"):
    return {"artist": "A", "artist_url": artist_url, "date_raw": date_raw, "venue_raw": venue,
            "source_raw": source, "folder_url": None, "catalog_page": "blue_AF.htm"}


A_URL = "https://gdarchive.net/AppData/Tony%20Rice/"


def _entries(*names):
    return [{"name": n, "url": A_URL + n.replace(" ", "%20") + "/", "is_dir": True, "size": None} for n in names]


def test_match_folder_single_ambiguous_and_missing():
    ents = _entries("79-08-29 Station Inn", "79-09-01 Elsewhere", "80-01-01 A", "01-01-80 B")
    counts = {}
    assert bluegrass.match_folder(_row(A_URL, "08-29-79"), ents, counts) == (
        A_URL + "79-08-29%20Station%20Inn/", None)
    assert bluegrass.match_folder(_row(A_URL, "01-01-80"), ents, counts) == (None, "ambiguous_folders")
    assert bluegrass.match_folder(_row(A_URL, "02-02-81"), ents, counts) == (None, "no_folder")
    assert bluegrass.match_folder(_row(A_URL, "00-00-76"), ents, counts) == (None, "partial_date")


def test_two_catalog_rows_on_one_date_match_nothing():
    rows = [_row(A_URL, "08-29-79", source="SBD"), _row(A_URL, "08-29-79", source="AUD")]
    counts = bluegrass.date_counts_of(rows)
    assert bluegrass.match_folder(rows[0], _entries("79-08-29 Station Inn"), counts) == (None, "ambiguous_rows")


def test_sample_is_over_rows_and_stable():
    rows = [_row(A_URL, f"{(i % 12) + 1:02d}-{(i % 27) + 1:02d}-{70 + i % 30}", venue=f"V{i}") for i in range(400)]
    a = bluegrass.select_sample(rows, 50)
    b = bluegrass.select_sample(list(reversed(rows)), 50)
    assert [bluegrass.row_key(r) for r in a] == [bluegrass.row_key(r) for r in b]
    assert [bluegrass.row_key(r) for r in bluegrass.select_sample(rows, 50, limit=7)] == [bluegrass.row_key(r) for r in a[:7]]


UNLINKED = ("<p><a href='https://gdarchive.net/AppData/Tony Rice/'>Tony Rice / Tony Rice Unit</a></p><table>"
            "<tr><td>Date<td>Venue<td>Source"
            "<tr><td>08-29-79<td>Station Inn, Nashville, TN<td>SBD"
            "<tr><td>01-01-80<td>Somewhere, TN<td>AUD"
            "<tr><td>02-02-81<td>Nowhere, TN<td>AUD"
            "<tr><td>03-03-82<td>Station Inn, Nashville, TN<td>AUD"
            "<tr><td>03-03-82<td>Station Inn, Nashville, TN<td>SBD</table>")


def test_g3_collect_matches_unlinked_rows_through_the_artist_folder(tmp_path):
    pages = _four_pages(UNLINKED.encode())
    names = ["79-08-29 Station Inn", "80-01-01 A", "01-01-80 B", "82-03-03 Early"]
    listing = "<pre>" + "\n".join(
        '<a href="%s/">x</a>  01-Jan-2010 10:00  -' % n.replace(" ", "%20") for n in names) + "\n</pre>"
    pages[A_URL] = ok(listing)
    pages[STATION] = ok(fx("listing_pre.html"))
    pages[STATION + "Station%20Inn%20info.txt"] = ok("Tony Rice\nStation Inn, Nashville, TN\nAugust 29, 1979\n")
    f, net, _ = make(tmp_path, pages)
    meta = bluegrass.collect(f, tmp_path / "out", log=lambda *_: None)
    lines = [json.loads(l) for l in (tmp_path / "out" / "g3.jsonl").read_text().splitlines()]
    tony = [i for i in lines if i["gold"]["artist"].startswith("Tony Rice")]
    assert [i["folder_name"] for i in tony] == ["79-08-29 Station Inn"]
    assert tony[0]["gold"]["artist"] == "Tony Rice / Tony Rice Unit"
    assert meta["skipped"]["ambiguous_folders"] == 1 and meta["skipped"]["no_folder"] == 1
    assert meta["skipped"]["ambiguous_rows"] == 2 and meta["unmatched_rows"] >= 4
    assert [u for _, u in net.calls].count(A_URL) == 1          # one listing per artist, however many rows


def test_g3_artist_heading_with_slashes_scores_each_billing():
    j = ev.judge_artist_archive
    assert j("Del McCoury / Del McCoury Band / Del & Friends", "Del McCoury Band") == "right"
    assert j("Bela Fleck / Flecktones / Friends", "Bela Fleck") in ("right", "variant")
    assert j("Bela Fleck / Flecktones", "Doc Watson") == "wrong"


def test_metadata_url_for_identifier_ending_in_audio_ext_is_allowed():
    from app.utils.resolver_corpus.polite import PoliteFetcher
    import inspect, tempfile, pathlib
    sig = inspect.signature(PoliteFetcher)
    kw = {}
    if "cache_dir" in sig.parameters:
        kw["cache_dir"] = pathlib.Path(tempfile.mkdtemp())
    if "hosts" in sig.parameters:
        kw["hosts"] = ("archive.org",)
    f = PoliteFetcher(**kw)
    f.check_url("https://archive.org/metadata/ween2000-06-29.dvda.flac")
    import pytest
    with pytest.raises(ValueError):
        f.check_url("https://archive.org/download/x/track01.flac")
