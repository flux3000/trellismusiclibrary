"""
tests/test_file_naming.py — app/utils/file_naming.py, the naming engine.

Presets only (chunk 5). Custom-template grammar, modifiers, groups and
validation get their own cases in a later chunk (spec section 12) — this
file covers the four built-in presets, every fallback the presets exercise,
and the token/context machinery they share with a future custom template.

Fixtures are plain objects (Obj below), not ORM rows: the engine is pure
and takes no app context, so nothing here touches the database.
"""

import pytest

from app.utils.file_naming import (
    rename_plan, render, parse_template, naming_context, flattens,
    TemplateError, PRESETS,
)


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def track(n, title, file_path, disc=None, disc_track=None, set_number=None,
          original_file_path=None):
    return Obj(track_number=n, title=title, file_path=file_path,
              disc_number=disc, disc_track_number=disc_track,
              set_number=set_number, original_file_path=original_file_path)


def recording(tracks, performance, source=None, source_tag=None, etree_shnid=None):
    return Obj(tracks=tracks, performance=performance, source=source,
              source_tag=source_tag, etree_shnid=etree_shnid)


def performance(year=None, month=None, day=None, artist=None, venue=None,
                city=None, state=None, country=None):
    return Obj(start_year=year, start_month=month, start_day=day,
              artist=artist, venue=venue, city=city, state=state, country=country)


def artist(name, abbreviation=None):
    return Obj(name=name, abbreviation=abbreviation)


def venue(name=None, city=None, state=None, country=None):
    return Obj(name=name, city=city, state=state, country=country)


GD = artist("Grateful Dead", abbreviation="gd")


# ── Presets against the 01-ux-report.md section 6 examples ──────────────────

