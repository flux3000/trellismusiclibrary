"""
tests/test_archive_downloads.py -- Archive Downloads server half (2026-10-01).

No network anywhere: LmaSource gets a fake JSON getter, the worker gets a fake
FETCHER, and the real defaults refuse under TESTING.
"""

import hashlib
import os
import sys
import types

import pytest

from app.extensions import db as _db
from app.models.artist import Artist
from app.models.download_job import DownloadJob
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.user import User
from app.sources import register_source, get_source
from app.sources import lma as lma_mod
from app.sources.lma import LmaSource, build_search_query, parse_source_type, parse_format
from app.sources.base import SourceError
from app.utils import download_queue as dq
from app.utils import node_settings


def md5(b):
    return hashlib.md5(b).hexdigest()


FILES = {"t01.flac": b"flac-one", "t02.flac": b"flac-two", "notes.txt": b"info text"}


def _meta(ident="gd1977-05-08.sbd.miller.flac16", private=(), collection=("etree", "GratefulDead")):
    files = [{"name": n, "source": "original", "format": "Flac" if n.endswith("flac") else "Text",
              "size": str(len(b)), "md5": md5(b), "track": str(i), "title": f"Song {i}",
              "length": "1:30"} for i, (n, b) in enumerate(FILES.items(), 1)]
    for f in files:
        if f["name"] in private:
            f["private"] = "true"
    files.append({"name": "t01.mp3", "source": "derivative", "format": "VBR MP3", "size": "5"})
    files.append({"name": ident + "_meta.xml", "source": "original", "format": "Metadata", "size": "5"})
    return {"metadata": {"identifier": ident, "creator": "Grateful Dead", "date": "1977-05-08",
                         "venue": "Barton Hall", "coverage": "Ithaca, NY",
                         "source": "Soundboard > reel > DAT", "lineage": "L", "taper": "T",
                         "transferer": "X", "item_size": "999", "addeddate": "2020-01-01",
                         "collection": list(collection)},
            "files": files}


def fake_getter(meta):
    calls = []

    def get_json(url):
        calls.append(url)
        if "advancedsearch" in url:
            return {"response": {"numFound": 120, "docs": [
                {"identifier": "gd77-05-08.sbd.x", "creator": "Grateful Dead", "date": "1977-05-08T00:00:00Z",
                 "venue": "Barton Hall", "coverage": "Ithaca, NY", "source": "SBD", "format": ["Flac", "Text", "Flac FingerPrint"],
                 "item_size": 1234, "addeddate": "2020-01-01", "collection": ["etree", "stream_only"]},
                {"identifier": "x2", "creator": ["A", "B"], "date": "1977", "format": ["VBR MP3"]}]}}
        return meta
    return get_json, calls


class FakeSource:
    name = "lma"

    def __init__(self, meta=None, items=None):
        self.meta = meta or _meta()
        self.items = items or []
        self.stream_only = False

    def recent(self, page=1, sort="newest"):
        return [dict(i) for i in self.items], False

    def search(self, q, page=1, sort="newest"):
        return [dict(i) for i in self.items], False

    def item(self, item_id):
        m = self.meta["metadata"]
        return {"source": "lma", "id": item_id, "artist": m["creator"], "date": m["date"],
                "venue": m["venue"], "stream_only": self.stream_only}

    def download_plan(self, item_id):
        return [{"name": f["name"], "url": "http://fake/" + f["name"], "size": int(f["size"]),
                 "md5": f.get("md5")} for f in self.meta["files"]
                if f["source"] == "original" and not f.get("private")
                and not f["name"].endswith("_meta.xml")]


@pytest.fixture()
def dl(app, tmp_path, monkeypatch):
    app.config["LOGIN_DISABLED"] = True
    app.config["IMPORT_DIR"] = str(tmp_path / "Downloads")
    app.config["IMPORT_ROOTS"] = [str(tmp_path)]
    app.config["TRIAGE_DIRS"] = {"backlog": str(tmp_path / "Backlog")}
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(dq, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dq, "PAUSE_BETWEEN_FILES", 0)
    monkeypatch.setattr(dq, "_sleep", lambda s: None)
    fake = FakeSource()
    register_source("lma", fake)
    yield types.SimpleNamespace(client=app.test_client(), fake=fake, root=tmp_path / "Downloads", tmp=tmp_path)
    register_source("lma", None)
    dq._CANCEL.clear()
    dq._CURRENT[0] = None


