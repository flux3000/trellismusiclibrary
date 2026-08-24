"""
tests/test_search.py — global search (IO-46 V1).

Two halves, deliberately:

  * The engine (app/utils/search.py) is pure, so it is tested with no app
    context at all. That matters here beyond tidiness — conftest holds ONE
    app context per test and Flask-Login caches identity on `g`, which is how
    a role-gate test in this repo passed for weeks without reaching its gate.
    Logic that never touches a request context cannot be fooled that way.

  * The API half asserts EXACT status codes and EXACT payload shapes.
    "Loose assertions hide broken tests" is a recorded trap in this project
    (CONTEXT, "Traps"): the same test that cached identity also accepted
    either 403 or 302, and the two covered for each other.

The two regression cases the whole feature lives on, per the design handoff,
are the apostrophe fold and multi-term AND. Both are pinned below by name.
"""

import pytest

from app.extensions import db as _db
from app.models.artist import Artist, Membership
from app.models.performance import Performance
from app.models.performer import Performer
from app.models.quality import RecordingQuality
from app.models.recording import Recording
from app.models.user import User
from app.models.venue import Venue
from app.utils import search as se


# ══════════════════════════════════════════════════════════════════════════
# Engine — normalisation
# ══════════════════════════════════════════════════════════════════════════

def test_norm_deletes_straight_apostrophe():
    """THE bug the design benchmark caught: an apostrophe mapped to a space
    turns "Don't" into "don t", and nobody types the space."""
    assert se.norm("Don't Give Your Heart") == "dont give your heart"


def test_norm_deletes_curly_apostrophe_too():
    """The corpus contains U+2019 (e.g. "Bear's Ampex"). Handling only the
    straight one reintroduces the bug for a subset of rows."""
    assert se.norm("Bear’s Ampex") == "bears ampex"


def test_norm_replaces_other_punctuation_with_space():
    assert se.norm("Rock-n-Roll") == "rock n roll"
    assert se.norm("Crosby, Stills & Nash") == "crosby stills nash"


def test_norm_folds_diacritics():
    """A collector typing ASCII must reach Esbjörn Svensson — 51 recordings,
    the densest act in the library."""
    assert se.norm("Esbjörn Svensson") == "esbjorn svensson"


def test_norm_is_unicode_form_agnostic():
    """Composed and decomposed spellings must produce the same key — the
    filename-vs-database mismatch that broke a folder-to-grade join once."""
    assert se.norm("Lucía") == se.norm("Lucía")


def test_norm_of_empty_and_none_is_empty_string():
    assert se.norm(None) == ""
    assert se.norm("") == ""
    assert se.norm("   ") == ""


def test_keys_dedupes_and_drops_empties():
    assert se.keys("Bill Evans", "bill evans", None, "") == ["bill evans"]


# ══════════════════════════════════════════════════════════════════════════
# Engine — date parsing
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("token,expected", [
    ("1983",       (1983, None, None)),
    ("1983-04",    (1983, 4, None)),
    ("1983-04-12", (1983, 4, 12)),
    ("1983-4-12",  (1983, 4, 12)),
    ("4/12/1983",  (1983, 4, 12)),
])
def test_recognised_date_forms(token, expected):
    assert se.parse_date_token(token, today_year=2026) == expected


def test_two_digit_year_expands_inside_a_slashed_date():
    assert se.parse_date_token("4/12/83", today_year=2026) == (1983, 4, 12)
    assert se.parse_date_token("4/12/12", today_year=2026) == (2012, 4, 12)


def test_two_digit_year_pivot_follows_the_current_year():
    """The pivot is derived, not hardcoded, so it stays correct over time."""
    assert se.parse_date_token("1/1/30", today_year=2026) == (1930, 1, 1)
    assert se.parse_date_token("1/1/30", today_year=2035) == (2030, 1, 1)


@pytest.mark.parametrize("token", ["83", "26", "4/12", "9:30", "1899", "abc", ""])
def test_ambiguous_or_non_dates_are_not_dates(token):
    """Ryan, 2026-08-18: shows span 1940–2026, so a bare two-digit token is
    genuinely ambiguous and must fall through to text rather than be guessed."""
    assert se.parse_date_token(token, today_year=2026) is None


def test_bare_two_digit_token_becomes_a_text_term():
    q = se.parse_query("83", today_year=2026)
    assert q.text_terms == ["83"]
    assert q.date_terms == []


def test_impossible_month_or_day_is_rejected():
    assert se.parse_date_token("1983-13-01", today_year=2026) is None
    assert se.parse_date_token("1983-04-32", today_year=2026) is None


