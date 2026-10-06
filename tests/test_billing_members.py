"""
Resolver v2 chunk 7b: a billing of several people ("X and Y") stays one Artist reading.
Splitting a billing into Musicians is postponed (Ryan, 2026-10-05): too many traps
("Peter Rowan and Hot Rize") and no way to measure how often it is right.
"""
import pytest
from types import SimpleNamespace as NS

from app.utils.ingest import build_scan_payload
from app.utils.resolve import resolve

from tests.test_learned_aliases import env  # noqa: F401  (fixture)


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
    assert r.artist.value == line                # the act stays one Artist reading


def test_resolving_creates_no_musician_rows(app, tmp_path):
    from app.extensions import db
    from app.models.musician import Musician
    before = db.session.query(Musician).count()
    _resolved(tmp_path, "Emmylou Harris and Linda Ronstadt")
    assert db.session.query(Musician).count() == before


# ── pre-filling Members from the billing (Ryan, 2026-10-05) ──────────────────

_BILLED = "Doc Watson and Jack Lawrence"


def test_a_duo_billing_carries_both_people_in_the_review_payload(tmp_path):
    r = _resolved(tmp_path, _BILLED)
    assert r.members == ["Doc Watson", "Jack Lawrence"]
    assert r.to_dict()["members"] == ["Doc Watson", "Jack Lawrence"]
    assert r.artist.value == _BILLED


@pytest.mark.parametrize("artist,expected", [
    ("Crosby, Stills, Nash and Young", ["Crosby", "Stills", "Nash", "Young"]),
    ("Emmylou Harris & Linda Ronstadt", ["Emmylou Harris", "Linda Ronstadt"]),
    ("Bela Fleck + Edgar Meyer", ["Bela Fleck", "Edgar Meyer"]),
    ("Bela Fleck and the Flecktones", []),
    ("Sam Bush Band", []),
    ("Tony Rice Unit", []),
    ("The Dillards and Doc Watson", []),
    ("Doc Watson and the Boys Band", []),
    ("Darol Anger's Fiddle Congress and Melee", []),
    ("Tony Rice with Sam Bush, Jerry Douglas", []),
    ("Phish", []),
    ("", []),
])
def test_billing_members_splits_only_person_billings(artist, expected):
    from app.utils.resolve import billing_members
    assert billing_members(artist) == expected


def test_a_with_list_is_never_pre_filled(tmp_path):
    r = _resolved(tmp_path, "Tony Rice\nwith Sam Bush, Jerry Douglas, Bryan Sutton")
    assert r.members == []


def test_bela_fleck_and_the_flecktones_gives_none(tmp_path):
    assert _resolved(tmp_path, "Bela Fleck and the Flecktones").members == []


def test_the_unattended_payload_has_no_members(tmp_path):
    from app.api.ingest import _confirm_payload_from_resolved
    r = _resolved(tmp_path, _BILLED)
    p = _confirm_payload_from_resolved(r, r.scan)
    assert "members" not in p and "guests" not in p


def _save(folder, **over):
    from app.api.ingest import _confirm_payload_from_resolved, _do_confirm
    from app.utils import bulk_ingest_run as bir
    from app.utils.ingest import build_scan_payload
    scan = build_scan_payload(str(folder))
    r = resolve(scan, library_root=str(folder.parent), placement=None)
    payload = _confirm_payload_from_resolved(r, scan)
    payload["resolver_result"] = r.to_dict()
    payload.update(over)
    return r, _do_confirm(payload, bir._owner_user_id())


def _roster(name):
    from app.extensions import db
    from app.models.artist import Artist
    a = db.session.query(Artist).filter_by(name=name).first()
    return a, sorted(m.musician.name for m in a.memberships) if a else []


def test_saving_a_prefilled_review_creates_the_musicians_and_the_roster(env):
    from app.models.musician import Musician
    from app.extensions import db
    from tests.test_learned_aliases import _flac
    d = env.lib / "A1"
    _flac(d / "01.flac", DATE="1999-03-20")
    (d / "info.txt").write_text(_BILLED + "\nTown Hall, New York, NY\nMarch 20, 1999\n\nSource: SBD\n\n01. Song One\n")
    r = resolve(__import__("app.utils.ingest", fromlist=["x"]).build_scan_payload(str(d)),
                library_root=str(env.lib), placement=None)
    _save(d, members=list(r.members), guests=[])
    a, names = _roster(_BILLED)
    assert names == ["Doc Watson", "Jack Lawrence"]
    assert db.session.query(Musician).filter_by(name="Jack Lawrence").count() == 1


