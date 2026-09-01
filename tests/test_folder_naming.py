"""
tests/test_folder_naming.py — unique_folder_name() and the two callers that
share it: move_to_library() at ingest time (see test_ingest_utils.py for the
filesystem-level ingest cases) and rename_recording_folder() when metadata is
edited post-ingest.

2026-09-01: unique_folder_name() was extracted out of rename_recording_folder()
so BOTH paths dedupe an on-disk name collision the same way — before this,
only the rename path did, and ingest would silently merge a second recording's
files into an existing folder of the same canonical name (Ryman Auditorium
1964 bug report: two "Various Artists" shows, same date, same venue, no
Source to distinguish them).

rename_recording_folder() only touches attributes via dot-access (never an
ORM-specific method), so it's exercised here with plain duck-typed stand-ins
rather than real SQLAlchemy models/DB — pure filesystem + object logic, same
spirit as test_ingest_utils.py.
"""

from types import SimpleNamespace

from app.utils.folder_naming import unique_folder_name, rename_recording_folder


# ── unique_folder_name() ───────────────────────────────────────────────────────

def test_unique_folder_name_no_collision_returns_name_unchanged(tmp_path):
    assert unique_folder_name(str(tmp_path), "Show") == "Show"


def test_unique_folder_name_first_collision_appends_bare_2(tmp_path):
    (tmp_path / "Show").mkdir()
    assert unique_folder_name(str(tmp_path), "Show") == "Show (2)"


def test_unique_folder_name_appends_inside_existing_parens(tmp_path):
    """"... (SBD)" colliding gets "... (SBD 2)", not a second trailing
    paren group — matches Ryan's existing manual naming practice."""
    (tmp_path / "Show (SBD)").mkdir()
    assert unique_folder_name(str(tmp_path), "Show (SBD)") == "Show (SBD 2)"


def test_unique_folder_name_walks_past_multiple_collisions(tmp_path):
    (tmp_path / "Show").mkdir()
    (tmp_path / "Show (2)").mkdir()
    assert unique_folder_name(str(tmp_path), "Show") == "Show (3)"


def test_unique_folder_name_keep_abs_excludes_self(tmp_path):
    """Renaming a folder back to its OWN current name (e.g. metadata saved
    with nothing folder-relevant actually changed) must not count as a
    collision against itself."""
    existing = tmp_path / "Show"
    existing.mkdir()
    assert unique_folder_name(str(tmp_path), "Show", keep_abs=str(existing)) == "Show"


def test_unique_folder_name_keep_abs_still_dedupes_other_folders(tmp_path):
    """keep_abs only exempts the folder's own path — a DIFFERENT existing
    folder with the same target name is still a real collision."""
    (tmp_path / "Show").mkdir()
    other = tmp_path / "Old Name"
    other.mkdir()
    assert unique_folder_name(str(tmp_path), "Show", keep_abs=str(other)) == "Show (2)"


# ── rename_recording_folder() ──────────────────────────────────────────────────

def _venue(name="Ryman Auditorium", city="Nashville", state="TN", country=None):
    return SimpleNamespace(name=name, city=city, state=state, country=country)


def _performance(performer, venue, start_year=1964, start_month=None, start_day=None,
                  end_year=None, end_month=None, end_day=None):
    return SimpleNamespace(
        performer=performer, venue=venue,
        start_year=start_year, start_month=start_month, start_day=start_day,
        end_year=end_year, end_month=end_month, end_day=end_day,
        city=None, state=None, country=None,
    )


def _recording(folder_path, performance, source=None):
    rec = SimpleNamespace(folder_path=folder_path, source=source, performance=performance)
    return rec


def test_rename_dedupes_against_a_different_recordings_folder(tmp_path):
    """Editing recording B's metadata to match recording A's exactly (the
    "Write tags to files" scenario in the bug report) must rename B's folder
    to a "(2)" variant, never collide with — or merge into — A's."""
    lib = tmp_path
    artist_dir = lib / "Various Artists"
    artist_dir.mkdir()
    (artist_dir / "Various Artists - 1964 - Ryman Auditorium - Nashville, TN").mkdir()
    stale = artist_dir / "Various Artists - 1964 - Ryman Auditorium - Unknown Location"
    stale.mkdir()

    performer = SimpleNamespace(name="Various Artists")
    venue = _venue()
    performance = _performance(performer, venue)
    rec = _recording(
        "Various Artists/Various Artists - 1964 - Ryman Auditorium - Unknown Location",
        performance)

    err = rename_recording_folder(rec, str(lib))

    assert err is None
    assert rec.folder_path == "Various Artists/Various Artists - 1964 - Ryman Auditorium - Nashville, TN (2)"
    assert not stale.exists()
    assert (artist_dir / "Various Artists - 1964 - Ryman Auditorium - Nashville, TN").exists()
    assert (artist_dir / "Various Artists - 1964 - Ryman Auditorium - Nashville, TN (2)").exists()


def test_rename_no_op_when_name_already_matches_metadata(tmp_path):
    """The common case on every save — folder already correctly named — must
    not touch the filesystem or grow a spurious suffix."""
    lib = tmp_path
    artist_dir = lib / "Various Artists"
    artist_dir.mkdir()
    correct = artist_dir / "Various Artists - 1964 - Ryman Auditorium - Nashville, TN"
    correct.mkdir()

    performer = SimpleNamespace(name="Various Artists")
    venue = _venue()
    performance = _performance(performer, venue)
    rec = _recording(
        "Various Artists/Various Artists - 1964 - Ryman Auditorium - Nashville, TN",
        performance)

    err = rename_recording_folder(rec, str(lib))

    assert err is None
    assert rec.folder_path == "Various Artists/Various Artists - 1964 - Ryman Auditorium - Nashville, TN"
    assert correct.exists()