def test_date_matches_only_compares_components_that_were_typed():
    assert se.date_matches((1983, None, None), 1983, 7, 4) is True
    assert se.date_matches((1983, 7, None),    1983, 7, 4) is True
    assert se.date_matches((1983, 7, 4),       1983, 7, 4) is True
    assert se.date_matches((1983, 7, 5),       1983, 7, 4) is False
    assert se.date_matches((1984, None, None), 1983, 7, 4) is False


def test_a_show_with_no_year_matches_no_date_term():
    assert se.date_matches((1983, None, None), None, None, None) is False


# ══════════════════════════════════════════════════════════════════════════
# Engine — query parsing and scoring
# ══════════════════════════════════════════════════════════════════════════

def test_parse_query_splits_text_and_dates():
    q = se.parse_query("hot rize 1983", today_year=2026)
    assert q.text_terms == ["hot", "rize"]
    assert q.date_terms == [(1983, None, None)]


def test_parse_query_dedupes_repeated_terms():
    q = se.parse_query("evans evans 1980 1980", today_year=2026)
    assert q.text_terms == ["evans"]
    assert q.date_terms == [(1980, None, None)]


def test_empty_query_is_falsy():
    assert not se.parse_query("")
    assert not se.parse_query("   ")


def test_match_strength_is_graded_exact_prefix_wordstart_infix():
    assert se.score_text_term("evans", ["evans"])        == se.EXACT
    assert se.score_text_term("evans", ["evanston hall"]) == se.PREFIX
    assert se.score_text_term("evans", ["bill evans"])   == se.WORD_START
    assert se.score_text_term("vans",  ["bill evans"])   == se.INFIX
    assert se.score_text_term("zzz",   ["bill evans"])   == 0


def test_score_row_requires_every_term_to_match():
    ks = ["hot rize"]
    assert se.score_row(se.parse_query("hot rize"), ks) is not None
    assert se.score_row(se.parse_query("hot rize banjo"), ks) is None


def test_score_row_rejects_a_dateless_row_when_a_date_was_typed():
    assert se.score_row(se.parse_query("hot 1983"), ["hot rize"], ymd=None) is None


# ══════════════════════════════════════════════════════════════════════════
# Engine — run_search over a hand-built index
# ══════════════════════════════════════════════════════════════════════════

def _index():
    """Small fixture corpus, shaped like the real one."""
    performers = [
        {"id": 1, "name": "Hot Rize",   "sort_name": None},
        {"id": 2, "name": "Bill Evans", "sort_name": "Evans, Bill"},
    ]
    artists = [
        {"id": 10, "name": "Tim O'Brien", "sort_name": None, "performer_ids": [1]},
        {"id": 11, "name": "Bill Evans",  "sort_name": None, "performer_ids": [2]},
    ]
    venues = [
        {"id": 20, "name": "Lulu White's", "city": "Boston",   "state": "MA", "country": "US"},
        {"id": 21, "name": "Telluride",    "city": "Telluride", "state": "CO", "country": "US"},
    ]
    recordings = [
        {"id": 100, "performance_id": 200, "performer_id": 1,
         "performer_name": "Hot Rize", "performer_sort_name": None,
         "artist_names": ["Tim O'Brien"], "venue_id": 21, "venue_name": "Telluride",
         "city": "Telluride", "state": "CO", "country": "US",
         "year": 1983, "month": 6, "day": 25, "source": "SBD", "listening_quality": 70.0},
        {"id": 101, "performance_id": 201, "performer_id": 1,
         "performer_name": "Hot Rize", "performer_sort_name": None,
         "artist_names": ["Tim O'Brien"], "venue_id": 21, "venue_name": "Telluride",
         "city": "Telluride", "state": "CO", "country": "US",
         "year": 1983, "month": 6, "day": 26, "source": "AUD", "listening_quality": 90.0},
        {"id": 102, "performance_id": 202, "performer_id": 2,
         "performer_name": "Bill Evans", "performer_sort_name": "Evans, Bill",
         "artist_names": ["Bill Evans"], "venue_id": 20, "venue_name": "Lulu White's",
         "city": "Boston", "state": "MA", "country": "US",
         "year": 1979, "month": 10, "day": 30, "source": "FM", "listening_quality": 84.0},
    ]
    return se.build_index(performers, artists, venues, recordings)


def _ids(result, group):
    return [e["row"]["id"] for e in result["groups"][group]["items"]]