@pytest.fixture()
def runmod(app, monkeypatch):
    """run.py as a module, wired to the test app. webview is stubbed so the
    import works headless (same pattern as test_first_run_owner_account)."""
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    import run
    monkeypatch.setattr(run, "app", app)
    return run


def _login(client, username="admin"):
    user = _db.session.query(User).filter_by(username=username).first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _fetcher(files, fail=None):
    """FETCHER that serves `files` ({name: bytes}); fail(name) may raise."""
    def fetch(url, sink, should_stop):
        name = url.rsplit("/", 1)[1]
        if fail:
            fail(name)
        data = files[name]
        for i in range(0, len(data), 4):
            sink(data[i:i + 4])
    return fetch


# -- sources -----------------------------------------------------------------

def test_list_parsing(app):
    get_json, _ = fake_getter(_meta())
    items, more = LmaSource(get_json=get_json).recent(1, "newest")
    a, b = items
    assert a["id"] == "gd77-05-08.sbd.x" and a["date"] == "1977-05-08"
    assert a["source_type"] == "SBD" and a["format"] == "FLAC"
    assert a["stream_only"] is True and a["size_bytes"] == 1234
    assert b["artist"] == "A" and b["date"] == "1977" and b["format"] == "MP3"
    assert b["stream_only"] is False and more is True


def test_source_type_and_format_rules():
    assert parse_source_type("matrix of SBD and AUD") == "MTX"
    assert parse_source_type("Audience recording") == "AUD"
    assert parse_source_type("", "gd77.fm.x") == "FM"
    assert parse_source_type("", "gd77.sbd.x") == "SBD"
    assert parse_source_type("unknown", "plain") is None
    assert parse_format(["Shorten", "Text"]) == "SHN"
    assert parse_format(["VBR MP3", "Metadata"]) == "MP3"
    assert parse_format(["Flac FingerPrint"]) is None


def test_item_detail_excludes_private_and_derivatives(app):
    get_json, _ = fake_getter(_meta(private=("t02.flac",)))
    src = LmaSource(get_json=get_json, get_text=lambda u: "INFO")
    item = src.item("gd1977-05-08.sbd.miller.flac16")
    assert item["info_text"] == "INFO"
    assert [t["n"] for t in item["tracks"]] == [1]
    assert item["tracks"][0]["length_s"] == 90
    assert item["audio_bytes"] == len(FILES["t01.flac"])
    assert item["stream_only"] is False and item["lineage"] == "L"
    names = [f["name"] for f in src.download_plan("gd1977-05-08.sbd.miller.flac16")]
    assert names == ["t01.flac", "notes.txt"]


def test_stream_only_detection(app):
    ident = "x"
    for meta in (_meta(collection=("etree", "stream_only")),
                 _meta(private=("t01.flac", "t02.flac"))):
        get_json, _ = fake_getter(meta)
        assert LmaSource(get_json=get_json, get_text=lambda u: "").item(ident)["stream_only"] is True
    meta = _meta()
    meta["metadata"]["access-restricted-item"] = "true"
    get_json, _ = fake_getter(meta)
    assert LmaSource(get_json=get_json, get_text=lambda u: "").item(ident)["stream_only"] is True


def test_search_query_building():
    q = build_search_query("dead barton 1977 1977-05-08")
    assert q.startswith("collection:etree AND ")
    assert "(creator:dead OR venue:dead OR title:dead)" in q
    assert "date:[1977-01-01 TO 1977-12-31]" in q
    assert "date:[1977-05-08 TO 1977-05-08]" in q
    assert "\\(" in build_search_query("a(b") and "\\:" in build_search_query("x:y")
    assert '"AND"' in build_search_query("AND")
    assert build_search_query("") == "collection:etree"


