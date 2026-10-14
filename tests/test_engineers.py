"""tests/test_engineers.py -- the Engineer feature (backend): model + API CRUD,
find-or-create by name/alias, resolver extraction (tags, info file, COMMENT),
gear rejection, FLAC and MP3 tag round trips, studio records ignored, the
recording payload and ingest confirm, search, and peer parity."""
import numpy as np
import pytest
import soundfile as sf
from mutagen.flac import FLAC

from app.extensions import db as _db
from app.models.engineer import Engineer
from app.models.alias import EngineerAlias
from app.models.recording import Recording
from app.utils.engineers import (extract_engineers, find_or_create_engineer, find_engineer,
                                 apply_engineer_names, engineer_tag_values, parse_engineer_values)
from app.utils.ingest import (build_scan_payload, read_flac_tags, write_flac_tags,
                              desired_track_tags, open_tags)
from app.utils.resolve import resolve, verdict


def _login(client):
    from app.models.user import User
    u = _db.session.query(User).filter_by(username="admin").first()
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    assert client.get("/api/collections/").status_code == 200


def _rec(seeded_ids):
    return _db.session.get(Recording, seeded_ids["recording_id"])


# ── find-or-create ───────────────────────────────────────────────────────────

def test_find_or_create_by_name_and_alias(app):
    a = find_or_create_engineer("Bornfree")
    _db.session.commit()
    assert find_or_create_engineer("  BORN free ").id == a.id        # case/space-insensitive
    _db.session.add(EngineerAlias(engineer_id=a.id, alias="Born Free Taper", alias_key="bornfreetaper"))
    _db.session.commit()
    assert find_engineer("born-free taper").id == a.id               # by alias
    assert find_or_create_engineer("") is None
    assert _db.session.query(Engineer).count() == 1


# ── API ──────────────────────────────────────────────────────────────────────

def test_engineer_api_crud_aliases_and_recordings(app, seeded_ids):
    c = app.test_client(); _login(c)
    r = c.post("/api/engineers/", json={"name": "Charlie Miller"})
    assert r.status_code == 201
    eid = r.get_json()["id"]
    assert c.post("/api/engineers/", json={"name": "charlie  miller"}).status_code == 200   # existing
    assert c.post("/api/engineers/", json={"name": ""}).status_code == 400

    # alias
    ar = c.post(f"/api/engineers/{eid}/aliases", json={"alias": "Chuck Miller"})
    assert ar.status_code == 201
    assert [x["alias"] for x in c.get(f"/api/engineers/{eid}").get_json()["aliases"]] == ["Chuck Miller"]
    assert c.get("/api/engineers/search?q=chuck").get_json()[0]["id"] == eid
    other = c.post("/api/engineers/", json={"name": "Someone Else"}).get_json()["id"]
    assert c.post(f"/api/engineers/{other}/aliases", json={"alias": "chuck miller"}).status_code == 409

    # credit it on a recording via PUT, in two roles
    rid = seeded_ids["recording_id"]
    assert c.put(f"/api/recordings/{rid}", json={"transfer_engineer": "Chuck Miller",
                                                 "mastering_engineer": "Charlie Miller"}).status_code == 200
    rec = c.get(f"/api/recordings/{rid}").get_json()
    assert rec["transfer_engineer"] == {"id": eid, "name": "Charlie Miller"}
    assert rec["mastering_engineer"]["id"] == eid
    assert rec["recording_engineer"] is None
    assert _db.session.query(Engineer).count() == 2                 # alias resolved, nothing new made

    detail = c.get(f"/api/engineers/{eid}").get_json()
    assert detail["recording_count"] == 1
    assert detail["recordings"][0]["id"] == rid
    assert sorted(detail["recordings"][0]["roles"]) == ["mastering", "transfer"]
    lst = c.get("/api/engineers/?index=1").get_json()
    row = next(x for x in lst if x["id"] == eid)
    assert row["recording_count"] == 1 and "excerpt" in row

    # empty string clears; update; delete clears the credits
    assert c.put(f"/api/recordings/{rid}", json={"transfer_engineer": ""}).status_code == 200
    assert c.get(f"/api/recordings/{rid}").get_json()["transfer_engineer"] is None
    assert c.put(f"/api/engineers/{eid}", json={"bio": "Taper"}).status_code == 200
    assert c.delete(f"/api/engineers/{eid}").status_code == 200
    assert c.get(f"/api/recordings/{rid}").get_json()["mastering_engineer"] is None
    assert c.get(f"/api/engineers/{eid}").status_code == 404


