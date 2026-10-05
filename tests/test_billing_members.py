"""
Resolver v2 chunk 7b: a billing of several people ("X and Y") stays one Artist reading.
Splitting a billing into Musicians is postponed (Ryan, 2026-10-05): too many traps
("Peter Rowan and Hot Rize") and no way to measure how often it is right.
"""
import pytest

from app.utils.ingest import build_scan_payload
from app.utils.resolve import resolve


def _resolved(tmp_path, head):
    show = tmp_path / "show"
    show.mkdir(parents=True)
    (show / "info.txt").write_text(head + "\nTown Hall, New York, NY\nMarch 20, 1999\n\nSource: SBD\n\n"
                                   "01. Song One\n02. Song Two\n")
    (show / "01.flac").write_bytes(b"")
    return resolve(build_scan_payload(str(show)), library_root=str(tmp_path), placement=None)


@pytest.mark.parametrize("line", [
    "Emmylou Harris and Linda Ronstadt",
    "Doc Watson & Jack Lawrence",
    "Gillian Welch and David Rawlings",
])
def test_a_duo_billing_is_one_artist(tmp_path, line):
    r = _resolved(tmp_path, line)
    assert r.artist.value == line
    assert "members" not in r.to_dict()


def test_resolving_creates_no_musician_rows(app, tmp_path):
    from app.extensions import db
    from app.models.musician import Musician
    before = db.session.query(Musician).count()
    _resolved(tmp_path, "Emmylou Harris and Linda Ronstadt")
    assert db.session.query(Musician).count() == before