def test_cache_and_expiry(app):
    get_json, calls = fake_getter(_meta())
    now = [1000.0]
    src = LmaSource(get_json=get_json, clock=lambda: now[0])
    src.recent(1, "newest"); src.recent(1, "newest")
    assert len(calls) == 1
    now[0] += 301
    src.recent(1, "newest")
    assert len(calls) == 2
    src.recent(1, "az")
    assert len(calls) == 3


def test_default_http_refuses_under_testing(app):
    with pytest.raises(SourceError):
        LmaSource().recent(1, "newest")


# -- in library --------------------------------------------------------------

def test_in_library_two_recordings_same_show(dl, app):
    perf = _db.session.query(Performance).first()
    _db.session.add(Recording(performance_id=perf.id, source="SBD", folder_path="x2"))
    _db.session.commit()
    dl.fake.items = [
        {"source": "lma", "id": "a", "artist": "bill evans", "date": "1980-02-22"},
        {"source": "lma", "id": "b", "artist": "Bill Evans", "date": "1980-02-23"},
        {"source": "lma", "id": "c", "artist": "Bill Evans", "date": "1980"}]
    _login(dl.client)
    r = dl.client.get("/api/archive/lma/recent").get_json()
    a, b, c = r["items"]
    assert sorted(x["source"] for x in a["in_library"]) == ["AUD", "SBD"]
    assert b["in_library"] == [] and c["in_library"] == []
    assert a["job"] is None and r["has_more"] is False and r["page"] == 1


def test_in_library_is_one_query(dl, app):
    from sqlalchemy import event
    dl.fake.items = [{"source": "lma", "id": str(i), "artist": "Bill Evans",
                      "date": f"1980-02-{i:02d}"} for i in range(1, 20)]
    _login(dl.client)
    stmts = []
    eng = _db.engine
    event.listen(eng, "before_cursor_execute", lambda *a: stmts.append(a[2]))
    dl.client.get("/api/archive/lma/search?q=evans")
    sel = [s for s in stmts if "FROM artist" in s]
    assert len(sel) == 1


def test_archive_endpoints_admin_only_and_errors(dl, app):
    app.config["LOGIN_DISABLED"] = False
    db = _db
    db.session.add(User(username="viewer", role="viewer", is_active=True, password_hash="x"))
    db.session.commit()
    _login(dl.client, "viewer")
    assert dl.client.get("/api/archive/lma/recent").status_code == 403
    assert dl.client.get("/api/downloads/queue").status_code == 403
    _login(dl.client)
    assert dl.client.get("/api/archive/lma/recent?sort=zzz").status_code == 400
    assert dl.client.get("/api/archive/lma/recent?page=x").status_code == 400

    class Down(FakeSource):
        def recent(self, *a, **k):
            raise SourceError("x")
    register_source("lma", Down())
    assert dl.client.get("/api/archive/lma/recent").status_code == 502


# -- queue API ---------------------------------------------------------------

def _enqueue(dl, ident="show1"):
    return dl.client.post("/api/downloads/queue", json={"source": "lma", "id": ident})


def test_enqueue_409_400(dl):
    _login(dl.client)
    r = _enqueue(dl)
    assert r.status_code == 201
    j = r.get_json()
    assert j["status"] == "queued" and j["folder"] == "show1" and j["artist"] == "Grateful Dead"
    assert j["total_bytes"] == sum(len(b) for b in FILES.values())
    assert _enqueue(dl).status_code == 409
    assert dl.client.post("/api/downloads/queue", json={"source": "nope", "id": "x"}).status_code == 400
    assert dl.client.post("/api/downloads/queue", json={"source": "lma", "id": "../x"}).status_code == 400
    assert dl.client.post("/api/downloads/queue", json={"source": "lma"}).status_code == 400
    dl.fake.stream_only = True
    assert _enqueue(dl, "other").status_code == 400


def test_archive_item_shows_job(dl):
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    dl.fake.items = [{"source": "lma", "id": "show1", "artist": "Z", "date": "2000-01-01"}]
    it = dl.client.get("/api/archive/lma/recent").get_json()["items"][0]
    assert it["job"] == {"id": jid, "status": "queued"}