def test_studio_recording_has_no_engineers(app, seeded_ids):
    c = app.test_client(); _login(c)
    rec = _rec(seeded_ids)
    rec.kind = "studio"
    _db.session.commit()
    assert apply_engineer_names(rec, {"recording_engineer": "Bornfree"}) is False
    c.put(f"/api/recordings/{rec.id}", json={"recording_engineer": "Bornfree"})
    payload = c.get(f"/api/recordings/{rec.id}").get_json()
    assert "recording_engineer" not in payload
    assert rec.recording_engineer_id is None
    assert engineer_tag_values(rec) == []


# ── resolver extraction ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Taped by Bornfree", {"recording": "Bornfree"}),
    ("Recorded by: Rob Eaton - Schoeps MK4 > Sony D5 > DAT", {"recording": "Rob Eaton"}),
    ("Transfer: Charlie Miller", {"transfer": "Charlie Miller"}),
    ("Mastered by Jamie Waddell", {"mastering": "Jamie Waddell"}),
    ("Source: SBD > DAT; recorded & transferred by MIKE", {"recording": "MIKE", "transfer": "MIKE"}),
    ("Recorded and mastered by Jim Smith (jim@example.com)", {"recording": "Jim Smith", "mastering": "Jim Smith"}),
    ("Taper - Dan Lynch using Nak 300", {"recording": "Dan Lynch"}),
    ("Seeded by Somebody\nUploaded by Other", {}),
])
def test_extract_engineers(text, expected):
    assert extract_engineers(text) == expected


@pytest.mark.parametrize("text", [
    "Transfer: Nakamichi CM300 > Sony D5 DAT > CD", "Taper: DPA 4023", "Recorded by: Schoeps MK4",
    "Lineage: DAT > transfer > flac", "Taped by 1234", "Recorded by: Neumann KM184",
    "Taped by " + "x" * 60,
])
def test_gear_is_rejected(text):
    assert extract_engineers(text) == {}


def _flac(path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(4410, dtype="int16"), 44100, format="FLAC")
    a = FLAC(str(path))
    for k, v in tags.items():
        a[k] = v
    a.save()


def _show(tmp_path, info=None, **tags):
    show = tmp_path / "Test Band - 1979-06-14 - Stars - Philadelphia, PA (SBD)"
    tags = {"ARTIST": "Test Band", "DATE": "1979-06-14", **tags}
    _flac(show / "01.flac", **tags)
    if info:
        (show / "info.txt").write_text(info)
    return show


def test_resolver_sources_info_tags_comment(tmp_path):
    info = "Test Band\n1979-06-14\nStars - Philadelphia, PA\nTaped by Bornfree\nTransfer: Charlie Miller\n"
    r = resolve(build_scan_payload(str(_show(tmp_path, info))), library_root=str(tmp_path))
    assert (r.recording_engineer.value, r.recording_engineer.source) == ("Bornfree", "info")
    assert r.transfer_engineer.value == "Charlie Miller"
    assert r.mastering_engineer.value is None
    assert set(r.to_dict()) >= {"recording_engineer", "transfer_engineer", "mastering_engineer"}
    assert not any("engineer" in x for x in verdict(r)[1])          # never a review reason