def test_multi_term_and_narrows_rather_than_breaks():
    """The headline behaviour: "hot rize 1983" must satisfy BOTH."""
    r = se.run_search(_index(), "hot rize 1983", today_year=2026)
    assert sorted(_ids(r, "recordings")) == [100, 101]
    assert _ids(r, "performers") == [1]


def test_adding_a_term_can_only_shrink_the_result():
    idx = _index()
    wide = se.run_search(idx, "hot", today_year=2026)["groups"]["recordings"]["total"]
    narrow = se.run_search(idx, "hot rize telluride 1983", today_year=2026)["groups"]["recordings"]["total"]
    assert narrow <= wide


def test_apostrophe_venue_is_reachable_without_typing_the_apostrophe():
    """The second regression case: "lulu whites" must find "Lulu White's"."""
    r = se.run_search(_index(), "lulu whites", today_year=2026)
    assert _ids(r, "venues") == [20]
    assert _ids(r, "recordings") == [102]


def test_artist_reaches_shows_through_membership():
    """Tim O'Brien is a person; his shows are Hot Rize's, via membership."""
    r = se.run_search(_index(), "obrien", today_year=2026)
    assert _ids(r, "artists") == [10]
    assert sorted(_ids(r, "recordings")) == [100, 101]


def test_geography_is_searchable_through_the_venue():
    r = se.run_search(_index(), "boston", today_year=2026)
    assert _ids(r, "venues") == [20]
    assert _ids(r, "recordings") == [102]


def test_date_only_query_returns_shows_and_no_entity_groups():
    """Matching every act in the library against no text is not a result."""
    r = se.run_search(_index(), "1983", today_year=2026)
    assert sorted(_ids(r, "recordings")) == [100, 101]
    assert r["groups"]["performers"]["total"] == 0
    assert r["groups"]["venues"]["total"] == 0
    assert r["groups"]["artists"]["total"] == 0


def test_entity_groups_survive_a_date_term_in_the_query():
    """"hot rize 1983" should still offer the act itself, not just shows."""
    r = se.run_search(_index(), "hot rize 1983", today_year=2026)
    assert _ids(r, "performers") == [1]


def test_full_date_narrows_to_the_single_night():
    r = se.run_search(_index(), "hot rize 1983-06-26", today_year=2026)
    assert _ids(r, "recordings") == [101]


def test_listening_quality_breaks_ties_between_equal_matches():
    """Both Hot Rize shows match "hot rize 1983" identically; the 90 sorts
    above the 70. Ordering equally relevant hits is not hiding anything."""
    r = se.run_search(_index(), "hot rize 1983", today_year=2026)
    assert _ids(r, "recordings") == [101, 100]


def test_stronger_match_outranks_better_sound():
    """Quality is a TIEBREAK, never a substitute for relevance."""
    idx = _index()
    r = se.run_search(idx, "telluride", today_year=2026)
    scores = [e["score"] for e in r["groups"]["recordings"]["items"]]
    assert scores == sorted(scores, reverse=True)


def test_empty_query_returns_every_group_empty():
    r = se.run_search(_index(), "", today_year=2026)
    assert all(g["total"] == 0 for g in r["groups"].values())


def test_unmatched_query_returns_zero_not_everything():
    r = se.run_search(_index(), "phish", today_year=2026)
    assert all(g["total"] == 0 for g in r["groups"].values())


def test_group_order_is_fixed():
    """A dropdown whose groups reshuffle between keystrokes cannot be aimed at."""
    assert se.GROUP_ORDER == ("performers", "recordings", "venues", "artists")


# ══════════════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════════════

def _login_as(client, username="admin"):
    """Log in and PROVE it — a broken harness must fail here, saying so,
    rather than impersonating an authorization result downstream."""
    from app.extensions import login_manager

    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None, f"no such user to log in as: {username!r}"
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass
    me = client.get("/api/auth/me")
    assert me.status_code == 200, (
        f"session login as {username!r} did not authenticate: /api/auth/me "
        f"returned {me.status_code}. The harness is broken, not the endpoint.")
    return user


@pytest.fixture()
def client(app):
    c = app.test_client()
    _login_as(c)
    return c


def test_search_requires_login(app):
    r = app.test_client().get("/api/search?q=evans")
    assert r.status_code == 401
    assert r.is_json
    assert "error" in r.get_json()


def test_both_slash_forms_answer_200(client):
    """The bare route exists so the omnibox does not eat a 308 per keystroke."""
    assert client.get("/api/search?q=evans").status_code == 200
    assert client.get("/api/search/?q=evans").status_code == 200