def test_reorder_cancel_retry_remove_pause(dl):
    _login(dl.client)
    ids = [_enqueue(dl, f"s{i}").get_json()["id"] for i in range(3)]
    q = dl.client.get("/api/downloads/queue").get_json()
    assert [j["id"] for j in q["jobs"]] == ids and q["paused"] is False
    assert dl.client.post("/api/downloads/queue/reorder", json={"order": [ids[2], ids[0]]}).status_code == 200
    q = dl.client.get("/api/downloads/queue").get_json()
    assert [j["id"] for j in q["jobs"]] == [ids[2], ids[0], ids[1]]
    assert dl.client.post("/api/downloads/queue/reorder", json={"order": "x"}).status_code == 400
    assert dl.client.post("/api/downloads/queue/reorder", json={"order": [True]}).status_code == 400

    assert dl.client.post(f"/api/downloads/queue/{ids[0]}/cancel").get_json()["status"] == "cancelled"
    assert dl.client.post(f"/api/downloads/queue/{ids[0]}/cancel").status_code == 409
    assert dl.client.post(f"/api/downloads/queue/{ids[0]}/retry").get_json()["status"] == "queued"
    assert dl.client.post(f"/api/downloads/queue/{ids[1]}/retry").status_code == 409

    assert dl.client.delete(f"/api/downloads/queue/{ids[1]}").status_code == 200
    assert _db.session.get(DownloadJob, ids[1]) is None
    assert dl.client.delete("/api/downloads/queue/9999").status_code == 404

    assert dl.client.post("/api/downloads/queue/pause").get_json() == {"paused": True}
    assert dl.client.get("/api/downloads/queue").get_json()["paused"] is True
    assert dl.client.post("/api/downloads/queue/resume").get_json() == {"paused": False}


def test_remove_active_refused_and_done_cleared(dl):
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    job = _db.session.get(DownloadJob, jid)
    job.status = "active"; _db.session.commit()
    assert dl.client.delete(f"/api/downloads/queue/{jid}").status_code == 409
    job.status = "done"; _db.session.commit()
    assert dl.client.delete(f"/api/downloads/queue/{jid}").status_code == 200
    assert _db.session.get(DownloadJob, jid).status == "cleared"
    assert dl.client.get("/api/downloads/queue").get_json()["jobs"] == []


# -- worker ------------------------------------------------------------------

def test_worker_happy_path(dl, monkeypatch):
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    assert dq.process_next() is True
    job = _db.session.get(DownloadJob, jid)
    assert job.status == "done" and job.done_bytes == job.total_bytes > 0
    folder = dl.root / "show1"
    assert sorted(os.listdir(folder)) == ["notes.txt", "t01.flac", "t02.flac"]
    assert (folder / "t01.flac").read_bytes() == FILES["t01.flac"]
    assert dq.process_next() is False


def test_worker_skips_good_files_and_respects_pause(dl, monkeypatch):
    fetched = []
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES, fail=lambda n: fetched.append(n)))
    _login(dl.client)
    _enqueue(dl)
    (dl.root / "show1").mkdir()
    (dl.root / "show1" / "t01.flac").write_bytes(FILES["t01.flac"])
    dq.set_paused(True)
    assert dq.process_next() is False
    dq.set_paused(False)
    assert dq.process_next() is True
    assert "t01.flac" not in fetched and "t02.flac" in fetched


def test_worker_md5_mismatch_keeps_file_and_finishes_done(dl, monkeypatch):
    bad = dict(FILES, **{"t02.flac": b"corrupt!"})
    monkeypatch.setattr(dq, "FETCHER", _fetcher(bad))
    _login(dl.client)
    first = _enqueue(dl, "bad1").get_json()["id"]
    second = _enqueue(dl, "bad2").get_json()["id"]
    dq.process_next()
    j = _db.session.get(DownloadJob, first)
    assert j.status == "done" and "Checksum mismatch: 1 file" in j.error and "t02.flac" in j.error
    names = sorted(os.listdir(dl.root / "bad1"))
    assert names == ["notes.txt", "t01.flac", "t02.flac"]          # kept, no .part
    assert (dl.root / "bad1" / "t02.flac").read_bytes() == b"corrupt!"
    assert _db.session.get(DownloadJob, second).status == "queued"
    f = {x["name"]: x for x in dl.client.get("/api/downloads/folder").get_json()["folders"]}
    assert f["bad1"]["checksums"] == "failed" and f["bad1"]["checksum_errors"] == ["t02.flac"]


