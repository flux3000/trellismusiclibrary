"""
tests/test_bulk_ingest_review_first.py -- Review First ('hold') mode: run mode
defaults, ready/moved statuses, pre-ingest Listening Quality, person-driven
ingest through the worker, blanket values, queue persistence, migration.

Same style as test_bulk_ingest_any_source.py: real FLACs under tmp dirs,
discover()/process() driven directly, no worker thread.
"""

import sqlite3

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.bulk_ingest import BulkIngestRun, BulkIngestItem
from app.models.performance import Performance
from app.models.artist import Artist
from app.models.quality import QualityAnalysis, RecordingQuality
from app.models.recording import Recording
from app.models.user import User
from app.utils import bulk_ingest_run as bir
from app.utils import quality as quality_mod


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    tags.setdefault("TRACKNUMBER", "1")
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _good(root, name, day=8):
    _flac(root / name / "01.flac", ARTIST="Grateful Dead",
          DATE=f"1977-05-{day:02d}", VENUE=f"Hall {name}")


def _flagged(root, name):
    _flac(root / name / "01.flac", ARTIST="Phish")        # needs_date


def _never_stop():
    return False


@pytest.fixture()
def env(app, tmp_path, monkeypatch, seeded_ids):
    allowed = tmp_path / "allowed"
    lib = allowed / "lib"
    incoming = allowed / "incoming"
    lib.mkdir(parents=True)
    incoming.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["IMPORT_ROOTS"] = [str(allowed)]
    app.config["IMPORT_DIR"] = str(incoming)
    app.config["LOGIN_DISABLED"] = True
    monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id, run_id=None: True)
    monkeypatch.setattr(bir, "_start_worker", lambda *a, **k: None)

    # Stand-in for the audio decode: a fixed score, counting how often it ran.
    calls = {"n": 0}

    def fake_features(path):
        calls["n"] += 1
        return {"sampled": {}, "analysis_version": quality_mod.QUALITY_ANALYSIS_VERSION}

    monkeypatch.setattr(quality_mod, "extract_recording_features", fake_features)
    monkeypatch.setattr(quality_mod, "score_recording",
                        lambda features, source=None: {"listening_quality": 77.0,
                                                       "score_tone": 70.0,
                                                       "score_noise": 80.0,
                                                       "score_dynamics": 75.0,
                                                       "score_version": 1})
    return type("Env", (), dict(lib=lib, incoming=incoming, score_calls=calls))


def _run(root, mode="hold"):
    run = BulkIngestRun(root=str(root), status="running", mode=mode)
    _db.session.add(run)
    _db.session.commit()
    return run


def _item(run, rel):
    return (_db.session.query(BulkIngestItem)
            .filter_by(run_id=run.id, rel_path=rel).first())


def _admin(client):
    user = _db.session.query(User).filter_by(username="admin").first()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _hold_run(env, *names, flagged=()):
    for n in names:
        _good(env.incoming, n, day=10 + len(n))
    for n in flagged:
        _flagged(env.incoming, n)
    run = _run(env.incoming)
    bir.discover(run)
    bir.process(run, _never_stop)
    return _db.session.get(BulkIngestRun, run.id)


# ── mode ────────────────────────────────────────────────────────────────────

def test_mode_defaults_and_idempotent_start(app, env):
    # One Queue, one mode (Ryan, 2026-10-06): the first source takes its
    # placement's default, a later source joins the Queue's mode, and an
    # explicit mode applies to the whole Queue.
    c = app.test_client()
    _admin(c)
    _good(env.incoming, "S1")
    first = c.post("/api/bulk-ingest/start", json={}).get_json()
    assert first["mode"] == "auto"
    out = c.post("/api/bulk-ingest/start", json={"path": str(env.incoming)}).get_json()
    assert out["mode"] == "auto"
    # Starting an active root again returns the same run.
    again = c.post("/api/bulk-ingest/start", json={"path": str(env.incoming)}).get_json()
    assert again["id"] == out["id"]
    sub = env.lib / "Sub"
    sub.mkdir()
    out = c.post("/api/bulk-ingest/start", json={"path": str(sub), "mode": "hold"}).get_json()
    assert out["mode"] == "hold"
    assert {r.mode for r in _db.session.query(BulkIngestRun)
            .filter(BulkIngestRun.status.in_(("running", "paused"))).all()} <= {"hold"}
    assert c.post("/api/bulk-ingest/start",
                  json={"path": str(env.incoming), "mode": "bogus"}).status_code == 400


# ── hold processing ─────────────────────────────────────────────────────────

def test_hold_run_leaves_items_ready_scored_and_library_empty(env):
    before = _db.session.query(Recording).count()
    run = _hold_run(env, "S1", flagged=("F1",))
    assert run.status == "done"
    ready, flagged = _item(run, "S1"), _item(run, "F1")
    assert ready.status == "ready" and ready.kind == "live"
    assert flagged.status == "review" and "needs_date" in flagged.reason
    assert _db.session.query(Recording).count() == before
    assert (env.incoming / "S1").is_dir()                       # nothing moved
    row = _db.session.query(QualityAnalysis).filter(
        QualityAnalysis.folder_path.like("%/S1")).first()
    assert row.listening_quality == 77.0 and row.recording_id is None
    assert env.score_calls["n"] == 2              # Needs Review is scored too (Ryan, 2026-10-06)