def test_empty_query_is_200_with_no_groups_not_400(client):
    """The omnibox fires on the keystroke that CLEARS the box too; an error
    status for that would paint a red state on a normal interaction."""
    r = client.get("/api/search?q=")
    assert r.status_code == 200
    body = r.get_json()
    assert body["groups"] == []
    assert body["total"] == 0


def test_unknown_type_is_400(client):
    r = client.get("/api/search?q=evans&type=songs")
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_seeded_act_is_found_with_exact_shape(client, seeded_ids):
    r = client.get("/api/search?q=evans")
    assert r.status_code == 200
    body = r.get_json()
    groups = {g["type"]: g for g in body["groups"]}

    assert groups["performers"]["label"] == "Performers"
    assert groups["performers"]["total"] == 1
    item = groups["performers"]["items"][0]
    assert item == {
        "type": "performer",
        "id": seeded_ids["performer_id"],
        "name": "Bill Evans",
        "recording_count": 1,
        "hash": f"#/performer/{seeded_ids['performer_id']}",
    }


def test_person_and_act_route_to_different_pages(client, seeded_ids):
    """Artist (person) and Performer (act) share a name in the seed and sit on
    adjacent routes — wiring one to the other's page is the obvious bug."""
    groups = {g["type"]: g for g in client.get("/api/search?q=evans").get_json()["groups"]}
    assert groups["performers"]["items"][0]["hash"] == f"#/performer/{seeded_ids['performer_id']}"
    assert groups["artists"]["items"][0]["hash"] == f"#/person/{seeded_ids['artist_id']}"


def test_recording_item_shape(client, seeded_ids):
    groups = {g["type"]: g for g in client.get("/api/search?q=evans").get_json()["groups"]}
    item = groups["recordings"]["items"][0]
    assert item == {
        "type": "recording",
        "id": seeded_ids["recording_id"],
        "performer": "Bill Evans",
        "performer_id": seeded_ids["performer_id"],
        "date": "1980-02-22",
        "venue": "Sprague Memorial Hall",
        "city": "New Haven",
        "state": "CT",
        "source": "AUD",
        "quality": "B+",
        "listening_quality": None,
        "hash": f"#/recording/{seeded_ids['recording_id']}",
    }


def test_venue_city_is_searchable(client):
    groups = {g["type"]: g for g in client.get("/api/search?q=new+haven").get_json()["groups"]}
    assert groups["venues"]["items"][0]["name"] == "Sprague Memorial Hall"
    assert groups["recordings"]["total"] == 1


def test_multi_term_and_across_dimensions(client):
    """Act plus year — the show is 1980, so 1980 finds it and 1979 does not."""
    hit = client.get("/api/search?q=bill+evans+1980").get_json()
    assert {g["type"]: g["total"] for g in hit["groups"]}["recordings"] == 1

    miss = client.get("/api/search?q=bill+evans+1979").get_json()
    assert "recordings" not in {g["type"] for g in miss["groups"]}


def test_empty_groups_are_omitted_entirely(client):
    """Every browse module hides when empty — the rule that lets a fixed
    module set survive a sparse library (CONTEXT)."""
    body = client.get("/api/search?q=1980").get_json()
    assert {g["type"] for g in body["groups"]} == {"recordings"}


def test_typed_query_pages_one_group(client):
    r = client.get("/api/search?q=evans&type=recordings&limit=1&offset=0")
    assert r.status_code == 200
    body = r.get_json()
    assert body["type"] == "recordings"
    assert body["label"] == "Recordings"
    assert body["total"] == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["items"]) == 1


def test_offset_past_the_end_is_an_empty_page_not_an_error(client):
    r = client.get("/api/search?q=evans&type=recordings&limit=10&offset=500")
    assert r.status_code == 200
    body = r.get_json()
    assert body["items"] == []
    assert body["total"] == 1        # total is the whole result, not the page


def test_garbage_limit_falls_back_rather_than_500ing(client):
    r = client.get("/api/search?q=evans&type=recordings&limit=banana")
    assert r.status_code == 200
    assert r.get_json()["limit"] == 25


def test_limit_is_capped(client):
    r = client.get("/api/search?q=evans&type=recordings&limit=99999")
    assert r.get_json()["limit"] == 100


def test_date_terms_are_reported_back_for_the_ui(client):
    body = client.get("/api/search?q=bill+1980").get_json()
    assert body["text_terms"] == ["bill"]
    assert body["date_terms"] == [[1980, None, None]]