def test_checksums_verified_and_null(dl, monkeypatch):
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES))
    _login(dl.client)
    _enqueue(dl, "good1")
    dq.process_next()
    (dl.root / "manual").mkdir()
    # An item whose files carry no MD5 at all is unverifiable, never "verified".
    for f in dl.fake.meta["files"]:
        f.pop("md5", None)
    _enqueue(dl, "nomd5")
    dq.process_next()
    f = {x["name"]: x for x in dl.client.get("/api/downloads/folder").get_json()["folders"]}
    assert f["good1"]["checksums"] == "verified" and f["good1"]["checksum_errors"] == []
    assert f["nomd5"]["checksums"] is None
    assert f["manual"]["checksums"] is None


def test_network_failure_still_fails_and_keeps_folder(dl, monkeypatch):
    def fail(name):
        if name == "t02.flac":
            raise dq.DownloadError("Archive answered 500")
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES, fail=fail))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    dq.process_next()
    assert _db.session.get(DownloadJob, jid).status == "failed"
    assert (dl.root / "show1" / "t01.flac").exists()
    f = {x["name"]: x for x in dl.client.get("/api/downloads/folder").get_json()["folders"]}
    assert f["show1"]["checksums"] is None and f["show1"]["downloading"] is False


def test_cancel_active_removes_whole_folder_even_if_it_existed(dl, monkeypatch):
    """A folder left by an earlier attempt used to survive a cancel."""
    holder = {}

    def fetch(url, sink, should_stop):
        sink(b"abc")
        dq.cancel(_db.session.get(DownloadJob, holder["id"]))
        if should_stop():
            raise dq._Cancelled()
    monkeypatch.setattr(dq, "FETCHER", fetch)
    _login(dl.client)
    (dl.root / "show1").mkdir()
    (dl.root / "show1" / "t01.flac").write_bytes(FILES["t01.flac"])      # finished earlier
    holder["id"] = _enqueue(dl).get_json()["id"]
    dq.process_next()
    assert _db.session.get(DownloadJob, holder["id"]).status == "cancelled"
    assert not (dl.root / "show1").exists()
    f = dl.client.get("/api/downloads/folder").get_json()["folders"]
    assert [x for x in f if x["name"] == "show1"] == []


def test_cancel_queued_removes_folder_but_keeps_completed_download(dl, monkeypatch):
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES))
    _login(dl.client)
    first = _enqueue(dl).get_json()["id"]
    dq.process_next()                                   # done, folder complete
    again = _enqueue(dl).get_json()["id"]                # re-download of the same show
    assert dl.client.post(f"/api/downloads/queue/{again}/cancel").status_code == 200
    assert sorted(os.listdir(dl.root / "show1")) == ["notes.txt", "t01.flac", "t02.flac"]
    # the same protection survives the first job being cleared from the panel
    dl.client.delete(f"/api/downloads/queue/{first}")
    third = _enqueue(dl).get_json()["id"]
    dl.client.post(f"/api/downloads/queue/{third}/cancel")
    assert (dl.root / "show1" / "t01.flac").exists()

    (dl.root / "stray").mkdir()
    (dl.root / "stray" / "a.flac").write_bytes(b"x")
    j = _enqueue(dl, "stray").get_json()["id"]            # never completed
    dl.client.post(f"/api/downloads/queue/{j}/cancel")
    assert not (dl.root / "stray").exists()