def test_resolver_tags_beat_info_and_comment_is_read(tmp_path):
    show = _show(tmp_path / "a", "Taped by Bornfree\n", ENGINEER=["Rob Eaton (recording)", "Jamie W (mastering)"])
    r = resolve(build_scan_payload(str(show)), library_root=str(tmp_path))
    assert (r.recording_engineer.value, r.recording_engineer.source) == ("Rob Eaton", "tags")
    assert r.recording_engineer.candidates == {"tags": "Rob Eaton", "info": "Bornfree"}
    assert r.mastering_engineer.value == "Jamie W"
    show = _show(tmp_path / "b", None, COMMENT="Mastered by Jamie Waddell")
    r = resolve(build_scan_payload(str(show)), library_root=str(tmp_path))
    assert (r.mastering_engineer.value, r.mastering_engineer.source) == ("Jamie Waddell", "comment")


def test_bare_engineer_tag_and_taper_tag(tmp_path):
    assert parse_engineer_values(["Bornfree"]) == {"recording": "Bornfree"}
    assert parse_engineer_values(["Bob (mixing)"]) == {}
    show = _show(tmp_path, None, TAPER="Dan Lynch", TRANSFER="Charlie Miller")
    r = resolve(build_scan_payload(str(show)), library_root=str(tmp_path))
    assert r.recording_engineer.value == "Dan Lynch" and r.transfer_engineer.value == "Charlie Miller"


def test_resolver_ignores_studio(tmp_path):
    from app.utils.resolve import _resolve_engineer_fields
    scan = build_scan_payload(str(_show(tmp_path, "Taped by Bornfree\n")))
    assert _resolve_engineer_fields(scan, "studio")["recording_engineer"].value is None
    assert _resolve_engineer_fields(scan, "live")["recording_engineer"].value == "Bornfree"


# ── tag write / read round trips ─────────────────────────────────────────────

def _credit(rec):
    apply_engineer_names(rec, {"recording_engineer": "Bornfree", "transfer_engineer": "Charlie Miller",
                               "mastering_engineer": "Jamie Waddell"})
    _db.session.commit()


def test_flac_tag_round_trip(app, seeded_ids, tmp_path):
    rec = _rec(seeded_ids); _credit(rec)
    assert engineer_tag_values(rec) == ["Bornfree (recording)", "Charlie Miller (transfer)",
                                        "Jamie Waddell (mastering)"]
    assert desired_track_tags(rec)[0][1]["ENGINEER"] == engineer_tag_values(rec)
    files = []
    for i, t in enumerate(rec.tracks, 1):
        p = tmp_path / rec.folder_path / t.file_path
        _flac(p)
        files.append({"path": str(p), "index": i, "filename": p.name})
    assert write_flac_tags(rec, str(tmp_path)) == (2, [])
    assert FLAC(files[0]["path"])["ENGINEER"] == engineer_tag_values(rec)
    c = read_flac_tags(files)["container"]
    assert (c["recording_engineer"], c["transfer_engineer"], c["mastering_engineer"]) == \
        ("Bornfree", "Charlie Miller", "Jamie Waddell")


def test_mp3_tag_round_trip(app, seeded_ids, tmp_path):
    from test_flac_tag_taxonomy import _silent_mp3
    from mutagen.mp3 import MP3
    rec = _rec(seeded_ids); _credit(rec)
    paths = []
    for t in rec.tracks:
        f = tmp_path / rec.folder_path / (t.file_path.rsplit(".", 1)[0] + ".mp3")
        _silent_mp3(f); paths.append(f)
        t.file_path = f.name
    _db.session.commit()
    assert write_flac_tags(rec, str(tmp_path)) == (2, [])
    tipl = MP3(str(paths[0])).tags.get("TIPL")
    assert sorted(map(tuple, tipl.people)) == [("mastering engineer", "Jamie Waddell"),
                                               ("recording engineer", "Bornfree"),
                                               ("transfer engineer", "Charlie Miller")]
    assert open_tags(str(paths[0]))["ENGINEER"] == engineer_tag_values(rec)
    files = [{"path": str(p), "index": i, "filename": p.name} for i, p in enumerate(paths, 1)]
    assert read_flac_tags(files)["container"]["transfer_engineer"] == "Charlie Miller"