def test_hold_reuses_current_analysis(env):
    _good(env.incoming, "S1")
    run = _run(env.incoming)
    bir.discover(run)
    bir.process(run, _never_stop)
    assert env.score_calls["n"] == 1
    # Second run over the same folder: the staging row is current, no decode.
    item = _item(run, "S1")
    item.status = "pending"
    _db.session.commit()
    run.status = "running"
    _db.session.commit()
    bir.process(run, _never_stop)
    assert _item(run, "S1").status == "ready"
    assert env.score_calls["n"] == 1


def test_hold_albums_are_not_scored(env, monkeypatch):
    _good(env.incoming, "S1")
    from app.utils import resolve as resolve_mod
    real = resolve_mod.resolve

    def as_studio(*a, **k):
        r = real(*a, **k)
        r.kind = "studio"
        return r
    monkeypatch.setattr("app.api.ingest.resolve", as_studio)
    run = _run(env.incoming)
    bir.discover(run)
    bir.process(run, _never_stop)
    assert _item(run, "S1").status == "ready"
    assert env.score_calls["n"] == 0


# ── ingest endpoints ────────────────────────────────────────────────────────

def test_ingest_one_ready_promotes_score_and_audio_pass_skips_it(app, env, monkeypatch):
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    c = app.test_client()
    _admin(c)
    r = c.post(f"/api/bulk-ingest/items/{item.id}/ingest")
    assert r.status_code == 202
    run = _db.session.get(BulkIngestRun, run.id)
    assert run.status == "running"                       # revived for the worker
    bir.process(run, _never_stop)

    item = _item(run, "S1")
    assert item.status == "ingested" and item.recording_id
    assert not item.ingest_requested
    rq = _db.session.query(RecordingQuality).filter_by(recording_id=item.recording_id).one()
    assert rq.listening_quality == 77.0                  # promoted, not recomputed
    assert env.score_calls["n"] == 1

    seen = {}
    monkeypatch.setattr("app.api.ingest.run_audio_pass",
                        lambda rid, score=True, reanalyze=False: seen.update(score=score)
                        or {"errors": []})
    from app.api.ingest import _handle_audio
    _handle_audio(item.recording_id)
    assert seen["score"] is False
    assert _db.session.get(BulkIngestRun, run.id).status == "done"


def test_ingest_one_rejects_other_statuses_and_paused_runs(app, env):
    run = _hold_run(env, "S1")
    c = app.test_client()
    _admin(c)
    item = _item(run, "S1")
    item.status = "moved"
    _db.session.commit()
    assert c.post(f"/api/bulk-ingest/items/{item.id}/ingest").status_code == 409
    item.status = "ready"
    run.status = "paused"
    _db.session.commit()
    assert c.post(f"/api/bulk-ingest/items/{item.id}/ingest").status_code == 409
    assert c.post("/api/bulk-ingest/items/99999/ingest").status_code == 404


def test_ingest_flagged_anyway_but_never_without_artist(app, env):
    _flac(env.incoming / "Nobody" / "01.flac", DATE="1977-05-08")    # no artist
    run = _hold_run(env, flagged=("F1",))
    nobody = _item(run, "Nobody")
    assert nobody.status == "review" and "needs_artist" in nobody.reason
    c = app.test_client()
    _admin(c)
    for it in (_item(run, "F1"), nobody):
        assert c.post(f"/api/bulk-ingest/items/{it.id}/ingest").status_code == 202
    run = _db.session.get(BulkIngestRun, run.id)
    bir.process(run, _never_stop)
    f1, nobody = _item(run, "F1"), _item(run, "Nobody")
    assert f1.status == "ingested" and f1.recording_id
    assert nobody.status == "review" and "needs_artist" in nobody.reason
    assert not nobody.ingest_requested and not (env.incoming / "F1").exists()


def test_ingest_anyway_refuses_exact_duplicate(app, env):
    # F1 is flagged; its twin (same FFP) gets ingested first, so by the time F1
    # is forced it is an exact content duplicate.
    h = "abcd1234abcd1234abcd1234abcd1234"
    _flagged(env.incoming, "F1")
    (env.incoming / "F1" / "a.ffp").write_text(f"{h} *01.flac\n")
    run = _hold_run(env, flagged=())
    assert _item(run, "F1").status == "review"
    _good(env.incoming, "Twin")
    (env.incoming / "Twin" / "a.ffp").write_text(f"{h} *01.flac\n")
    run2 = _run(env.incoming, mode="auto")
    bir.discover(run2)
    bir.process(run2, _never_stop)
    assert _item(run2, "Twin").status in ("ingested", "skipped")
    c = app.test_client()
    _admin(c)
    it = _item(run, "F1")
    c.post(f"/api/bulk-ingest/items/{it.id}/ingest")
    run = _db.session.get(BulkIngestRun, run.id)
    bir.process(run, _never_stop)
    it = _item(run, "F1")
    assert it.status == "skipped" and it.reason == "duplicate_content"