def test_downloading_flag_only_for_queued_or_active(dl):
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    (dl.root / "show1").mkdir()
    get = lambda: {x["name"]: x for x in dl.client.get("/api/downloads/folder").get_json()["folders"]}
    assert get()["show1"]["downloading"] is True
    (dl.root / "show1").mkdir(exist_ok=True)
    job = _db.session.get(DownloadJob, jid)
    job.status = "cancelled"; _db.session.commit()
    assert get()["show1"]["downloading"] is False


def test_worker_retries_rate_limit(dl, monkeypatch):
    seen = {"n": 0}

    def fail(name):
        if name == "t01.flac" and seen["n"] == 0:
            seen["n"] += 1
            raise dq.RateLimited(1)
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES, fail=fail))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    dq.process_next()
    assert _db.session.get(DownloadJob, jid).status == "done" and seen["n"] == 1


def test_worker_cancel_removes_created_folder(dl, monkeypatch):
    holder = {}

    def fetch(url, sink, should_stop):
        sink(b"abc")
        dq.cancel(_db.session.get(DownloadJob, holder["id"]))
        if should_stop():
            raise dq._Cancelled()
    monkeypatch.setattr(dq, "FETCHER", fetch)
    _login(dl.client)
    holder["id"] = _enqueue(dl).get_json()["id"]
    dq.process_next()
    assert _db.session.get(DownloadJob, holder["id"]).status == "cancelled"
    assert not (dl.root / "show1").exists()


def test_worker_unsafe_plan_name_fails(dl, monkeypatch):
    dl.fake.meta["files"].append({"name": "../evil", "source": "original", "size": "1", "md5": md5(b"x")})
    monkeypatch.setattr(dq, "FETCHER", _fetcher(dict(FILES, **{"../evil": b"x"})))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    dq.process_next()
    assert _db.session.get(DownloadJob, jid).status == "failed"
    assert not (dl.root / "evil").exists()


def test_default_fetcher_refuses_under_testing(app):
    with pytest.raises(dq.DownloadError):
        dq._http_fetch("http://x", lambda b: None, lambda: False)


def test_resume_on_boot_resets_active(dl, app):
    _login(dl.client)
    a = _enqueue(dl, "r1").get_json()["id"]
    b = _enqueue(dl, "r2").get_json()["id"]
    _db.session.get(DownloadJob, a).status = "active"
    _db.session.get(DownloadJob, b).status = "done"
    _db.session.commit()
    dq.resume_on_boot(app)
    _db.session.expire_all()
    assert _db.session.get(DownloadJob, a).status == "queued"
    assert _db.session.get(DownloadJob, b).status == "done"


# -- folder + delete guard ---------------------------------------------------

def test_folder_listing(dl):
    _login(dl.client)
    (dl.root / "Show A").mkdir()
    (dl.root / "Show A" / "01.flac").write_bytes(b"1234")
    (dl.root / "Show A" / "x.txt").write_bytes(b"12")
    (dl.root / "Show A" / "02.flac.part").write_bytes(b"1")
    (dl.root / ".hidden").mkdir()
    (dl.root / "loose.txt").write_text("x")
    jid = _enqueue(dl, "queued1").get_json()["id"]
    r = dl.client.get("/api/downloads/folder").get_json()
    assert r["path"] == str(dl.root) and r["destinations"] == ["backlog"]
    by = {f["name"]: f for f in r["folders"]}
    assert set(by) == {"Show A"}
    assert by["Show A"]["files"] == 2 and by["Show A"]["size_bytes"] == 7
    assert by["Show A"]["format"] == "FLAC" and by["Show A"]["downloading"] is False
    (dl.root / "queued1").mkdir()
    by = {f["name"]: f for f in dl.client.get("/api/downloads/folder").get_json()["folders"]}
    assert by["queued1"]["downloading"] is True and by["queued1"]["job_id"] == jid


def test_destinations_only_configured(dl, app):
    _login(dl.client)
    app.config["TRIAGE_DIRS"] = {}
    assert dl.client.get("/api/downloads/folder").get_json()["destinations"] == []


