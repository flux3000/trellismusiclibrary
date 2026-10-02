"""The one canonical title-case function (app.utils.ingest.title_case)."""
import pytest

from app.utils.ingest import title_case


@pytest.mark.parametrize("raw,expected", [
    ("mcfadden", "McFadden"),
    ("mccoury", "McCoury"),
    ("Del McCoury Band", "Del McCoury Band"),
    ("o'donovan", "O'Donovan"),
    ("d’angelo", "D’Angelo"),
    ("don't stop", "Don't Stop"),
    ("rock'n'roll", "Rock'n'roll"),
    ("i'm free", "I'm Free"),
    ("j.d. crowe", "J.D. Crowe"),
    ("J.d. Crowe", "J.D. Crowe"),
    ("J.D. CROWE", "J.D. Crowe"),
    ("(bill", "(Bill"),
    ('"song', '"Song'),
    ("béla fleck", "Béla Fleck"),
    ("GRATEFUL DEAD", "Grateful Dead"),
    ("JGB at the Warfield", "JGB at the Warfield"),
    ("Pat Metheny Group", "Pat Metheny Group"),
    ("the band", "The Band"),
    ("Mack Macon", "Mack Macon"),
    ("mack macon", "Mack Macon"),
    ("jean-luc ponty", "Jean-Luc Ponty"),
    ("song of the (the remix)", "Song of the (The Remix)"),
    ("", ""),
])
def test_title_case(raw, expected):
    assert title_case(raw) == expected


@pytest.mark.parametrize("v", ["J.D. Crowe", "McFadden", "O'Donovan", "Jean-Luc Ponty"])
def test_idempotent(v):
    assert title_case(v) == v