def test_original_preset_flat():
    t = track(1, "X", "01.flac", original_file_path="gd1977-05-08.sbd.miller.t01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    plan = rename_plan(rec, "original")
    assert plan == [(t, "01.flac", "gd1977-05-08.sbd.miller.t01.flac")]


def test_original_preset_has_no_extension_duplication():
    t = track(1, "X", "01.flac", original_file_path="show.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    _, _, proposed = rename_plan(rec, "original")[0]
    assert proposed == "show.flac"


def test_number_title_preset_flat():
    t = track(1, "Scarlet Begonias", "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    _, _, proposed = rename_plan(rec, "number_title")[0]
    assert proposed == "01 - Scarlet Begonias.flac"


def test_etree_preset_flat():
    t = track(1, "X", "01.flac")
    rec = recording([t], performance(1978, 9, 19, GD), source="AUD", source_tag="schoeps")
    _, _, proposed = rename_plan(rec, "etree")[0]
    assert proposed == "gd1978-09-19.aud.schoeps.t01.flac"


def test_etree_preset_multi_disc():
    t = track(1, "X", "01.flac", disc=2, disc_track=1)
    rec = recording([t], performance(1988, 5, 1, GD), source="SBD", etree_shnid=118671)
    _, _, proposed = rename_plan(rec, "etree")[0]
    assert proposed == "gd1988-05-01.sbd.118671.d2t01.flac"


def test_etree_tracks_preset_drops_disc_and_counts_continuously():
    t = track(13, "Eyes", "13.flac", disc=2, disc_track=1)
    rec = recording([t], performance(1977, 5, 8, GD), source="SBD")
    _, _, proposed = rename_plan(rec, "etree_tracks")[0]
    assert proposed == "gd1977-05-08.sbd.t13.flac"


def test_etree_sets_preset():
    t = track(1, "X", "01.flac", set_number="Set 1")
    rec = recording([t], performance(1988, 5, 1, GD), source="SBD", etree_shnid=118671)
    _, _, proposed = rename_plan(rec, "etree_sets")[0]
    assert proposed == "gd1988-05-01.sbd.118671s1t01.flac"


def test_etree_preset_with_nothing_present_drops_every_group():
    """Every bracket group drops out when its field is empty; the literal dot
    before the disc/track segment is outside the disc group and always
    renders (spec chunk-5 fix), so this is artist+date+dot+t, not bare."""
    t = track(1, "X", "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    _, _, proposed = rename_plan(rec, "etree")[0]
    assert proposed == "gd1977-05-08.t01.flac"


def test_number_title_multi_disc_is_continuous_and_flat():
    t1 = track(1, "Scarlet Begonias", "01.flac", disc=1, disc_track=1)
    t2 = track(13, "Eyes", "13.flac", disc=2, disc_track=1)
    rec = recording([t1, t2], performance(1977, 5, 8, GD))
    plan = rename_plan(rec, "number_title")
    proposed = {pt.track_number: name for pt, _old, name in plan}
    assert proposed[1] == "01 - Scarlet Begonias.flac"
    assert proposed[13] == "13 - Eyes.flac"


# ── flattens() ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("scheme, expected", [
    ("original", False),
    ("number_title", True),
    ("etree_tracks", True),
    ("etree", True),
    ("etree_sets", True),
])
def test_flattens_per_preset(scheme, expected):
    assert flattens(scheme) is expected


def test_flattens_for_custom_with_a_position_token():
    assert flattens("custom", "{title} ({track})") is True
    assert flattens("custom", "{disc}-{track_in_disc} {title}") is True
    assert flattens("custom", "{set}-{track_in_set} {title}") is True


def test_flattens_for_custom_without_a_position_token():
    assert flattens("custom", "{artist} - {title}") is False
    assert flattens("custom", "{original}") is False


# ── Fallbacks (spec section 4) ────────────────────────────────────────────────

def test_track_in_disc_falls_back_to_track_when_no_disc():
    t = track(3, "X", "03.flac")   # no disc_number/disc_track_number
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert render("{track_in_disc}", ctx) == "03"


def test_track_in_set_falls_back_to_track_when_no_set():
    t = track(3, "X", "03.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert render("{track_in_set}", ctx) == "03"


def test_empty_title_renders_track_nn():
    t = track(5, "", "05.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert render("{title}", ctx) == "Track 05"


def test_artist_abbr_falls_back_to_initials():
    plain_artist = artist("Old And In The Way")
    t = track(1, "X", "01.flac")
    rec = recording([t], performance(1977, 5, 8, plain_artist))
    ctx = naming_context(rec, rec.performance, plain_artist, None, t, rec.tracks)
    assert render("{artist_abbr}", ctx) == "oaitw"


def test_original_falls_back_to_current_stem_when_no_original_path():
    t = track(1, "X", "current name.flac")   # no original_file_path
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert render("{original}", ctx) == "current name"


def test_track_zero_padded_to_width_of_highest_number():
    tracks = [track(n, "X", f"{n}.flac") for n in (1, 20, 120)]
    rec = recording(tracks, performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, tracks[0], tracks)
    assert render("{track}", ctx) == "001"   # width 3, from "120"


def test_track_padding_minimum_is_two():
    tracks = [track(1, "X", "1.flac")]
    rec = recording(tracks, performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, tracks[0], tracks)
    assert render("{track}", ctx) == "01"


def test_location_drops_missing_parts():
    v = venue(name="Barton Hall", city="Ithaca", state=None, country="US")
    t = track(1, "X", "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, v, t, rec.tracks)
    assert render("{location}", ctx) == "Ithaca, US"


def test_bracket_group_drops_when_a_token_inside_is_empty():
    t = track(1, "X", "01.flac")   # no disc
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert render("a[d{disc}]b", ctx) == "ab"


def test_bracket_group_renders_when_its_token_resolves():
    t = track(1, "X", "01.flac", disc=1)
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert render("a[d{disc}]b", ctx) == "ad1b"


def test_track_in_set_derivation_restarts_per_set_but_track_stays_continuous():
    t1 = track(1, "A", "1.flac", set_number="Set 1")
    t2 = track(2, "B", "2.flac", set_number="Set 1")
    t3 = track(3, "C", "3.flac", set_number="Set 2")
    tracks = [t1, t2, t3]
    rec = recording(tracks, performance(1977, 5, 8, GD))
    ctx1 = naming_context(rec, rec.performance, GD, None, t1, tracks)
    ctx3 = naming_context(rec, rec.performance, GD, None, t3, tracks)
    assert render("{track_in_set}", ctx1) == "01"
    assert render("{track}", ctx1) == "01"
    assert render("{track_in_set}", ctx3) == "01"   # restarts in Set 2
    assert render("{track}", ctx3) == "03"           # stays continuous


def test_set_token_is_none_for_encore_label():
    t = track(1, "X", "01.flac", set_number="Encore")
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    assert ctx["set"] is None
    assert ctx["set_label"] == "Encore"


# ── Sanitization ──────────────────────────────────────────────────────────────

def test_illegal_characters_become_dashes():
    t = track(1, 'Who: What / Why?', "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    _, _, proposed = rename_plan(rec, "number_title")[0]
    assert proposed == "01 - Who- What - Why-.flac"


# ── Collisions within a batch ──────────────────────────────────────────────────

def test_collisions_within_a_batch_get_disambiguated():
    t1 = track(1, "Same Title", "a.flac")
    t2 = track(1, "Same Title", "b.flac")   # same number, same title
    rec = recording([t1, t2], performance(1977, 5, 8, GD))
    plan = rename_plan(rec, "number_title")
    names = [name for _t, _old, name in plan]
    assert names == ["01 - Same Title.flac", "01 - Same Title (2).flac"]


# ── Parse errors ────────────────────────────────────────────────────────────────

def test_parse_template_rejects_unknown_token():
    with pytest.raises(TemplateError):
        parse_template("{nonsense}")


def test_parse_template_rejects_unbalanced_brace():
    with pytest.raises(TemplateError):
        parse_template("{track")


def test_parse_template_rejects_unbalanced_bracket():
    with pytest.raises(TemplateError):
        parse_template("[d{disc}")


def test_parse_template_rejects_path_separator():
    with pytest.raises(TemplateError):
        parse_template("{artist}/{title}")


def test_parse_template_rejects_unknown_modifier():
    with pytest.raises(TemplateError):
        parse_template("{title:sideways}")


def test_render_rejects_a_template_with_empty_output():
    t = track(1, "", "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    ctx = naming_context(rec, rec.performance, GD, None, t, rec.tracks)
    with pytest.raises(TemplateError):
        render("[d{disc}]", ctx)   # disc empty -> group drops -> nothing left


# ── Presets registry ────────────────────────────────────────────────────────────

def test_presets_cover_the_four_keys():
    assert set(PRESETS) == {"original", "number_title", "etree_tracks", "etree", "etree_sets"}


# ── Custom templates (spec sections 4 and 12): grammar, modifiers, groups and
# validation all already live in parse_template()/render() (chunk 5); these
# cases exercise them through a template a person would actually type into
# the Settings field, combining a bracket group with a modifier.

def test_custom_template_with_bracket_group_and_modifier_renders():
    t1 = track(1, "Scarlet Begonias", "01.flac")
    t2 = track(2, "Fire on the Mountain", "02.flac")
    rec = recording([t1, t2], performance(1977, 5, 8, GD),
                    source="AUD", source_tag="schoeps")
    template = "{artist:upper} {date}[ ({source_tag})] - {track} {title}"
    plan = rename_plan(rec, "custom", template)
    proposed = {pt.track_number: name for pt, _old, name in plan}
    assert proposed[1] == "GRATEFUL DEAD 1977-05-08 (schoeps) - 01 Scarlet Begonias.flac"
    assert proposed[2] == "GRATEFUL DEAD 1977-05-08 (schoeps) - 02 Fire on the Mountain.flac"


def test_custom_template_drops_its_bracket_group_when_the_token_is_empty():
    # No source_tag on this recording -> the whole bracketed group vanishes,
    # not just the token, leaving no stray parenthesis or space behind.
    t = track(1, "Scarlet Begonias", "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD), source="AUD")
    template = "{artist:upper} {date}[ ({source_tag})] - {track} {title}"
    plan = rename_plan(rec, "custom", template)
    assert plan[0][2] == "GRATEFUL DEAD 1977-05-08 - 01 Scarlet Begonias.flac"


def test_custom_template_requires_a_template():
    t = track(1, "X", "01.flac")
    rec = recording([t], performance(1977, 5, 8, GD))
    with pytest.raises(TemplateError):
        rename_plan(rec, "custom", None)


# ── Preview endpoint ──────────────────────────────────────────────────────────

def _login_as(client, username="admin"):
    from app.extensions import db as _db, login_manager
    from app.models.user import User
    user = _db.session.query(User).filter_by(username=username).first()
    assert user is not None
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        gen = getattr(login_manager, "_session_identifier_generator", None)
        if callable(gen):
            try:
                sess["_id"] = gen()
            except Exception:
                pass


def test_preview_endpoint_returns_the_plan(app, seeded_ids):
    client = app.test_client()
    _login_as(client)
    resp = client.get(f"/api/naming/preview?scheme=number_title&recording_id={seeded_ids['recording_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["recording_id"] == seeded_ids["recording_id"]
    assert len(data["plan"]) == 2
    assert all("proposed" in row and "current" in row for row in data["plan"])


def test_preview_endpoint_defaults_to_the_most_recent_recording(app, seeded_ids):
    client = app.test_client()
    _login_as(client)
    resp = client.get("/api/naming/preview?scheme=original")
    assert resp.status_code == 200
    assert resp.get_json()["recording_id"] == seeded_ids["recording_id"]


def test_preview_endpoint_returns_400_on_a_bad_template(app, seeded_ids):
    client = app.test_client()
    _login_as(client)
    resp = client.get(f"/api/naming/preview?scheme=custom&template={{nonsense}}"
                       f"&recording_id={seeded_ids['recording_id']}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
