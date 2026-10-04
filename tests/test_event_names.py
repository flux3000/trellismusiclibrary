"""
tests/test_event_names.py -- event text is tidied, matched by a normalised key, and never
created by an unattended ingest (Resolver v2, chunk 4).
"""
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.event import Event
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.user import User
from app.utils.event_names import clean_event_name, event_key
from app.utils.ingest import title_case
from app.utils.resolve import resolve
from app.utils.resolver_eval import build_scan


# ── 1a: tidy the text ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("30Th Telluride Bluegrass Festival", "Telluride Bluegrass Festival"),
    ("3rd International Jazz Festival", "International Jazz Festival"),
    ("27. Internationale Jazzwoche", "Internationale Jazzwoche"),
    ("2nd Annual Spirit of the Suwanee Springfest", "Spirit of the Suwanee Springfest"),
    ("(Merlefest Hillside Jam)", "Merlefest Hillside Jam"),
    ("Part of the Big Ears Music Festival", "Big Ears Music Festival"),
    ("Grey Fox Bluegrass Festival", "Grey Fox Bluegrass Festival"),
])
def test_event_text_is_tidied(raw, want):
    assert clean_event_name(raw) == want


@pytest.mark.parametrize("raw", ["Festival", "Fest", "festival", "(Festival)", "The Show", "2024", "", None])
def test_a_bare_generic_word_is_not_an_event(raw):
    assert clean_event_name(raw) is None


def test_ordinals_keep_their_casing_when_title_cased():
    assert title_case("3rd international jazz festival") == "3rd International Jazz Festival"
    assert title_case("THE 30TH ANNUAL FEST") == "The 30th Annual Fest"
    assert "3Rd" not in title_case("festival 3rd stage")


def test_the_reader_hands_over_tidy_events():
    r = resolve(build_scan({"info_text": "Tony Rice\n1998-06-20\n30th Telluride Bluegrass Festival\nTelluride, CO\n",
                            "folder_name": ""}))
    assert r.event.value == "Telluride Bluegrass Festival"
    r = resolve(build_scan({"info_text": "Tony Rice\n1998-06-20\nFestival\nTelluride, CO\n", "folder_name": ""}))
    assert r.event.value is None


# ── 1b: one key for the spellings of one event ───────────────────────────────

def test_spellings_of_one_event_share_a_key():
    names = ["Telluride Bluegrass Festival", "30th Telluride Bluegrass Festival", "Telluride BG Festival",
             "telluride bluegrass festival 1998", "(Telluride Bluegrass Festival)"]
    assert len({event_key(n) for n in names}) == 1
    assert event_key("Suwannee Springfest") == event_key("Suwannee Spring Fest") == event_key("Suwannee SpringFest")
    assert event_key("Newport Jazz Fest") == event_key("Newport Jazz Festival")
    assert event_key("Mid-Summer Bluegrass Festival") == event_key("MidSummer Bluegrass Festival")


def test_different_events_keep_different_keys():
    assert event_key("Telluride Bluegrass Festival") != event_key("Telluride Jazz Festival")
    assert event_key("Newport Jazz Festival") != event_key("Newport Folk Festival")
    assert event_key("Festival") == ""


# ── 1b/1c: linking and creating ──────────────────────────────────────────────

def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _show(tmp_path, event_line):
    show = tmp_path / "import" / "Tony Rice - 1998-06-20"
    for i in (1, 2):
        _flac(show / f"{i:02d}.flac", ARTIST="Tony Rice", DATE="1998")
    (show / "info.txt").write_text(f"Tony Rice\n1998-06-20\n{event_line}\nTelluride, CO\n")
    return show


def _ingest(app, tmp_path, event_line, **kw):
    from app.api import ingest
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = _db.session.query(User).first().id
    out = ingest.auto_confirm(str(_show(tmp_path, event_line)), uid, **kw)
    assert out["status"] == "ingested", out
    rec = _db.session.get(Recording, out["result"]["recording_id"])
    return _db.session.get(Performance, rec.performance_id)


def test_unattended_ingest_links_a_variant_to_the_existing_event(app, tmp_path):
    ev = Event(name="Telluride Bluegrass Festival")
    _db.session.add(ev)
    _db.session.flush()
    before = _db.session.query(Event).count()
    perf = _ingest(app, tmp_path, "30th Telluride BG Festival")
    assert perf.event_id == ev.id
    assert _db.session.query(Event).count() == before


def test_unattended_ingest_never_creates_an_event(app, tmp_path):
    before = _db.session.query(Event).count()
    perf = _ingest(app, tmp_path, "Brand New Bluegrass Festival")
    assert perf.event_id is None
    assert _db.session.query(Event).count() == before


def test_a_blanket_event_typed_by_a_person_may_create(app, tmp_path):
    perf = _ingest(app, tmp_path, "Some Other Festival", applied={"event": "Typed By Hand Festival"})
    assert perf.event is not None and perf.event.name == "Typed By Hand Festival"


def test_existing_events_are_found_by_key(app, tmp_path):
    from app.api.ingest import _find_event
    ev = Event(name="Typed In The Wizard Festival")
    _db.session.add(ev)
    _db.session.flush()
    assert _find_event("12th Typed In The Wizard Fest").id == ev.id
    assert _find_event("Wizard Festival") is None
    assert _find_event("Festival") is None