def test_fresh_root_uses_downloads_name(runmod, app, tmp_path):
    runmod._apply_trellis_root(tmp_path / "T")
    assert app.config["IMPORT_DIR"].endswith("/T/Downloads")
    assert "Downloads" in runmod.TRELLIS_SUBFOLDERS and "Download" not in runmod.TRELLIS_SUBFOLDERS


def test_imported_library_falls_back_to_home_downloads(runmod, app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    lib = tmp_path / "lib"
    lib.mkdir()
    runmod._apply_imported_library(lib)
    want = str(home / "Downloads" / "Trellis")
    assert app.config["IMPORT_DIR"] == want
    assert want in app.config["IMPORT_ROOTS"]
    assert not (home / "Downloads").exists()          # not created at boot

    from app.utils.downloads_dir import ensure_downloads_dir
    assert ensure_downloads_dir() == want
    assert os.path.isdir(want)

    runmod._apply_imported_library(lib, import_dir=str(tmp_path / "mine"))
    assert app.config["IMPORT_DIR"] == str(tmp_path / "mine")


def test_saved_downloads_folder_that_is_the_library_is_ignored(runmod, app, tmp_path, monkeypatch):
    # Older installs saved the library root as Downloads; Add Recordings must
    # not open on the collection.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    lib = tmp_path / "lib"
    lib.mkdir()
    runmod._apply_imported_library(lib, import_dir=str(lib))
    assert app.config["IMPORT_DIR"] == str(home / "Downloads" / "Trellis")


def test_get_working_folders_reports_effective(runmod, app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(runmod, "_read_trellis_root_marker",
                        lambda: {"mode": "imported", "library_root": str(tmp_path)})
    runmod._apply_imported_library(tmp_path)
    out = runmod.FluxAPI().get_working_folders()
    # Unset folders report their effective path (2026-10-01, every library).
    want = str(tmp_path / "Downloads" / "Trellis")
    assert out["import_dir"] == want
    assert out["effective_import_dir"] == want


def test_add_recordings_browse_opens_at_downloads(dl, app, tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    want = home / "Downloads" / "Trellis"
    app.config["IMPORT_DIR"] = str(want)
    app.config["IMPORT_ROOTS"] = [str(want)]
    _login(dl.client)
    r = dl.client.get("/api/quality/browse")
    assert r.status_code == 200
    assert r.get_json()["path"] == str(want) and want.is_dir()
    # The admin is not bound by IMPORT_ROOTS (2026-10-02), so "Up" reaches the
    # folder holding Downloads/Trellis, as it does for a created library.
    assert r.get_json()["nav_root"] == str(want.parent)


def test_browse_reports_in_library(dl, app, tmp_path):
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["IMPORT_ROOTS"] = [str(lib), str(out)]
    app.config["IMPORT_DIR"] = str(out)
    _login(dl.client)
    assert dl.client.get(f"/api/quality/browse?path={lib / 'sub'}").get_json()["in_library"] is True
    assert dl.client.get(f"/api/quality/browse?path={out}").get_json()["in_library"] is False


# -- review fixes (2026-10-01) -----------------------------------------------

def test_set_working_folder_rejects_library_overlap(runmod, app, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    bk = tmp_path / "bk"
    bk.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)   # the running app's root is what the guard reads
    marker = {"mode": "imported", "library_root": str(lib), "backlog_dir": str(bk)}
    monkeypatch.setattr(runmod, "_read_trellis_root_marker", lambda: dict(marker))
    monkeypatch.setattr(runmod, "_write_marker", lambda d: None)
    api = runmod.FluxAPI()
    assert api.set_working_folder("import_dir", str(lib))["ok"] is False
    assert api.set_working_folder("import_dir", str(lib / "sub"))["ok"] is False
    assert api.set_working_folder("import_dir", str(tmp_path))["ok"] is False   # contains lib
    assert api.set_working_folder("import_dir", str(bk))["ok"] is False
    assert api.set_working_folder("import_dir", str(other))["ok"] is True


def test_busy_check_survives_case_change(dl):
    _login(dl.client)
    _enqueue(dl, "MixedCase")
    (dl.root / "MixedCase").mkdir()
    assert (dl.root / "MixedCase").exists()
    assert dq.is_busy_folder(str(dl.root / "mixedcase")) is True


def test_cancel_after_job_went_active_still_stops(dl):
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    stale = _db.session.get(DownloadJob, jid)          # caller read it as queued
    _db.session.query(DownloadJob).filter_by(id=jid).update({"status": "active"})
    _db.session.commit()
    assert dq.cancel(stale) is True
    assert jid in dq._CANCEL
    assert _db.session.get(DownloadJob, jid).status == "cancelled"


def test_worker_cannot_claim_a_cancelled_job(dl, monkeypatch):
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    dq.cancel(_db.session.get(DownloadJob, jid))
    dq._run_job(jid)
    assert _db.session.get(DownloadJob, jid).status == "cancelled"
    assert not (dl.root / "show1").exists()


def test_destination_resolved_at_run_time(dl, app, monkeypatch):
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    moved = dl.tmp / "Moved"
    moved.mkdir()
    app.config["IMPORT_DIR"] = str(moved)
    dq.process_next()
    job = _db.session.get(DownloadJob, jid)
    assert job.status == "done" and job.dest_path == str(moved / "show1")
    assert (moved / "show1" / "t01.flac").exists() and not (dl.root / "show1").exists()


def test_queue_list_excludes_cancelled(dl):
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    dl.client.post(f"/api/downloads/queue/{jid}/cancel")
    assert dl.client.get("/api/downloads/queue").get_json()["jobs"] == []


def test_failure_commit_error_does_not_strand_job(dl, monkeypatch):
    monkeypatch.setattr(dq, "FETCHER", _fetcher(FILES, fail=lambda n: (_ for _ in ()).throw(dq.DownloadError("boom"))))
    _login(dl.client)
    jid = _enqueue(dl).get_json()["id"]
    real_commit = _db.session.commit
    calls = {"fail": False}

    def flaky():
        if calls["fail"]:
            calls["fail"] = False
            raise RuntimeError("db locked")
        return real_commit()
    # Arm the failure for the commit that records `failed`.
    orig_short = dq._short
    monkeypatch.setattr(dq, "_short", lambda e: (calls.__setitem__("fail", True), orig_short(e))[1])
    monkeypatch.setattr(_db.session, "commit", flaky)
    dq.process_next()                       # must not raise
    monkeypatch.setattr(_db.session, "commit", real_commit)
    _db.session.rollback()
    # Not left in a state that only a manual DB edit can clear: boot resume heals it.
    dq.resume_on_boot(dl.client.application)
    assert _db.session.get(DownloadJob, jid).status in ("queued", "failed")


# -- Move / Ingest refuse a folder still downloading (2026-10-02) -------------

def test_move_refuses_folder_still_downloading(dl):
    _login(dl.client)
    _enqueue(dl, "busyshow")
    (dl.root / "busyshow").mkdir()
    r = dl.client.post("/api/quality/move",
                       json={"folder_path": str(dl.root / "busyshow"), "destination": "backlog"})
    assert r.status_code == 409
    assert (dl.root / "busyshow").is_dir()


def test_move_allows_folder_not_downloading(dl):
    _login(dl.client)
    (dl.root / "idle").mkdir()
    r = dl.client.post("/api/quality/move",
                       json={"folder_path": str(dl.root / "idle"), "destination": "backlog"})
    assert r.status_code == 200
    assert (dl.tmp / "Backlog" / "idle").is_dir()


def test_busy_name_outside_downloads_is_not_refused(dl):
    _login(dl.client)
    _enqueue(dl, "samename")
    other = dl.tmp / "Workshop" / "samename"
    other.mkdir(parents=True)
    assert dq.downloading_here(str(other)) is False
    assert dq.downloading_here(str(dl.root / "samename")) is True


def test_ingest_refuses_folder_still_downloading(dl):
    _login(dl.client)
    _enqueue(dl, "busyingest")
    (dl.root / "busyingest").mkdir()
    folder = str(dl.root / "busyingest")
    r = dl.client.post("/api/ingest/confirm",
                       json={"source_folder_path": folder, "artist_name": "X"})
    assert r.status_code == 409
