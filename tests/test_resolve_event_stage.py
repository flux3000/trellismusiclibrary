"""
tests/test_resolve_event_stage.py -- event and stage as resolver fields, learned-alias tables
(Resolver v2, chunk 4).

The festival is an Event, "Hillside Stage" is the performance's Stage, neither is the venue.
Resolved carries both; auto_confirm, _do_confirm and apply_blanket_values pass them to the
saved Performance; resolver_json stores them; venue_alias and artist_alias exist.
"""
import json

import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db
from app.models.alias import ArtistAlias, VenueAlias
from app.models.artist import Artist
from app.models.event import Event
from app.models.performance import Performance
from app.models.recording import Recording
from app.models.user import User
from app.models.venue import Venue
from app.utils.resolve import resolve
from app.utils.resolver_eval import build_scan

_INFO = "Jerry Douglas Band\n2003-07-18\nGrey Fox Bluegrass Festival\nHillside Stage\nAncramdale, NY\n"


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    audio = FLAC(str(path))
    for k, v in tags.items():
        audio[k] = v
    audio.save()


def _show(tmp_path, info=_INFO):
    show = tmp_path / "import" / "Jerry Douglas Band - 2003-07-18 - Grey Fox"
    for i in (1, 2):
        _flac(show / f"{i:02d}.flac", ARTIST="Jerry Douglas Band", DATE="2003")
    (show / "info.txt").write_text(info)
    return show


def _resolved(info=_INFO):
    return resolve(build_scan({"info_text": info, "folder_name": ""}))


def test_resolved_carries_event_and_stage_apart_from_the_venue():
    r = _resolved()
    assert r.event.value == "Grey Fox Bluegrass Festival" and r.event.source == "info"
    assert r.stage.value == "Hillside Stage" and r.stage.candidates == {"info": "Hillside Stage"}
    assert r.venue.value is None
    d = r.to_dict()
    assert d["event"]["value"] == "Grey Fox Bluegrass Festival" and d["stage"]["value"] == "Hillside Stage"


def test_resolved_event_and_stage_are_empty_when_the_text_has_none():
    r = _resolved("Some Band\n2001-06-02\nThe Rex Theatre\nToronto, Ontario, Canada\n")
    assert r.event.value is None and r.stage.value is None and r.event.source is None
    assert r.to_dict()["stage"] == {"value": None, "source": None, "candidates": {}, "conflict": False}


def test_resolver_json_for_storage_keeps_event_and_stage():
    from app.api.ingest import resolver_json_for_storage
    stored = json.loads(resolver_json_for_storage(_resolved().to_dict()))
    assert stored["event"]["value"] == "Grey Fox Bluegrass Festival"
    assert stored["stage"]["value"] == "Hillside Stage"
    assert "tracks" not in stored


def test_blanket_values_replace_event_and_stage_on_the_resolved_and_the_payload():
    from app.api.ingest import apply_blanket_values
    r = _resolved()
    payload = {}
    apply_blanket_values(r, payload, {"event": "Telluride Bluegrass Festival", "stage": "Main Stage"})
    assert (r.event.value, r.event.source) == ("Telluride Bluegrass Festival", "applied")
    assert (r.stage.value, r.stage.source) == ("Main Stage", "applied")
    assert payload["event_name"] == "Telluride Bluegrass Festival" and payload["event_id"] is None
    assert payload["stage"] == "Main Stage"
    r2 = _resolved()
    apply_blanket_values(r2, None, {"stage": "Barn"})            # before the payload exists
    assert r2.stage.value == "Barn" and r2.event.value == "Grey Fox Bluegrass Festival"


def test_bulk_run_accepts_a_blanket_stage():
    from app.api.bulk_ingest import _APPLIED_KEYS
    assert "stage" in _APPLIED_KEYS and "event" in _APPLIED_KEYS