# ── ingest confirm ───────────────────────────────────────────────────────────

def test_do_confirm_links_engineers(app, db, tmp_path):
    from app.api.ingest import _do_confirm
    from app.models.user import User
    src = tmp_path / "src"; _flac(src / "t01.flac")
    lib = tmp_path / "lib"; lib.mkdir()
    app.config["LIBRARY_ROOT"] = str(lib)
    uid = db.session.query(User).first().id
    base = {"artist_name": "Eng Test Act", "start_year": 1981, "start_month": 3, "start_day": 4,
            "tracks": [{"track_number": 1, "title": "One", "duration": 10, "filename": "t01.flac"}]}
    res = _do_confirm({**base, "source_folder_path": str(src), "recording_engineer": "Bornfree",
                       "transfer_engineer": "Bornfree", "mastering_engineer": ""}, uid, None)
    rec = db.session.get(Recording, res["recording_id"])
    assert rec.recording_engineer.name == "Bornfree"
    assert rec.transfer_engineer_id == rec.recording_engineer_id
    assert rec.mastering_engineer_id is None

    src2 = tmp_path / "src2"; _flac(src2 / "t01.flac")
    res = _do_confirm({**base, "artist_name": "Studio Act", "kind": "studio", "title": "An Album",
                       "source_folder_path": str(src2), "recording_engineer": "Bornfree"}, uid, None)
    assert db.session.get(Recording, res["recording_id"]).recording_engineer_id is None


# ── schema upgrade ───────────────────────────────────────────────────────────

def test_schema_upgrade_adds_columns_and_tables(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from app.utils.schema_upgrades import ensure_recording_engineer_columns
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, folder_path TEXT)")
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{path}")
    assert ensure_recording_engineer_columns(eng) == ["recording_engineer_id", "transfer_engineer_id",
                                                      "mastering_engineer_id"]
    assert ensure_recording_engineer_columns(eng) is None            # idempotent
    con = sqlite3.connect(path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"engineer", "engineer_image", "engineer_alias"} <= names
    con.close()


# ── search ───────────────────────────────────────────────────────────────────

def test_search_finds_engineers_by_name_and_alias(app):
    c = app.test_client(); _login(c)
    e = find_or_create_engineer("Bornfree")
    _db.session.add(EngineerAlias(engineer_id=e.id, alias="Zebulon", alias_key="zebulon"))
    _db.session.commit()
    for q in ("bornfree", "zebulon"):
        g = {x["type"]: x for x in c.get(f"/api/search/?q={q}").get_json()["groups"]}
        assert g["engineers"]["items"][0]["hash"] == f"#/engineer/{e.id}"


# ── peers ────────────────────────────────────────────────────────────────────

def test_peer_engineer_routes_scoped_to_visible_recordings(app, seeded_ids):
    from test_studio_sharing import _peer_granted_junction, _auth, _recording
    rec = _rec(seeded_ids)
    hidden = _recording("hidden")
    apply_engineer_names(rec, {"recording_engineer": "Visible Taper"})
    apply_engineer_names(hidden, {"recording_engineer": "Hidden Taper"})
    _db.session.commit()
    col, peer, raw = _peer_granted_junction(rec)
    c = app.test_client()
    lst = c.get("/api/share/engineers/", headers=_auth(raw)).get_json()
    assert [e["name"] for e in lst] == ["Visible Taper"]
    vid = lst[0]["id"]
    d = c.get(f"/api/share/engineers/{vid}", headers=_auth(raw)).get_json()
    assert [r["id"] for r in d["recordings"]] == [rec.id] and d["recordings"][0]["roles"] == ["recording"]
    hid = find_engineer("Hidden Taper").id
    assert c.get(f"/api/share/engineers/{hid}", headers=_auth(raw)).status_code == 403
    groups = c.get("/api/share/search?q=taper", headers=_auth(raw)).get_json()["groups"]
    eng = next(g for g in groups if g["type"] == "engineers")
    assert [i["name"] for i in eng["items"]] == ["Visible Taper"]
