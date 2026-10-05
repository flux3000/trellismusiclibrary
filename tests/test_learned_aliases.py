"""
tests/test_learned_aliases.py -- Resolver v2 chunk 8: learned aliases, the matcher reading
them at once, the importer's queue re-check and the review queue's order.

Real FLACs under tmp dirs, discover()/process() driven directly (no worker thread), as in
test_bulk_ingest_review_first.py.
"""
import copy
import json

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.alias import ArtistAlias, VenueAlias
from app.models.artist import Artist
from app.models.bulk_ingest import BulkIngestItem, BulkIngestRun
from app.models.recording import Recording
from app.models.venue import Venue
from app.utils import bulk_ingest_run as bir
from app.utils.aliases import confirmed_keys, learn_aliases
from app.utils.ingest import build_scan_payload, parse_info_file
from app.utils.reader import confidence as C
from app.utils.reader.library import current_library, invalidate_library_cache, norm_key
from app.utils.resolve import resolve


# ── helpers ──────────────────────────────────────────────────────────────────

def _rr(venue=None, artist=None):
    """A resolver_result as Resolved.to_dict() carries it: value + quoted info evidence."""
    def f(v):
        return {"value": v, "evidence": [{"source": "info", "text": v}]} if v else \
               {"value": None, "evidence": []}
    return {"venue": f(venue), "artist": f(artist)}


def _data(rr, **extra):
    return {"resolver_result": rr, **extra}


@pytest.fixture()
def rows(app):
    v = Venue(name="Merlefest Grounds", city="Wilkesboro", state="NC", country="US")
    a = Artist(name="Jerry Garcia Band")
    _db.session.add_all([v, a])
    _db.session.commit()
    invalidate_library_cache()
    return v, a


def _count(model):
    return _db.session.query(model).count()


# ── writing aliases ──────────────────────────────────────────────────────────

def test_a_correction_writes_exactly_one_alias_per_field_and_resaving_adds_none(rows):
    v, a = rows
    data = _data(_rr("Wilkes Commnity College", "Jerry Garsia Band"))
    got = learn_aliases(data, a, v)
    _db.session.commit()
    assert got == [("artist", "Jerry Garsia Band"), ("venue", "Wilkes Commnity College")]
    assert _count(VenueAlias) == 1 and _count(ArtistAlias) == 1
    va = _db.session.query(VenueAlias).one()
    assert va.venue_id == v.id and va.alias == "Wilkes Commnity College"
    assert va.alias_key == norm_key("Wilkes Commnity College")
    assert _db.session.query(ArtistAlias).one().alias_key == "jerry garsia band"
    # saving again (or another recording quoting the same text) is a no-op
    assert learn_aliases(data, a, v) == []
    assert learn_aliases(_data(_rr("wilkes  commnity college!", None)), a, v) == []
    _db.session.commit()
    assert _count(VenueAlias) == 1 and _count(ArtistAlias) == 1


def test_no_alias_when_the_field_equals_the_top_candidate(rows):
    v, a = rows
    same = _data(_rr("Merlefest Grounds", "Jerry Garcia Band"))
    assert learn_aliases(same, a, v) == []
    # case, punctuation and a leading "The" do not make a correction either
    assert learn_aliases(_data(_rr("merlefest grounds.", "the Jerry Garcia Band")), a, v) == []
    assert _count(VenueAlias) == 0 and _count(ArtistAlias) == 0


def test_no_alias_without_quoted_evidence_text(rows):
    v, a = rows
    rr = {"venue": {"value": "Wilkes Commnity College", "evidence": []},
          "artist": {"value": "Jerry Garsia Band",
                     "evidence": [{"source": "tags", "text": "Jerry Garsia Band"}]}}
    assert learn_aliases(_data(rr), a, v) == []
    assert learn_aliases({"resolver_result": None}, a, v) == []
    assert _count(VenueAlias) == 0 and _count(ArtistAlias) == 0


