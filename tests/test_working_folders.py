"""
tests/test_working_folders.py: the working-folder pages (2026-10-06).

A complete folder must never read as incomplete because its MD5 list also names
artwork and text files, or because the list was written in Windows-1252. A
folder with no audio says so, and Move to Trash resolves its path server-side.
"""
from app.utils import checksums
from app.utils import trash
from tests.test_archive_downloads import dl, _login  # noqa: F401  (fixture)


def _folder(root, name, files, md5_bytes):
    d = root / name
    d.mkdir()
    for f in files:
        (d / f).write_bytes(b"x")
    (d / f"{name}.md5").write_bytes(md5_bytes)
    checksums._fp_state_cache.clear()
    return d


def test_md5_listing_artwork_is_not_incomplete(tmp_path):
    d = _folder(tmp_path, "show", ["01 a.flac", "02 b.flac", "cover.jpg", "info.txt"],
                b"0" * 32 + b" *01 a.flac\n" + b"1" * 32 + b" *02 b.flac\n"
                + b"2" * 32 + b" *cover.jpg\n" + b"3" * 32 + b" *info.txt\n")
    assert checksums.folder_fingerprint_state(str(d))["state"] == "unchecked"


def test_md5_in_windows_1252_matches_accented_names(tmp_path):
    d = _folder(tmp_path, "show", ["03. Natiembé - Song.flac"],
                ("4" * 32 + " *03. Natiembé - Song.flac\n").encode("cp1252"))
    assert checksums.folder_fingerprint_state(str(d))["state"] == "unchecked"


def test_md5_naming_a_missing_track_is_not_a_problem(tmp_path):
    d = _folder(tmp_path, "show", ["01 a.flac"],
                b"0" * 32 + b" *01 a.flac\n" + b"1" * 32 + b" *02 b.flac\n")
    assert checksums.folder_fingerprint_state(str(d))["state"] == "unchecked"


def test_folder_without_audio_is_flagged(dl):  # noqa: F811
    _login(dl.client)
    (dl.root / "notes").mkdir()
    (dl.root / "notes" / "info.txt").write_text("hi")
    rows = {f["name"]: f for f in dl.client.get("/api/downloads/folder").get_json()["folders"]}
    assert rows["notes"]["has_audio"] is False


def test_move_to_trash_resolves_inside_the_folder(dl, monkeypatch):  # noqa: F811
    _login(dl.client)
    moved = []
    monkeypatch.setattr(trash, "move_to_trash", lambda p: moved.append(p))
    (dl.root / "show").mkdir()
    assert dl.client.post("/api/downloads/trash", json={"name": "../etc"}).status_code == 404
    assert dl.client.post("/api/downloads/trash", json={"name": "nope"}).status_code == 404
    assert dl.client.post("/api/downloads/trash", json={"which": "x", "name": "show"}).status_code == 400
    r = dl.client.post("/api/downloads/trash", json={"which": "downloads", "name": "show"})
    assert r.status_code == 200 and moved == [str(dl.root / "show")]


def test_move_to_trash_unavailable_is_reported(dl, monkeypatch):  # noqa: F811
    def boom(p):
        raise trash.TrashUnavailable("Move to Trash is not available on this computer.")
    monkeypatch.setattr(trash, "move_to_trash", boom)
    _login(dl.client)
    (dl.root / "show").mkdir()
    r = dl.client.post("/api/downloads/trash", json={"name": "show"})
    assert r.status_code == 501