def test_event_and_stage_round_trip_through_auto_confirm(app, tmp_path):
    from app.api import ingest
    lib = tmp_path / "library"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = _db.session.query(User).first().id
    _db.session.add(Event(name="Grey Fox Bluegrass Festival"))   # unattended ingest only links
    _db.session.flush()
    out = ingest.auto_confirm(str(_show(tmp_path)), uid)
    assert out["status"] == "ingested", out
    assert out["resolved"].event.value == "Grey Fox Bluegrass Festival"
    rec = _db.session.get(Recording, out["result"]["recording_id"])
    perf = _db.session.get(Performance, rec.performance_id)
    assert perf.stage == "Hillside Stage"
    assert perf.event is not None and perf.event.name == "Grey Fox Bluegrass Festival"
    assert out["result"]["stage"] == "Hillside Stage"
    stored = json.loads(rec.resolver_json)
    assert stored["stage"]["value"] == "Hillside Stage" and stored["event"]["value"] == "Grey Fox Bluegrass Festival"


def test_blanket_stage_wins_over_the_texts_through_auto_confirm(app, tmp_path):
    from app.api import ingest
    lib = tmp_path / "library"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = _db.session.query(User).first().id
    out = ingest.auto_confirm(str(_show(tmp_path)), uid, applied={"stage": "Barn Stage"})
    assert out["status"] == "ingested", out
    perf = _db.session.get(Performance, _db.session.get(Recording, out["result"]["recording_id"]).performance_id)
    assert perf.stage == "Barn Stage"


def test_a_folder_with_no_stage_saves_none(app, tmp_path):
    from app.api import ingest
    lib = tmp_path / "library"
    lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = _db.session.query(User).first().id
    out = ingest.auto_confirm(str(_show(tmp_path, "Jerry Douglas Band\n2003-07-18\nThe Rex Theatre\nToronto, Ontario, Canada\n")), uid)
    assert out["status"] == "ingested", out
    perf = _db.session.get(Performance, _db.session.get(Recording, out["result"]["recording_id"]).performance_id)
    assert perf.stage is None and perf.event_id is None


# ── learned-alias tables ─────────────────────────────────────────────────────

def test_alias_tables_exist_with_their_columns(app):
    cols = lambda t: {r[1] for r in _db.session.execute(_db.text(f"pragma table_info({t})"))}
    assert cols("venue_alias") == {"id", "venue_id", "alias", "alias_key", "created_at"}
    assert cols("artist_alias") == {"id", "artist_id", "alias", "alias_key", "created_at"}


def test_alias_rows_link_and_are_unique_per_key(app):
    v = Venue(name="Wilkes Community College")
    a = Artist(name="Doc Watson")
    _db.session.add_all([v, a])
    _db.session.flush()
    _db.session.add_all([VenueAlias(venue_id=v.id, alias="Wilkes Commnity College", alias_key="wilkes commnity college"),
                         ArtistAlias(artist_id=a.id, alias="Doc Watsen", alias_key="doc watsen")])
    _db.session.flush()
    assert [x.alias for x in v.aliases] == ["Wilkes Commnity College"]
    _db.session.add(VenueAlias(venue_id=v.id, alias="WILKES COMMNITY COLLEGE", alias_key="wilkes commnity college"))
    with pytest.raises(IntegrityError):
        _db.session.flush()
    _db.session.rollback()


def test_deleting_a_venue_deletes_its_aliases(app):
    v = Venue(name="Gone Hall")
    _db.session.add(v)
    _db.session.flush()
    _db.session.add(VenueAlias(venue_id=v.id, alias="Gone Hal", alias_key="gone hal"))
    _db.session.flush()
    _db.session.delete(v)
    _db.session.flush()
    assert _db.session.query(VenueAlias).count() == 0


def test_the_migration_script_names_the_columns_it_checks():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "migrate_add_aliases.py").read_text()
    assert "venue_alias" in src and "artist_alias" in src and "PRAGMA table_info" in src