def test_no_alias_from_an_ai_proposal_the_person_did_not_accept(rows):
    v, a = rows
    ai = {"proposals": [{"field": "venue", "proposed": "Merlefest Grounds", "confidence": "high",
                         "source": "web"},
                        {"field": "artist", "proposed": "Jerry Garcia Band", "confidence": "low",
                         "source": "web"}]}
    rr = _rr("Wilkes Commnity College", "Jerry Garsia Band")
    assert learn_aliases(_data(rr, ai_result=ai), a, v) == []
    assert _count(VenueAlias) == 0 and _count(ArtistAlias) == 0
    # accepted, the proposal is the person's own value again
    assert learn_aliases(_data(rr, ai_result=ai, ai_accepted=["venue"]), a, v) == \
        [("venue", "Wilkes Commnity College")]


def test_no_alias_from_an_unattended_save(rows):
    v, a = rows
    rr = _rr("Wilkes Commnity College", "Jerry Garsia Band")
    assert learn_aliases(_data(rr, unattended=True), a, v) == []
    assert _count(VenueAlias) == 0 and _count(ArtistAlias) == 0


def test_an_exact_name_of_another_row_is_never_aliased(rows):
    v, a = rows
    other = Venue(name="Wilkes Commnity College")
    _db.session.add(other)
    _db.session.commit()
    invalidate_library_cache()
    assert learn_aliases(_data(_rr("Wilkes Commnity College", None)), a, v) == []


@pytest.mark.parametrize("quote", [
    "Theatre", "Park", "Live", "Concert", "Soundboard",
    "1977-05-08", "May 8, 1977", "Boston, MA", "Massachusetts", "CA", "Wilkesboro, NC",
    "Set 1: Promised Land (4:59)", "01. Promised Land", "d1t02 Dark Star", "Tour 1977-05-08"])
def test_generic_quotes_never_become_aliases(rows, quote):
    v, a = rows
    assert learn_aliases(_data(_rr(quote, quote)), a, v) == []
    assert _count(VenueAlias) == 0 and _count(ArtistAlias) == 0


def test_a_single_word_typo_fix_still_learns(rows):
    v = Venue(name="Fillmore West")
    _db.session.add(v)
    _db.session.commit()
    invalidate_library_cache()
    a = Artist(name="Santana")
    _db.session.add(a)
    _db.session.commit()
    assert learn_aliases(_data(_rr("Filmore", None)), a, v) == [("venue", "Filmore")]
    _db.session.commit()
    assert _db.session.query(VenueAlias).filter_by(venue_id=v.id).count() == 1


def test_swapped_header_does_not_cross_kinds(rows):
    v, a = rows
    _db.session.add(Venue(name="Wilkes Community College"))
    _db.session.commit()
    invalidate_library_cache()
    # the resolver read the venue line as the act and the act line as the venue
    rr = _rr("Jerry Garcia Band", "Wilkes Community College")
    assert learn_aliases(_data(rr), a, v) == []
    assert _count(VenueAlias) == 0 and _count(ArtistAlias) == 0


def test_a_text_already_an_alias_of_another_row_is_not_given_to_a_second(rows):
    v, a = rows
    other = Venue(name="Other Hall")
    _db.session.add(other)
    _db.session.commit()
    invalidate_library_cache()
    assert learn_aliases(_data(_rr("Wilkes Commnity College", None)), None, v) == \
        [("venue", "Wilkes Commnity College")]
    _db.session.commit()
    assert learn_aliases(_data(_rr("Wilkes Commnity College", None)), None, other) == []
    assert _count(VenueAlias) == 1


# ── the matcher reads aliases at once ────────────────────────────────────────

INFO = ("Jerry Garcia Band\nWilkes Commnity College\nWilkesboro, NC\n1977-05-08\n\n"
        "Source: SBD\n\n01. Promised Land\n02. Dark Star\n")


def test_a_re_resolve_of_the_same_text_matches_the_saved_venue(rows):
    v, a = rows
    before = parse_info_file(None, text=INFO, library=current_library())
    assert before["venue"] == "Wilkes Commnity College" and before["venue_match"] is None
    learn_aliases(_data(_rr("Wilkes Commnity College", None)), a, v)
    _db.session.commit()
    # same session, no explicit invalidation: the cached index notices the new alias
    after = parse_info_file(None, text=INFO, library=current_library())
    assert after["venue"] == "Merlefest Grounds"
    assert after["venue_match"] == "Merlefest Grounds"
    assert after["evidence"]["fields"]["venue"]["text"] == "Wilkes Commnity College"
    cand = after["evidence"]["cands"]["venue"][0]
    assert any(n == "lib_venue" for n, _ in cand["ext"])