def test_an_existing_acts_roster_is_never_changed_by_a_save(env):
    from app.models.artist import Artist
    from app.extensions import db
    from app.utils.artists import set_artist_members
    from tests.test_learned_aliases import _flac
    a = Artist(name=_BILLED)
    db.session.add(a)
    db.session.flush()
    set_artist_members(a, ["Ron Block"])
    db.session.commit()
    d = env.lib / "A1"
    _flac(d / "01.flac", DATE="1999-03-20")
    (d / "info.txt").write_text(_BILLED + "\nTown Hall, New York, NY\nMarch 20, 1999\n\nSource: SBD\n\n01. Song One\n")
    _save(d, members=["Doc Watson", "Jack Lawrence"], guests=[])
    assert _roster(_BILLED)[1] == ["Ron Block"]


def test_an_unattended_import_writes_no_musicians(env):
    from app.api.ingest import auto_confirm
    from app.models.musician import Musician
    from app.extensions import db
    from app.utils import bulk_ingest_run as bir
    from tests.test_learned_aliases import _flac
    d = env.lib / "A1"
    _flac(d / "01.flac", DATE="1999-03-20", ARTIST=_BILLED)
    (d / "info.txt").write_text(_BILLED + "\nTown Hall, New York, NY\nMarch 20, 1999\n\nSource: SBD\n\n01. Song One\n")
    before = db.session.query(Musician).count()
    out = auto_confirm(str(d), bir._owner_user_id())
    assert out["status"] == "ingested"
    assert db.session.query(Musician).count() == before


# ── band pairs are not people ────────────────────────────────────────────────

def _lib(**kw):
    from app.utils.reader.library import LibraryIndex
    return LibraryIndex.from_dicts(**kw)


def test_two_known_bands_are_not_split():
    from app.utils.resolve import billing_members
    lib = _lib(artists=["Los Lobos", "Los Lonely Boys", "Blues Traveler", "Phish"])
    assert billing_members("Los Lobos and Los Lonely Boys", lib, None) == []
    assert billing_members("Blues Traveler and Phish", lib, None) == []
    assert billing_members("Blues Traveler and Phish", _lib(), None) == ["Blues Traveler", "Phish"]  # unknown to the library


def test_one_known_band_is_enough_to_refuse():
    from app.utils.resolve import billing_members
    assert billing_members("Doc Watson and Phish", _lib(artists=["Phish"]), None) == []


def test_a_learned_alias_of_a_band_counts():
    from app.utils.resolve import billing_members
    lib = _lib(artists=["Blues Traveler"], artist_aliases=[("Blues Traveler", "BT")])
    assert billing_members("Doc Watson and BT", lib, None) == []


def test_a_solo_act_that_is_also_a_musician_is_a_person():
    from app.utils.resolve import billing_members
    lib = _lib(artists=[{"name": "Doc Watson", "members": []}, "Jack Lawrence"], musicians=["Doc Watson", "Jack Lawrence"])
    assert billing_members("Doc Watson and Jack Lawrence", lib, None) == ["Doc Watson", "Jack Lawrence"]


class _AtlasStub:
    def __init__(self, kinds):
        self.kinds = kinds

    def artist(self, text, **kw):
        k = self.kinds.get(text)
        return [NS(extra={"act_kind": k})] if text in self.kinds else []


def test_an_atlas_group_is_not_a_person_but_an_atlas_person_is():
    from app.utils.resolve import billing_members
    atlas = _AtlasStub({"Los Lobos": "Group", "Doc Watson": "Person"})
    assert billing_members("Doc Watson and Los Lobos", _lib(), atlas) == []
    assert billing_members("Doc Watson and Jack Lawrence", _lib(), atlas) == ["Doc Watson", "Jack Lawrence"]