def test_track_titles_are_not_searchable(client):
    """THE RULE. The seed has a track called "My Foolish Heart"; searching it
    must find nothing. If this test ever fails, someone widened the scope
    without the conversation — IO-46's Jira text still asks for it."""
    body = client.get("/api/search?q=foolish").get_json()
    assert body["groups"] == []
    assert body["total"] == 0


def test_provenance_text_is_not_searchable(app, client, seeded_ids):
    """The other half of THE RULE: lineage and info-file blobs are out."""
    rec = _db.session.get(Recording, seeded_ids["recording_id"])
    rec.lineage = "Schoeps CMC6 > Nakamichi > DAT"
    rec.info_file_content = "Transferred by Charlie Miller"
    _db.session.commit()

    for q in ("schoeps", "nakamichi", "charlie+miller"):
        body = client.get(f"/api/search?q={q}").get_json()
        assert body["total"] == 0, f"{q!r} leaked provenance text into search"


def test_listening_quality_orders_equal_matches(app, client):
    """End-to-end tiebreak: two recordings of the same show, different scores."""
    perf = _db.session.query(Performance).first()
    extra = Recording(performance_id=perf.id, source="SBD", is_complete=True,
                      folder_path="Bill Evans/1980-sbd")
    _db.session.add(extra)
    _db.session.flush()
    first = _db.session.query(Recording).filter(Recording.id != extra.id).first()
    _db.session.add(RecordingQuality(recording_id=extra.id, listening_quality=92.0))
    _db.session.add(RecordingQuality(recording_id=first.id, listening_quality=61.0))
    _db.session.commit()

    groups = {g["type"]: g for g in client.get("/api/search?q=evans").get_json()["groups"]}
    ids = [i["id"] for i in groups["recordings"]["items"]]
    assert ids == [extra.id, first.id]


def test_unanalysed_recording_sorts_last_without_crashing(app, client):
    """listening_quality is nullable; a None must not break the comparison."""
    perf = _db.session.query(Performance).first()
    unscored = Recording(performance_id=perf.id, source="AUD", is_complete=True,
                         folder_path="Bill Evans/1980-unscored")
    _db.session.add(unscored)
    _db.session.flush()
    scored = _db.session.query(Recording).filter(Recording.id != unscored.id).first()
    _db.session.add(RecordingQuality(recording_id=scored.id, listening_quality=50.0))
    _db.session.commit()

    groups = {g["type"]: g for g in client.get("/api/search?q=evans").get_json()["groups"]}
    ids = [i["id"] for i in groups["recordings"]["items"]]
    assert ids[-1] == unscored.id


def test_a_show_with_no_venue_is_still_found_by_act(app, client):
    """10 of 552 shows have no venue. They lose the geography dimension —
    accepted — but must not vanish from search altogether."""
    performer = Performer(name="Ornette Coleman")
    _db.session.add(performer)
    _db.session.flush()
    perf = Performance(performer_id=performer.id, venue_id=None,
                       start_year=1972, start_month=3, start_day=1)
    _db.session.add(perf)
    _db.session.flush()
    _db.session.add(Recording(performance_id=perf.id, source="AUD",
                              folder_path="Ornette/1972"))
    _db.session.commit()

    groups = {g["type"]: g for g in client.get("/api/search?q=ornette").get_json()["groups"]}
    assert groups["recordings"]["total"] == 1
    assert groups["recordings"]["items"][0]["venue"] is None


def test_diacritic_free_typing_finds_an_accented_act(app, client):
    performer = Performer(name="Esbjörn Svensson Trio")
    _db.session.add(performer)
    _db.session.flush()
    person = Artist(name="Esbjörn Svensson")
    _db.session.add(person)
    _db.session.flush()
    _db.session.add(Membership(performer_id=performer.id, artist_id=person.id, order=0))
    _db.session.commit()

    groups = {g["type"]: g for g in client.get("/api/search?q=esbjorn").get_json()["groups"]}
    assert groups["performers"]["items"][0]["name"] == "Esbjörn Svensson Trio"
    assert groups["artists"]["items"][0]["name"] == "Esbjörn Svensson"


def test_venue_recording_count_is_derived_correctly(app, client):
    venue = _db.session.query(Venue).first()
    groups = {g["type"]: g for g in client.get("/api/search?q=sprague").get_json()["groups"]}
    item = groups["venues"]["items"][0]
    assert item["id"] == venue.id
    assert item["recording_count"] == 1