def test_a_re_resolve_of_the_same_text_matches_the_saved_artist(rows):
    v, a = rows
    text = INFO.replace("Jerry Garcia Band", "Jerry Garsia Band")
    assert parse_info_file(None, text=text, library=current_library())["artist"] == "Jerry Garsia Band"
    learn_aliases(_data(_rr(None, "Jerry Garsia Band")), a, v)
    _db.session.commit()
    got = parse_info_file(None, text=text, library=current_library())
    assert got["artist"] == "Jerry Garcia Band" and got["artist_match"] == "Jerry Garcia Band"


def test_an_exact_name_outranks_an_alias_and_deleting_the_row_drops_its_aliases(rows):
    v, a = rows
    ix = current_library()
    learn_aliases(_data(_rr("Wilkes Commnity College", None)), a, v)
    _db.session.commit()
    assert current_library().venue_match("Wilkes Commnity College")["name"] == "Merlefest Grounds"
    # a venue that really carries the text wins it from then on
    _db.session.add(Venue(name="Wilkes Commnity College"))
    _db.session.commit()
    assert current_library().venue_match("Wilkes Commnity College")["name"] == "Wilkes Commnity College"
    assert current_library().venue_alias_name("Wilkes Commnity College") is None
    _db.session.delete(_db.session.get(Venue, v.id))
    _db.session.commit()
    assert _count(VenueAlias) == 0


# ── the importer: a save in a run ────────────────────────────────────────────

def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    tags.setdefault("TRACKNUMBER", "1")
    for k, val in tags.items():
        audio[k] = val
    audio.save()


def _show(root, name, artist, day, venue="Wilkes Community College"):
    """An info-only show: no artist tag, so the artist is read from the text and is tentative."""
    _flac(root / name / "01.flac", DATE=f"1977-05-{day:02d}")
    (root / name / "info.txt").write_text(
        f"{artist}\n{venue}\nWilkesboro, NC\n1977-05-{day:02d}\n\nSource: SBD\n\n"
        "01. Promised Land\n02. Dark Star\n")


