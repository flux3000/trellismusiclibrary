"""
tests/test_audio_convert.py — SHN/WAV detection, the conversion endpoint's
guards, and the Top Shelf's ordered sequence.

No ffmpeg is invoked anywhere here.  `convert_folder` is a thin loop around a
subprocess call; what is worth pinning is everything AROUND it — which folders
get offered a conversion at all, what the endpoint refuses, and the ordering
guarantee the Top Shelf's record bin now depends on.
"""

import os

import pytest

from app.extensions import db as _db
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.recording import Recording
from app.models.track import Track
from app.models.user import User
from app.utils.audio_convert import (ORIGINALS_DIRNAME, convertible_files,
                                     detect_convertible)


def _login_as(client, username="admin"):
    from app.extensions import login_manager
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


@pytest.fixture()
def client(app):
    c = app.test_client()
    _login_as(c)
    return c


def _folder(tmp_path, *names):
    d = tmp_path / "show"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"\0" * 16)
    return str(d)


# ═════════════════════════════════════════════════════════════════════════════
# detect_convertible
# ═════════════════════════════════════════════════════════════════════════════
def test_shn_folder_is_offered_a_conversion(tmp_path):
    got = detect_convertible(_folder(tmp_path, "d1t01.shn", "d1t02.shn"))
    assert got == {"kind": "shn", "ext": ".shn", "count": 2}


def test_wav_folder_is_offered_a_conversion(tmp_path):
    got = detect_convertible(_folder(tmp_path, "01.wav", "02.wav", "03.wav"))
    assert got == {"kind": "wav", "ext": ".wav", "count": 3}


def test_any_flac_present_means_no_offer(tmp_path):
    """
    The one rule that separates "needs converting" from "already converted,
    or deliberately mixed".  Without it, re-scanning a folder right after a
    successful conversion would offer to convert it all over again — the
    originals are still on disk under _originals/, just not in the root.
    """
    assert detect_convertible(_folder(tmp_path, "01.flac", "02.shn")) is None


def test_originals_subfolder_is_not_counted(tmp_path):
    """
    Post-conversion shape: FLACs in the root, sources tucked underneath.
    detect_convertible only ever looks at the ROOT, which is also the only
    place resolve_shows/scan_folder look — that alignment is what makes
    `_originals/` invisible to ingest rather than a second copy of the show.
    """
    d = _folder(tmp_path, "01.flac")
    sub = os.path.join(d, ORIGINALS_DIRNAME)
    os.makedirs(sub)
    open(os.path.join(sub, "01.shn"), "wb").close()
    assert detect_convertible(d) is None


def test_shn_wins_over_wav_in_a_mixed_folder(tmp_path):
    """SHN cannot be ingested at all; WAV merely costs disk. Fix first."""
    got = detect_convertible(_folder(tmp_path, "a.shn", "b.wav"))
    assert got["kind"] == "shn"


def test_ordinary_flac_folder_and_empty_folder_are_both_silent(tmp_path):
    assert detect_convertible(_folder(tmp_path, "01.flac")) is None
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert detect_convertible(str(empty)) is None


def test_convertible_files_is_stable_and_extension_scoped(tmp_path):
    d = _folder(tmp_path, "b.shn", "a.shn", "notes.txt")
    assert convertible_files(d, ".shn") == ["a.shn", "b.shn"]


# ═════════════════════════════════════════════════════════════════════════════
# The endpoint's guards
# ═════════════════════════════════════════════════════════════════════════════
def test_convert_refuses_a_folder_outside_the_import_roots(app, client, tmp_path):
    d = _folder(tmp_path, "01.shn")
    r = client.post("/api/quality/convert", json={"folder_path": d})
    # 403 (outside the roots) is the expected answer for a tmp path; what must
    # NOT happen is a job starting on an arbitrary directory.
    assert r.status_code in (400, 403)
    assert "job_id" not in (r.get_json() or {})


def test_convert_refuses_a_missing_folder(app, client):
    r = client.post("/api/quality/convert",
                    json={"folder_path": "/nope/not/here"})
    assert r.status_code == 400


def test_convert_status_of_an_unknown_job_is_404(app, client):
    assert client.get("/api/quality/convert/deadbeef").status_code == 404


def test_convert_requires_login(app):
    c = app.test_client()
    r = c.post("/api/quality/convert", json={"folder_path": "/tmp"})
    assert r.status_code in (401, 403)


# ═════════════════════════════════════════════════════════════════════════════
# Top Shelf: the ordered sequence behind the record bin
# ═════════════════════════════════════════════════════════════════════════════
def _make_top(db, performer_name, n):
    p = Performer(name=performer_name)
    db.session.add(p); db.session.flush()
    out = []
    for i in range(n):
        perf = Performance(performer_id=p.id, start_year=1990 + i,
                           start_month=1, start_day=1)
        db.session.add(perf); db.session.flush()
        rec = Recording(performance_id=perf.id, source="SBD", quality="A",
                        folder_path=f"{performer_name}/{i}")
        db.session.add(rec); db.session.flush()
        db.session.add(Track(recording_id=rec.id, track_number=1, title="One",
                             duration=100, file_path="01.flac"))
        out.append(rec.id)
    return out


def test_offset_walks_the_pool_without_repeating(app, client, db):
    """
    The bin deals each record once. Flipping right N times must never hand
    back something already on the shelf — that is the whole promise of
    'one pass, no repeats'.
    """
    _make_top(db, "Act One", 2)
    _make_top(db, "Act Two", 2)
    _make_top(db, "Act Three", 1)
    db.session.commit()

    seen = []
    for off in range(12):
        r = client.get(f"/api/recordings/recommended?limit=1&offset={off}")
        assert r.status_code == 200
        got = r.get_json()
        if not got:
            break
        seen.append(got[0]["id"])

    assert len(seen) == len(set(seen)), "a recording was dealt twice"
    assert len(seen) == 5, "the whole A/A+ pool should be reachable"


def test_the_bin_runs_out_and_says_so(app, client, db):
    """
    An empty answer past the end is what makes the right-hand chevron
    disappear. If this ever started wrapping around instead, the control would
    never go away and the shelf would silently repeat itself.
    """
    _make_top(db, "Only Act", 1)
    db.session.commit()
    r = client.get("/api/recordings/recommended?limit=1&offset=50")
    assert r.status_code == 200
    assert r.get_json() == []


def test_offset_zero_is_still_the_old_single_draw(app, client, db):
    """
    Round one of the sequence IS the historic six-tile draw, which is why the
    existing diversity test still holds. Four shows by one act, and at most one
    of them may appear.
    """
    made = _make_top(db, "Prolific", 4)
    db.session.commit()
    r = client.get("/api/recordings/recommended?limit=3&offset=0")
    ids = [x["id"] for x in r.get_json()]
    assert len(set(ids) & set(made)) <= 1


def test_a_performer_does_not_come_round_until_the_others_have(app, client, db):
    """
    The bin is rounds, not a shuffled list: with two acts of two shows each,
    the first two flips must be different acts.
    """
    a = set(_make_top(db, "Act A", 2))
    b = set(_make_top(db, "Act B", 2))
    db.session.commit()

    first_two = []
    for off in (0, 1):
        got = client.get(f"/api/recordings/recommended?limit=1&offset={off}").get_json()
        assert got
        first_two.append(got[0]["id"])

    assert (first_two[0] in a) != (first_two[1] in a), \
        "both of the first two flips came from the same performer"
    assert len(set(first_two) & (a | b)) == 2