def test_ingest_ready_takes_only_ready_items(app, env):
    run = _hold_run(env, "S1", "S22", flagged=("F1",))
    c = app.test_client()
    _admin(c)
    out = c.post(f"/api/bulk-ingest/runs/{run.id}/ingest-ready")
    assert out.status_code == 202 and out.get_json()["queued"] == 2
    run = _db.session.get(BulkIngestRun, run.id)
    bir.process(run, _never_stop)
    assert _item(run, "S1").status == "ingested"
    assert _item(run, "S22").status == "ingested"
    assert _item(run, "F1").status == "review"
    assert (env.incoming / "F1").is_dir()
    assert c.post("/api/bulk-ingest/runs/999/ingest-ready").status_code == 404


# ── blanket values ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["auto", "hold"])
def test_applied_values_overwrite_inference(app, env, mode):
    c = app.test_client()
    _admin(c)
    _good(env.incoming, "S1")
    run = _run(env.incoming, mode=mode)
    r = c.put(f"/api/bulk-ingest/runs/{run.id}/applied",
              json={"artist": "Phish", "city": "Burlington", "notes": "n1", "junk": "x"})
    assert r.get_json()["applied"] == {"artist": "Phish", "city": "Burlington",
                                       "notes": "n1"}
    bir.discover(run)
    bir.process(run, _never_stop)
    item = _item(run, "S1")
    if mode == "hold":
        assert item.status == "ready"
        c.post(f"/api/bulk-ingest/items/{item.id}/ingest")
        run = _db.session.get(BulkIngestRun, run.id)
        bir.process(run, _never_stop)
        item = _item(run, "S1")
    assert item.status == "ingested"
    rec = _db.session.get(Recording, item.recording_id)
    perf = _db.session.get(Performance, rec.performance_id)
    assert _db.session.get(Artist, perf.artist_id).name == "Phish"
    assert perf.venue.city == "Burlington" and rec.notes == "n1"


def test_applied_source_tag_reaches_the_recording(app, env):
    """Source Tag is a blanket field: kept by the PUT, written on ingest."""
    c = app.test_client()
    _admin(c)
    _good(env.incoming, "S1")
    run = _run(env.incoming, mode="auto")
    r = c.put(f"/api/bulk-ingest/runs/{run.id}/applied",
              json={"source_tag": " Neumann ", "source": "AUD"})
    assert r.get_json()["applied"] == {"source_tag": "Neumann", "source": "AUD"}
    assert c.put(f"/api/bulk-ingest/runs/{run.id}/applied",
                 json={"source_tag": 5}).status_code == 400
    c.put(f"/api/bulk-ingest/runs/{run.id}/applied", json={"source_tag": "Neumann"})
    bir.discover(run)
    bir.process(run, _never_stop)
    item = _item(run, "S1")
    assert item.status == "ingested"
    assert _db.session.get(Recording, item.recording_id).source_tag == "Neumann"


def test_applied_artist_clears_needs_artist(app, env):
    _flac(env.incoming / "Nobody" / "01.flac", DATE="1977-05-08", VENUE="Hall")
    run = _run(env.incoming, mode="auto")
    run.applied_json = '{"artist": "Phish"}'
    _db.session.commit()
    bir.discover(run)
    bir.process(run, _never_stop)
    assert _item(run, "Nobody").status == "ingested"


def test_applied_endpoint_validates_and_clears(app, env):
    c = app.test_client()
    _admin(c)
    run = _run(env.incoming)
    url = f"/api/bulk-ingest/runs/{run.id}/applied"
    assert c.put(url, json=["x"]).status_code == 400
    assert c.put(url, json={"artist": 5}).status_code == 400
    out = c.put(url, json={"venue": "The Gorge", "venue_id": 3, "event": "  "}).get_json()
    assert out["applied"] == {"venue": "The Gorge", "venue_id": 3}
    assert c.put(url, json={}).get_json()["applied"] is None
    assert c.put("/api/bulk-ingest/runs/999/applied", json={}).status_code == 404


# ── statuses in counts, filters and listings ────────────────────────────────

def test_ready_and_moved_in_counts_queue_and_runs(app, env):
    c = app.test_client()
    _admin(c)
    run = _run(env.incoming)
    for i, st in enumerate(("ready", "review", "moved", "ingested", "pending")):
        _db.session.add(BulkIngestItem(run_id=run.id, rel_path=f"i{i}", status=st,
                                       kind="live" if st == "ingested" else None))
    run.status = "done"
    _db.session.commit()

    cur = c.get(f"/api/bulk-ingest/current?run_id={run.id}").get_json()
    assert cur["counts"]["ready"] == 1 and cur["counts"]["moved"] == 1
    q = c.get(f"/api/bulk-ingest/{run.id}/items?status=queue").get_json()
    assert {i["status"] for i in q["items"]} == {"ready", "review", "pending"}
    done = c.get(f"/api/bulk-ingest/{run.id}/items?status=done").get_json()
    assert {i["status"] for i in done["items"]} == {"ready", "review", "moved", "ingested"}
    assert {i["status"] for i in c.get(
        f"/api/bulk-ingest/{run.id}/items?status=live").get_json()["items"]} == {"ingested"}

    # A finished hold run with ready/review items stays listed ...
    listed = c.get("/api/bulk-ingest/runs").get_json()["runs"]
    assert [r["id"] for r in listed] == [run.id] and listed[0]["mode"] == "hold"
    assert c.get("/api/bulk-ingest/current").get_json()["id"] == "queue"
    # ... and drops off once only moved/ingested items remain.
    for it in _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all():
        if it.status in ("ready", "review", "pending"):
            it.status = "moved"
    _db.session.commit()
    assert c.get("/api/bulk-ingest/runs").get_json()["runs"] == []

    # A finished AUTO run with review items is listed too: review work is
    # never orphaned, whichever mode the run had.
    auto = _run(env.incoming, mode="auto")
    auto.status = "done"
    _db.session.add(BulkIngestItem(run_id=auto.id, rel_path="r", status="review"))
    _db.session.commit()
    assert [r["id"] for r in c.get("/api/bulk-ingest/runs").get_json()["runs"]] == [auto.id]
    assert bir._active_run_for_root(str(env.incoming)).id == auto.id


