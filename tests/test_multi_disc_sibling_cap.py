"""An artist folder of albums that happens to hold two "..., Disc N" folders is
a grouping, not one multi-disc show (Herbie Hancock, 2026-10-13). One extra
non-disc folder ("Extras") still leaves a multi-disc show intact."""
from app.utils.ingest import resolve_shows


def _album(p, n=2):
    p.mkdir(parents=True)
    for i in range(n):
        (p / f"{i + 1:02d}.mp3").write_bytes(b"x")


def test_artist_folder_with_two_disc_albums_is_a_grouping(tmp_path):
    a = tmp_path / "Herbie Hancock"
    for name in ("Head Hunters", "Thrust", "Sextant",
                 "The Warner Bros. Years 1969-1972, Disc 1",
                 "The Warner Bros. Years 1969-1972, Disc 3"):
        _album(a / name)
    assert len(resolve_shows(str(a))) == 5


def test_discs_plus_one_extra_stay_one_show(tmp_path):
    s = tmp_path / "Show"
    for name in ("CD1", "CD2", "Extras"):
        _album(s / name)
    assert resolve_shows(str(s)) == [str(s)]
