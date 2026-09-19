"""
tests/test_install_epoch.py — write-once, backdated.

Overrides app.config["DB_PATH"] to a throwaway tmp_path rather than reusing
the shared `app` fixture's real Config.DB_PATH — same convention
test_user_profile.py uses for AVATAR_DIR.
"""

import json
from datetime import datetime, timezone

import pytest

from app.utils import install_epoch


@pytest.fixture()
def epoch_dir(app, tmp_path):
    db_file = tmp_path / "db" / "trellis.db"
    db_file.parent.mkdir()
    db_file.write_text("")
    app.config["DB_PATH"] = str(db_file)
    return tmp_path


def _read(tmp_path):
    return json.loads((tmp_path / "install.json").read_text())


def test_writes_when_absent(app, epoch_dir):
    install_epoch.ensure_install_epoch()
    data = _read(epoch_dir)
    assert data["schema"] == 1 and data["install_id"] and data["epoch_source"]


def test_does_not_rewrite_when_present(app, epoch_dir):
    install_epoch.ensure_install_epoch()
    first = _read(epoch_dir)
    (epoch_dir / "install.json").write_text(
        json.dumps(dict(first, install_id="untouched-marker")))

    install_epoch.ensure_install_epoch()
    assert _read(epoch_dir)["install_id"] == "untouched-marker"


@pytest.mark.parametrize("owner_dt, file_dt, expected_source, expected_epoch", [
    # Backdates from the owner row.
    (datetime(2020, 1, 1, tzinfo=timezone.utc), None,
     "owner_created_at", datetime(2020, 1, 1, tzinfo=timezone.utc)),
    # Backdates from the DB file's birthtime when there is no owner row.
    (None, datetime(2021, 6, 1, tzinfo=timezone.utc),
     "db_file_birthtime", datetime(2021, 6, 1, tzinfo=timezone.utc)),
    # Picks the earlier of the two when both are present.
    (datetime(2023, 3, 1, tzinfo=timezone.utc), datetime(2019, 3, 1, tzinfo=timezone.utc),
     "db_file_birthtime", datetime(2019, 3, 1, tzinfo=timezone.utc)),
], ids=["owner_row", "file_birthtime_no_owner", "earlier_of_two"])
def test_backdating_signals(app, epoch_dir, monkeypatch,
                             owner_dt, file_dt, expected_source, expected_epoch):
    monkeypatch.setattr(install_epoch, "_owner_created_at", lambda: owner_dt)
    monkeypatch.setattr(install_epoch, "_db_file_birthtime", lambda: file_dt)

    install_epoch.ensure_install_epoch()
    data = _read(epoch_dir)
    assert data["epoch_source"] == expected_source
    assert data["epoch"] == expected_epoch.isoformat()


def test_never_raises_when_write_fails(app, epoch_dir, monkeypatch):
    class _BoomPath:
        def exists(self):
            return False

        @property
        def parent(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(install_epoch, "_install_epoch_path", lambda: _BoomPath())
    install_epoch.ensure_install_epoch()  # must not raise
    assert not (epoch_dir / "install.json").exists()
