import subprocess
import sys

import pytest

from app.utils import startup_error as se
from app.utils.db_upgrade import DatabaseTooNewError, DatabaseUpgradeError


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path))
    return tmp_path


def test_upgrade_errors_pass_their_own_text_through(data):
    for cls in (DatabaseUpgradeError, DatabaseTooNewError):
        assert se.message_for(cls("exact approved text"), data / "x.log") == "exact approved text"


def test_other_errors_get_the_generic_message(data):
    msg = se.message_for(ValueError("boom"), data / "x.log")
    assert msg == f"Trellis could not start. Details were saved to {data / 'x.log'}."
    assert "boom" not in msg


def test_osascript_gets_argv_list_with_message_verbatim(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(se.subprocess, "run", lambda args, **kw: calls.append(args))
    nasty = 'He said "hi" \\ and\nmore'
    se.show_startup_error(nasty)
    args = calls[0]
    assert isinstance(args, list) and args[0] == "osascript"
    assert args[-1] == nasty
    assert "critical" in " ".join(args)


def test_never_raises_when_osascript_missing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError("osascript")
    monkeypatch.setattr(se.subprocess, "run", boom)
    se.show_startup_error("hello")
    assert "hello" in capsys.readouterr().err


def test_log_written_with_traceback(data):
    try:
        raise ValueError("kaput")
    except ValueError as e:
        path = se.write_log(e)
    assert path == data / "startup-error.log"
    text = path.read_text()
    assert "ValueError: kaput" in text and "Traceback" in text


def test_handle_does_not_show_from_source(monkeypatch):
    shown = []
    monkeypatch.setattr(se, "show_startup_error", shown.append)
    monkeypatch.delenv("TRELLIS_SHOW_STARTUP_ERRORS", raising=False)
    se.handle_startup_failure(ValueError("x"))
    assert shown == []
    monkeypatch.setenv("TRELLIS_SHOW_STARTUP_ERRORS", "1")
    se.handle_startup_failure(DatabaseTooNewError("newer"))
    assert shown == ["newer"]


def test_handle_shows_when_frozen(monkeypatch, data):
    shown = []
    monkeypatch.setattr(se, "show_startup_error", shown.append)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    se.handle_startup_failure(ValueError("x"))
    assert shown == [f"Trellis could not start. Details were saved to {data / 'startup-error.log'}."]