@pytest.fixture()
def env(app, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    app.config["IMPORT_ROOTS"] = [str(tmp_path)]
    app.config["LOGIN_DISABLED"] = True
    monkeypatch.setattr("app.api.ingest._enqueue", lambda app, kind, item_id, run_id=None: True)
    woken = []
    monkeypatch.setattr(bir, "_start_worker", lambda *a, **k: woken.append(1))
    # A library match alone does not lift an info-only artist over the shipped thresholds
    # (it takes another source). Loosen the artist bar for these tests so the library's own
    # effect is what decides, through the real engine.
    cal = copy.deepcopy(C.load_calibration())
    cal["fields"]["artist"].update(tau=0.9, m=5.0)
    C.set_calibration(cal)
    yield type("Env", (), dict(lib=lib, woken=woken))
    C.set_calibration(None)
    C.load_calibration(force=True)
    invalidate_library_cache()


def _run(env, mode):
    run = BulkIngestRun(root=str(env.lib), status="running", mode=mode)
    _db.session.add(run)
    _db.session.commit()
    bir.discover(run)
    bir.process(run, lambda: False)
    return _db.session.get(BulkIngestRun, run.id)


def _item(run, rel):
    return _db.session.query(BulkIngestItem).filter_by(run_id=run.id, rel_path=rel).first()


def _confirm(folder, **over):
    """A person's save of one folder, the way /api/ingest/confirm reaches _do_confirm: the
    resolver's own answer as the form, edited by `over`."""
    from app.api.ingest import _confirm_payload_from_resolved, _do_confirm
    scan = build_scan_payload(str(folder))
    resolved = resolve(scan, library_root=str(folder.parent), placement=None)
    payload = _confirm_payload_from_resolved(resolved, scan)
    payload["resolver_result"] = resolved.to_dict()
    payload.update(over)
    return _do_confirm(payload, bir._owner_user_id())


def _three_shows(env):
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _show(env.lib, "A2", "Sam Bush Band", 9)
    _show(env.lib, "B1", "Del McCoury Band", 10, venue="Ryman Auditorium")


def test_every_row_starts_in_review_with_a_tentative_artist(env):
    _three_shows(env)
    run = _run(env, "auto")
    for n in ("A1", "A2", "B1"):
        it = _item(run, n)
        assert it.status == "review" and "tentative:artist" in it.reason
    assert json.loads(_item(run, "A1").meta)["artist"] == "Sam Bush Band"


def test_confirming_an_artist_clears_queued_rows_with_the_same_reading_auto(env):
    _three_shows(env)
    run = _run(env, "auto")
    n_before = _db.session.query(Recording).count()
    _confirm(env.lib / "A1")
    assert _db.session.query(Artist).filter_by(name="Sam Bush Band").count() == 1
    assert _item(run, "A2").status == "pending" and _item(run, "B1").status == "review"
    assert env.woken, "the worker is woken"
    bir.process(_db.session.get(BulkIngestRun, run.id), lambda: False)
    a2, b1 = _item(run, "A2"), _item(run, "B1")
    assert a2.status == "ingested" and a2.recording_id
    assert b1.status == "review" and "tentative:artist" in b1.reason
    assert _db.session.query(Recording).count() == n_before + 2    # A1 by the person, A2 by the run


def test_review_first_marks_the_rechecked_rows_ready_not_ingested(env):
    _three_shows(env)
    run = _run(env, "hold")
    assert _item(run, "A2").status == "review"
    _confirm(env.lib / "A1")
    n_after_confirm = _db.session.query(Recording).count()
    assert _item(run, "A2").status == "pending"
    assert run.id in bir._SKIP_DISCOVER          # the reopened run is not walked again
    bir._SKIP_DISCOVER.discard(run.id)
    bir.process(_db.session.get(BulkIngestRun, run.id), lambda: False)
    a2 = _item(run, "A2")
    assert a2.status == "ready" and a2.recording_id is None
    assert _item(run, "B1").status == "review"
    assert _db.session.query(Recording).count() == n_after_confirm


def test_human_set_values_on_rechecked_rows_stay_locked(env):
    _three_shows(env)
    run = _run(env, "hold")
    run.applied_json = json.dumps({"venue": "Fixed Venue", "city": "Boone"})
    _db.session.commit()
    _confirm(env.lib / "A1")
    bir.process(_db.session.get(BulkIngestRun, run.id), lambda: False)
    a2 = _item(run, "A2")
    assert a2.status == "ready"
    meta = json.loads(a2.meta)
    assert meta["venue"] == "Fixed Venue" and meta["city"] == "Boone"


def test_a_shared_venue_spelling_sends_a_row_back_even_when_its_artist_differs(env):
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _show(env.lib, "B1", "Del McCoury Band", 10)               # same venue text, other act
    run = _run(env, "auto")
    _confirm(env.lib / "A1")
    assert _item(run, "B1").status == "pending"
    bir.process(_db.session.get(BulkIngestRun, run.id), lambda: False)
    b1 = _item(run, "B1")
    assert b1.status == "review" and "tentative:artist" in b1.reason    # still its own question


def test_the_recheck_leaves_rows_alone_that_are_not_in_a_run_or_not_review(env):
    _three_shows(env)
    run = _run(env, "auto")
    a1 = _item(run, "A1")
    a2 = _item(run, "A2")
    a2.ingest_requested = True               # a person already asked for this one
    _db.session.commit()
    keys = {"artist": {norm_key("Sam Bush Band")}}
    assert bir.recheck_queue(str(env.lib / "not-a-queued-folder"), keys) == []
    assert bir.recheck_queue(str(env.lib / "A1"), keys) == []
    assert _item(run, "A2").status == "review" and a1.status == "review"


def test_a_correction_in_a_save_writes_its_alias_once_through_do_confirm(env):
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _show(env.lib, "A2", "Sam Bush Band", 9)
    _confirm(env.lib / "A1", venue_name="Merlefest Grounds")
    va = _db.session.query(VenueAlias).all()
    assert len(va) == 1 and va[0].alias == "Wilkes Community College"
    assert _db.session.get(Venue, va[0].venue_id).name == "Merlefest Grounds"
    assert _db.session.query(ArtistAlias).count() == 0      # the artist was saved as read
    _confirm(env.lib / "A2", venue_name="Merlefest Grounds")
    assert _db.session.query(VenueAlias).count() == 1       # same text, same row: nothing new


def test_an_unattended_auto_ingest_writes_no_alias(env):
    from app.api.ingest import auto_confirm
    _show(env.lib, "A1", "Sam Bush Band", 8)
    _flac(env.lib / "A1" / "01.flac", DATE="1977-05-08", ARTIST="Sam Bush Band")
    out = auto_confirm(str(env.lib / "A1"), bir._owner_user_id(), applied={"venue": "Fixed Venue"})
    assert out["status"] == "ingested"
    assert _db.session.query(Venue).filter_by(name="Fixed Venue").count() == 1
    assert _db.session.query(VenueAlias).count() == 0
    assert _db.session.query(ArtistAlias).count() == 0


# ── queue order ──────────────────────────────────────────────────────────────

def test_the_review_queue_puts_one_row_per_unconfirmed_act_first(app):
    run = BulkIngestRun(root="/x", status="done", mode="hold")
    _db.session.add(run)
    _db.session.commit()
    readings = [("a1", "Sam Bush Band"), ("a2", "Sam Bush Band"), ("b1", "Del McCoury Band"),
                ("a3", "Sam Bush Band"), ("b2", "Del McCoury Band"), ("c1", "Phish"),
                ("none", None)]
    for rel, artist in readings:
        _db.session.add(BulkIngestItem(run_id=run.id, rel_path=rel, status="review",
                                       meta=json.dumps({"artist": artist})))
    _db.session.add(Artist(name="Phish"))                   # c1's act is confirmed already
    _db.session.commit()
    invalidate_library_cache()
    ids = {i.rel_path: i.id for i in _db.session.query(BulkIngestItem).all()}
    rows = [(i.id, i.meta) for i in _db.session.query(BulkIngestItem).order_by(BulkIngestItem.id)]
    order = bir.lead_first(rows)
    names = {v: k for k, v in ids.items()}
    assert [names[i] for i in order] == ["a1", "b1", "a2", "a3", "b2", "c1", "none"]
    assert bir.lead_first(rows) == order                    # stable

    c = app.test_client()
    from app.models.user import User
    user = _db.session.query(User).filter_by(username="admin").first()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    body = c.get(f"/api/bulk-ingest/{run.id}/items?status=review").get_json()
    assert [it["rel_path"] for it in body["items"]] == ["a1", "b1", "a2", "a3", "b2", "c1", "none"]
    page2 = c.get(f"/api/bulk-ingest/{run.id}/items?status=review&per_page=2&page=2").get_json()
    assert [it["rel_path"] for it in page2["items"]] == ["a2", "a3"]
    # other filters keep discovery order
    allrows = c.get(f"/api/bulk-ingest/{run.id}/items?status=queue").get_json()
    assert [it["rel_path"] for it in allrows["items"]] == [r for r, _ in readings]


def test_a_reading_that_resolves_through_an_alias_is_not_a_lead(rows):
    v, a = rows
    learn_aliases(_data(_rr(None, "Jerry Garsia Band")), a, v)
    _db.session.commit()
    meta = lambda n: json.dumps({"artist": n})
    order = bir.lead_first([(1, meta("Jerry Garsia Band")), (2, meta("Del McCoury Band")),
                            (3, meta("Jerry Garcia Band Trio"))])
    assert order == [2, 1, 3]       # alias and act-core readings are confirmed; Del leads


def test_rows_waiting_on_something_else_are_not_rechecked(env):
    _three_shows(env)
    run = _run(env, "auto")
    b1 = _item(run, "B1")
    b1.reason = "needs_date"
    _db.session.commit()
    keys = {"artist": {norm_key("Del McCoury Band")}, "venue": {norm_key("Wilkes Community College")}}
    assert bir.recheck_queue(str(env.lib / "A1"), keys) == [_item(run, "A2").id]


def test_confirmed_keys_cover_the_saved_name_the_reading_and_the_quote(rows):
    v, a = rows
    keys = confirmed_keys(_data(_rr("Wilkes Commnity College", "Jerry Garsia Band")), a, v, None)
    assert keys["venue"] == {"merlefest grounds", "wilkes commnity college"}
    assert keys["artist"] == {"jerry garcia band", "jerry garsia band"}
    assert "event" not in keys