def test_restarting_a_queued_hold_root_resumes_that_run(app, env):
    run = _hold_run(env, "S1")
    again = bir.start_run(str(env.incoming))
    assert again.id == run.id and again.status == "running"
    _good(env.incoming, "S2", day=20)
    bir._pick_run(set())              # worker pass: re-walks the revived run
    assert _item(run, "S2") is not None


# ── resume ──────────────────────────────────────────────────────────────────

def test_resume_does_not_reprocess_ready_items(app, env):
    run = _hold_run(env, "S1")
    calls = env.score_calls["n"]
    bir.reset_in_progress(run)
    bir.resume_on_boot(app)                              # finished run: left alone
    assert _item(run, "S1").status == "ready"
    run.status = "running"
    _db.session.commit()
    bir.process(run, _never_stop)                        # nothing pending or requested
    assert _item(run, "S1").status == "ready"
    assert env.score_calls["n"] == calls
    assert _db.session.get(BulkIngestRun, run.id).status == "done"


# ── migration ───────────────────────────────────────────────────────────────

def test_migration_is_idempotent(tmp_path):
    import sqlalchemy as sa
    from scripts import migrate_unified_import as mig
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE bulk_ingest_run (id INTEGER PRIMARY KEY, root TEXT, status TEXT);
        CREATE TABLE bulk_ingest_item (id INTEGER PRIMARY KEY, run_id INTEGER, status TEXT);
        INSERT INTO bulk_ingest_run (root, status) VALUES ('/x', 'done');
        INSERT INTO bulk_ingest_item (run_id, status) VALUES (1, 'review');
    """)
    con.commit()
    con.close()
    engine = sa.create_engine(f"sqlite:///{db_file}")
    added = mig.migrate(engine)
    assert sorted(added) == ["bulk_ingest_item.ingest_requested",
                             "bulk_ingest_run.applied_json", "bulk_ingest_run.mode"]
    assert mig.migrate(engine) == []
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT mode FROM bulk_ingest_run")).scalar() == "auto"
        assert conn.execute(sa.text(
            "SELECT ingest_requested FROM bulk_ingest_item")).scalar() == 0


# ── Move to Backlog / Workshop ──────────────────────────────────────────────

def test_item_move_sends_folder_and_marks_moved(app, env, tmp_path):
    backlog = tmp_path / "allowed" / "Backlog"
    app.config["TRIAGE_DIRS"] = {"backlog": str(backlog)}
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    assert item.status == "ready"
    c = app.test_client()
    _admin(c)
    out = c.post(f"/api/bulk-ingest/items/{item.id}/move", json={"dest": "backlog"})
    assert out.status_code == 200
    assert (backlog / "S1" / "01.flac").exists()
    assert not (env.incoming / "S1").exists()
    assert _db.session.get(BulkIngestItem, item.id).status == "moved"
    # A moved item has left the Queue.
    q = c.get(f"/api/bulk-ingest/{run.id}/items?status=queue").get_json()
    assert q["total"] == 0


def test_item_move_refused_for_in_place_run(app, env, tmp_path):
    app.config["TRIAGE_DIRS"] = {"backlog": str(tmp_path / "allowed" / "Backlog")}
    _good(env.lib, "L1")
    run = _run(env.lib)
    bir.discover(run)
    bir.process(run, _never_stop)
    item = _item(_db.session.get(BulkIngestRun, run.id), "L1")
    c = app.test_client()
    _admin(c)
    out = c.post(f"/api/bulk-ingest/items/{item.id}/move", json={"dest": "backlog"})
    assert out.status_code == 409
    assert (env.lib / "L1" / "01.flac").exists()
    assert _db.session.get(BulkIngestItem, item.id).status != "moved"


def test_item_move_refused_while_downloading(app, env, tmp_path, monkeypatch):
    from app.utils import download_queue as dq
    app.config["TRIAGE_DIRS"] = {"backlog": str(tmp_path / "allowed" / "Backlog")}
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    monkeypatch.setattr(dq, "downloading_here", lambda p: True)
    c = app.test_client()
    _admin(c)
    out = c.post(f"/api/bulk-ingest/items/{item.id}/move", json={"dest": "backlog"})
    assert out.status_code == 409
    assert (env.incoming / "S1" / "01.flac").exists()
    assert _db.session.get(BulkIngestItem, item.id).status == "ready"


def test_item_serializes_abs_path_and_sound_band(app, env):
    run = _hold_run(env, "S1")
    c = app.test_client()
    _admin(c)
    it = c.get(f"/api/bulk-ingest/{run.id}/items?status=queue").get_json()["items"][0]
    assert it["abs_path"] == str(env.incoming / "S1")
    assert it["listening_quality"] == 77.0 and it["sound_band"] in ("green", "yellow", "red")


def test_reanalyze_puts_item_back_to_pending(app, env):
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    c = app.test_client()
    _admin(c)
    assert c.post(f"/api/bulk-ingest/items/{item.id}/reanalyze").status_code == 202
    got = _db.session.get(BulkIngestItem, item.id)
    assert got.status == "pending" and got.meta is None


# ── review fixes (2026-10-02) ───────────────────────────────────────────────

def test_single_show_run_reads_its_own_folder_name(env):
    # An outside single-folder run has one item, rel_path "."; the folder name
    # (here the only place the date is) must still reach the resolver, and the
    # staging row must be keyed by the real path, not "<path>/.".
    show = env.incoming / "Phish 1997-11-22 Hampton Coliseum"
    _flac(show / "01.flac", ARTIST="Phish")
    run = _run(show)
    bir.discover(run)
    bir.process(run, _never_stop)
    it = _item(_db.session.get(BulkIngestRun, run.id), ".")
    # The folder name alone is single-source date evidence: the resolver reads
    # it (so the reason is "tentative:date", not a missing date) but the
    # calibrated verdict will not auto-ingest on it.
    assert (it.status, it.reason) == ("review", "tentative:date"), (it.status, it.reason)
    from app.utils import quality_store as qs
    assert qs.get_staging(str(show)) is not None


def test_item_move_refused_once_queued_for_ingest(app, env, tmp_path):
    backlog = tmp_path / "allowed" / "Backlog"
    app.config["TRIAGE_DIRS"] = {"backlog": str(backlog)}
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    c = app.test_client()
    _admin(c)
    assert c.post(f"/api/bulk-ingest/items/{item.id}/ingest").status_code == 202
    out = c.post(f"/api/bulk-ingest/items/{item.id}/move", json={"dest": "backlog"})
    assert out.status_code == 409
    assert (env.incoming / "S1" / "01.flac").exists()
    assert not backlog.exists() or not (backlog / "S1").exists()


def test_abs_key_treats_dot_item_as_its_folder():
    assert bir._abs_key("/a/Downloads/Show", ".") == bir._abs_key("/a/Downloads", "Show")


def test_per_item_review_confirm_takes_the_item_out_of_the_queue(app, env, seeded_ids, monkeypatch):
    # The import page's Review opens the Add Recording form; its confirm job
    # must reconcile the run's item, or the row keeps offering Ingest for a
    # folder that is already in the library.
    from app.api import ingest as ingest_api
    from app.utils import quality_store as qs
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    assert item.status == "ready"
    rid = seeded_ids["recording_id"]

    def fake_confirm(data, user_id, *a, **k):
        qs.get_staging(data["source_folder"]).recording_id = rid   # what promote_to_recording does
        _db.session.commit()
        return {"recording_id": rid}

    monkeypatch.setattr(ingest_api, "_do_confirm", fake_confirm)
    ingest_api._INGEST_JOBS["t"] = {"status": "running"}
    ingest_api._run_ingest_job("t", app, {"source_folder": str(env.incoming / "S1")}, None)
    assert ingest_api._INGEST_JOBS["t"]["status"] == "done"
    _db.session.expire_all()
    it = _db.session.get(BulkIngestItem, item.id)
    assert it.status == "ingested" and it.recording_id == rid


# ── review fixes (2026-10-02, second pass) ─────────────────────────────────

def test_bring_in_in_progress_goes_to_review_on_boot_reset(env):
    run = _run(env.incoming)
    _db.session.add(BulkIngestItem(run_id=run.id, rel_path="S1", status="in_progress"))
    _db.session.commit()
    bir.reset_in_progress(run)
    it = _item(run, "S1")
    assert it.status == "review" and it.reason == "interrupted"


def test_in_place_in_progress_still_resets_to_pending(env):
    run = _run(env.lib)
    _db.session.add(BulkIngestItem(run_id=run.id, rel_path="S1", status="in_progress"))
    _db.session.commit()
    bir.reset_in_progress(run)
    assert _item(run, "S1").status == "pending"


def test_move_failure_parks_item_for_review_and_run_continues(app, env, monkeypatch):
    from app.api.ingest import MoveFailed
    _good(env.incoming, "S1", day=11)
    _good(env.incoming, "S2", day=12)
    import app.api.ingest as ing
    orig = ing.auto_confirm

    def flaky(folder_abs, *a, **k):
        if folder_abs.endswith("S1"):
            raise MoveFailed("File operation failed: disk")
        return orig(folder_abs, *a, **k)
    monkeypatch.setattr(ing, "auto_confirm", flaky)
    run = _run(env.incoming, mode="auto")
    bir.discover(run)
    bir.process(run, _never_stop)
    s1 = _item(run, "S1")
    assert s1.status == "review" and s1.reason == "move_failed"
    assert _item(run, "S2").status == "ingested"


def test_converting_folder_is_skipped_then_retried(app, env, monkeypatch):
    _good(env.incoming, "S1")
    import app.api.quality as q
    monkeypatch.setattr(q, "converting_here", lambda p: True)
    run = _run(env.incoming, mode="auto")
    bir.discover(run)
    bir.process(run, _never_stop)
    it = _item(run, "S1")
    assert it.status == "pending" and it.reason == "converting"
    # Cooling down: not workable until the retry window lapses.
    assert bir._pick_run({run.id}) is None
    monkeypatch.setattr(bir, "_DOWNLOAD_RETRY_SECS", 0)
    monkeypatch.setattr(q, "converting_here", lambda p: False)
    bir.process(run, _never_stop)
    assert _item(run, "S1").status == "ingested"


def test_converting_here_tracks_running_job(app, tmp_path):
    import app.api.quality as q
    from app.utils import quality_store as qs
    q._CONVERT_JOBS["j"] = {"status": "running", "folder": qs.norm_path(str(tmp_path))}
    try:
        assert q.converting_here(str(tmp_path))
        assert not q.converting_here(str(tmp_path / "other"))
        q._CONVERT_JOBS["j"]["status"] = "done"
        assert not q.converting_here(str(tmp_path))
    finally:
        q._CONVERT_JOBS.pop("j", None)


def test_ingest_endpoints_409_while_converting(app, env, monkeypatch):
    import app.api.quality as q
    run = _hold_run(env, "S1")
    item = _item(run, "S1")
    monkeypatch.setattr(q, "converting_here", lambda p: True)
    c = app.test_client()
    _admin(c)
    assert c.post(f"/api/bulk-ingest/items/{item.id}/ingest").status_code == 409
    assert c.post(f"/api/bulk-ingest/runs/{run.id}/ingest-ready").status_code == 409
    app.config["TRIAGE_DIRS"] = {"backlog": str(env.lib.parent / "Backlog")}
    assert c.post(f"/api/bulk-ingest/items/{item.id}/move",
                  json={"dest": "backlog"}).status_code == 409
    assert _item(run, "S1").ingest_requested in (False, None)


def test_start_refused_while_folder_downloading(app, env, monkeypatch):
    from app.utils import download_queue as dq
    _good(env.incoming, "S1")
    monkeypatch.setattr(dq, "downloading_here", lambda p: True)
    c = app.test_client()
    _admin(c)
    out = c.post("/api/bulk-ingest/start", json={"path": str(env.incoming)})
    assert out.status_code == 409
    assert out.get_json()["error"] == "This folder is still downloading."


def test_idle_worker_backs_off_for_retry_window(monkeypatch):
    # The only remaining work is cooling-down items: the worker waits on _WAKE
    # for the full retry window, not a 1 s poll.
    waits = []
    monkeypatch.setattr(bir._WAKE, "wait", lambda t=None: waits.append(t) or True)
    import inspect
    src = inspect.getsource(bir._start_worker)
    assert "_WAKE.wait(_DOWNLOAD_RETRY_SECS)" in src and "time.sleep(min(1.0" not in src


def test_hold_mode_scoring_does_not_hold_fs_lock(app, env, monkeypatch):
    import threading
    from app.utils import download_queue as dq
    seen = {}

    def scorer(folder_abs, base):
        got = []

        def probe():
            ok = dq.FS_LOCK.acquire(timeout=2)
            got.append(ok)
            if ok:
                dq.FS_LOCK.release()
        t = threading.Thread(target=probe)
        t.start()
        t.join()
        seen["acquirable"] = bool(got and got[0])
        return 50.0
    monkeypatch.setattr(bir, "_score_before_ingest", scorer)
    _hold_run(env, "S1")
    assert seen.get("acquirable") is True


# ── in-place hold: no pre-ingest scoring ────────────────────────────────────

def test_in_place_hold_run_is_ready_with_no_scoring(env):
    _good(env.lib, "L1")
    run = _run(env.lib)
    bir.discover(run)
    bir.process(run, _never_stop)
    item = _item(_db.session.get(BulkIngestRun, run.id), "L1")
    assert item.status == "ready"
    assert env.score_calls["n"] == 0


def test_ingest_requested_ids_filter_returns_only_those_rows(app, env):
    c = app.test_client()
    _admin(c)
    run = _hold_run(env, "S1", "S22")
    ids = [_item(run, "S1").id]
    body = c.get(f"/api/bulk-ingest/{run.id}/items?ids={ids[0]},99999").get_json()
    assert [i["id"] for i in body["items"]] == ids


# ── unsupported audio (WAV/AIFF/SHN/APE/WV): never imported ─────────────────

def _wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="WAV")


def _unsupported_run(env, mode):
    _wav(env.incoming / "WavShow" / "01.wav")
    _flac(env.incoming / "MixedShow" / "01.flac", ARTIST="Grateful Dead",
          DATE="1977-05-09", VENUE="Hall M")
    _wav(env.incoming / "MixedShow" / "02.wav")
    _good(env.incoming, "GoodShow")
    run = _run(env.incoming, mode=mode)
    bir.discover(run)
    bir.process(run, _never_stop)
    return _db.session.get(BulkIngestRun, run.id)


@pytest.mark.parametrize("mode", ["auto", "hold"])
def test_wav_and_mixed_folders_stay_in_queue_as_unsupported(env, mode):
    run = _unsupported_run(env, mode)
    for name in ("WavShow", "MixedShow"):
        it = _item(run, name)
        assert it.status == "review" and it.reason == "unsupported_format"
        assert (env.incoming / name).is_dir()          # nothing moved
    assert _item(run, "WavShow").format == "WAV"
    assert _item(run, "MixedShow").format == "FLAC, WAV"
    assert _item(run, "GoodShow").status == ("ingested" if mode == "auto" else "ready")


def test_force_cannot_import_unsupported(app, env):
    run = _unsupported_run(env, "hold")
    c = app.test_client()
    _admin(c)
    it = _item(run, "WavShow")
    # The endpoint refuses outright ...
    assert c.post(f"/api/bulk-ingest/items/{it.id}/ingest").status_code == 409
    # ... and even a forced request the worker is handed stays in review.
    it.ingest_requested = True
    _db.session.commit()
    bir.process(_db.session.get(BulkIngestRun, run.id), _never_stop)
    it = _item(run, "WavShow")
    assert it.status == "review" and it.reason == "unsupported_format"
    assert (env.incoming / "WavShow").is_dir()


def test_ingest_ready_skips_unsupported(app, env):
    run = _unsupported_run(env, "hold")
    c = app.test_client()
    _admin(c)
    out = c.post(f"/api/bulk-ingest/runs/{run.id}/ingest-ready")
    assert out.status_code == 202 and out.get_json()["queued"] == 1


def test_convert_unsupported_queues_only_unsupported_rows(app, env, monkeypatch):
    import app.api.bulk_ingest as api
    run = _unsupported_run(env, "hold")
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            started.append(args)

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", FakeThread)
    c = app.test_client()
    _admin(c)
    out = c.post(f"/api/bulk-ingest/runs/{run.id}/convert-unsupported")
    assert out.status_code == 202 and out.get_json()["queued"] == 2
    ids = started[0][2]
    assert set(ids) == {_item(run, "WavShow").id, _item(run, "MixedShow").id}
    # A second press while the first batch runs is refused.
    assert c.post(f"/api/bulk-ingest/runs/{run.id}/convert-unsupported").status_code == 409
    api._CONVERT_ALL_ACTIVE.discard(run.id)
    api._CONVERT_ALL_QUEUED.clear()


def test_convert_unsupported_refuses_paused_and_downloading(app, env, monkeypatch):
    from app.utils import download_queue as dq
    run = _unsupported_run(env, "hold")
    c = app.test_client()
    _admin(c)
    monkeypatch.setattr(dq, "downloading_here", lambda p: True)
    out = c.post(f"/api/bulk-ingest/runs/{run.id}/convert-unsupported")
    assert out.status_code == 409 and "downloading" in out.get_json()["error"]
    monkeypatch.undo()
    monkeypatch.setattr(bir, "_start_worker", lambda *a, **k: None)
    run = _db.session.get(BulkIngestRun, run.id)
    run.status = "paused"
    _db.session.commit()
    assert c.post(f"/api/bulk-ingest/runs/{run.id}/convert-unsupported").status_code == 409


def test_convert_all_worker_converts_and_requeues(app, env, monkeypatch):
    import subprocess
    import app.api.bulk_ingest as api
    run = _unsupported_run(env, "hold")
    monkeypatch.setattr("app.utils.audio_convert.probe_decoder", lambda *a: True)

    def fake_run(cmd, **kw):
        open(cmd[-1], "wb").write(b"fLaC" + b"\0" * 8)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr("app.utils.audio_convert.subprocess.run", fake_run)
    ids = [_item(run, "WavShow").id, _item(run, "MixedShow").id]
    api._CONVERT_ALL_ACTIVE.add(run.id)
    api._CONVERT_ALL_QUEUED.update(ids)
    api._convert_all_worker(app, run.id, ids)
    assert not (env.incoming / "WavShow" / "01.wav").exists()
    assert (env.incoming / "WavShow" / "01.flac").exists()
    assert not (env.incoming / "MixedShow" / "02.wav").exists()
    assert run.id not in api._CONVERT_ALL_ACTIVE
    _db.session.expire_all()
    assert _item(run, "WavShow").status == "pending"
    assert _item(run, "MixedShow").status == "pending"
    assert _item(run, "GoodShow").status == "ready"


def test_wav_is_classified_unsupported_by_scan_folder(tmp_path):
    from app.utils.ingest import scan_folder, AUDIO_EXTENSIONS
    assert ".wav" not in AUDIO_EXTENSIONS
    _wav(tmp_path / "s" / "01.wav")
    scan = scan_folder(str(tmp_path / "s"))
    assert scan["audio_files"] == [] and len(scan["unsupported_audio"]) == 1


def test_queued_convert_all_rows_report_converting(app, env):
    import app.api.bulk_ingest as api
    run = _unsupported_run(env, "hold")
    it = _item(run, "WavShow")
    api._CONVERT_ALL_QUEUED.add(it.id)
    try:
        c = app.test_client()
        _admin(c)
        got = c.get(f"/api/bulk-ingest/{run.id}/items?ids={it.id}").get_json()["items"][0]
        assert got["converting"] is not None
        other = _item(run, "MixedShow")
        got = c.get(f"/api/bulk-ingest/{run.id}/items?ids={other.id}").get_json()["items"][0]
        assert got["converting"] is None
    finally:
        api._CONVERT_ALL_QUEUED.discard(it.id)


def test_add_to_library_queues_the_folder_paused(app, env):
    # Add to Library on a working folder (Ryan, 2026-10-06): the folder is in the
    # Queue, nothing has been scanned, and the run waits for the person's Start.
    _good(env.incoming, "S1")
    c = app.test_client()
    _admin(c)
    out = c.post("/api/bulk-ingest/start", json={"path": str(env.incoming / "S1"), "paused": True})
    assert out.status_code == 200
    run = _db.session.get(BulkIngestRun, out.get_json()["id"])
    assert run.status == "paused"
    items = _db.session.query(BulkIngestItem).filter_by(run_id=run.id).all()
    assert [i.status for i in items] == ["pending"]


# ── One Queue (Ryan, 2026-10-06: "There should only be one queue. Ever.") ────

def test_two_sources_make_one_queue_and_reset_clears_both(app, env):
    _good(env.incoming, "S1")
    _good(env.incoming, "S2", day=9)
    c = app.test_client()
    _admin(c)
    for name in ("S1", "S2"):
        assert c.post("/api/bulk-ingest/start",
                      json={"path": str(env.incoming / name), "paused": True}).status_code == 200
    q = c.get("/api/bulk-ingest/current").get_json()
    assert q["id"] == "queue" and q["counts"]["pending"] == 2
    assert c.get("/api/bulk-ingest/queue/items?status=queue").get_json()["total"] == 2
    assert c.post("/api/bulk-ingest/queue/reset").status_code == 200
    assert c.get("/api/bulk-ingest/current").get_json() == {"run": None}
    assert c.get("/api/bulk-ingest/runs").get_json()["runs"] == []


def test_an_overlapping_source_never_queues_a_folder_twice(app, env):
    _good(env.incoming, "S1")
    c = app.test_client()
    _admin(c)
    c.post("/api/bulk-ingest/start", json={"path": str(env.incoming / "S1"), "paused": True})
    c.post("/api/bulk-ingest/start", json={"path": str(env.incoming), "paused": True})
    assert c.get("/api/bulk-ingest/queue/items?status=queue").get_json()["total"] == 1


def test_pause_and_resume_apply_to_the_whole_queue(app, env):
    _good(env.incoming, "S1")
    _good(env.incoming, "S2", day=9)
    c = app.test_client()
    _admin(c)
    for name in ("S1", "S2"):
        c.post("/api/bulk-ingest/start", json={"path": str(env.incoming / name), "paused": True})
    c.post("/api/bulk-ingest/queue/resume")
    assert {r.status for r in _db.session.query(BulkIngestRun).all()} <= {"running", "done"}
    c.post("/api/bulk-ingest/queue/pause")
    runs = _db.session.query(BulkIngestRun).all()
    assert all(r.status in ("paused", "done") for r in runs)


def test_import_all_requests_ready_and_review_and_skipped_sort_last(app, env):
    run = _run(env.incoming, mode="hold")
    run.status = "done"
    for rel, st in (("A", "skipped"), ("B", "ready"), ("C", "review")):
        _db.session.add(BulkIngestItem(run_id=run.id, rel_path=rel, status=st))
    _db.session.commit()
    c = app.test_client()
    _admin(c)
    rows = c.get("/api/bulk-ingest/queue/items?status=queue").get_json()["items"]
    assert [r["rel_path"] for r in rows] == ["B", "C", "A"]
    assert c.post("/api/bulk-ingest/queue/ingest-all").get_json()["queued"] == 2
    asked = {it.rel_path for it in _db.session.query(BulkIngestItem)
             .filter_by(run_id=run.id, ingest_requested=True).all()}
    assert asked == {"B", "C"}


# ── Pre-import checks on Add Recording (2026-10-06) ──────────────────────────

def test_fingerprint_and_spectrogram_routes_are_admin_only(app, env):
    _good(env.incoming, "S1")
    anon = app.test_client()
    assert anon.get(f"/api/quality/fingerprints?path={env.incoming / 'S1'}").status_code in (401, 403)
    assert anon.get(f"/api/tracks/spectrogram-file?path={env.incoming / 'S1'}").status_code in (401, 403)
    c = app.test_client()
    _admin(c)
    out = c.get(f"/api/quality/fingerprints?path={env.incoming / 'S1'}")
    assert out.status_code == 200 and isinstance(out.get_json()["files"], list)
    assert c.get(f"/api/tracks/spectrogram-file?path={env.incoming / 'nope.flac'}").status_code == 404


def test_reset_queue_clears_the_imported_rows_from_the_page(app, env):
    run = _run(env.incoming, mode="hold")
    run.status = "done"
    _db.session.add(BulkIngestItem(run_id=run.id, rel_path="A", status="ingested", kind="live"))
    _db.session.commit()
    c = app.test_client()
    _admin(c)
    assert c.get("/api/bulk-ingest/current").get_json()["counts"]["ingested"] == 1
    assert c.post("/api/bulk-ingest/queue/reset").status_code == 200
    assert c.get("/api/bulk-ingest/current").get_json() == {"run": None}
