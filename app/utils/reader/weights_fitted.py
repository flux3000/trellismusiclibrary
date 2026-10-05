"""
Weights for the role decoder (Resolver v2, chunk 7b, 2026-10-05): FITTED.

Every number the decoder uses is in this file, as readable rows:

    EMISSION    (feature, role, weight)   how much a feature favours a role for one segment
    TRANSITIONS (previous role, role): weight   how likely a role is to follow another

A segment's score for a role is the sum of weight * value over its features, plus the
role's BIAS. The decoder then picks the best whole-file labelling (Viterbi), so a name that
follows "with" is a MEMBER because the transition CONNECTOR -> MEMBER is strong, not because
a special rule says so. Units are log-score points: +3 is "clearly", +6 "decisively".

The rows are the chunk 3 hand-set table (weights_handset.py) with the ARTIST emission rows
moved by an averaged perceptron fitted on the evaluation corpora G1 and G3 (reader/fit.py).
A weight moves little when few rows touched it and never more than 3 points from its
hand-set value. Every other row is still hand-set: fitting them as well made the venue field
worse on held-out acts (see the module docstring of fit.py). Refit after any change to the
features, then recalibrate: python3 -m app.utils.resolver_eval --fit-weights --write
"""

ROLES = ("TITLE", "ARTIST", "CONNECTOR", "MEMBER", "VENUE", "EVENT", "STAGE", "PLACE",
         "DATE", "SOURCE", "LINEAGE", "EQUIPMENT", "SET_HEADER", "TRACK", "NOTES",
         "BOILERPLATE", "OTHER")

# What a role scores with no feature at all.
BIAS = {
    "TITLE": -1.0, "ARTIST": 0.0, "CONNECTOR": -2.0, "MEMBER": -1.0, "VENUE": -1.0,
    "EVENT": -3.0, "STAGE": -3.0, "PLACE": -2.0, "DATE": -3.0, "SOURCE": -2.0,
    "LINEAGE": -2.0, "EQUIPMENT": -2.0, "SET_HEADER": -3.0, "TRACK": -2.0,
    "NOTES": 0.0, "BOILERPLATE": -3.0, "OTHER": -0.5,
}

# Which roles a kind of unit may take at all (a numbered track line is never a venue).
ALLOWED = {
    "seg":       set(ROLES) - {"DATE", "SET_HEADER", "BOILERPLATE"},
    "date":      {"DATE", "NOTES", "BOILERPLATE"},
    "track":     {"TRACK", "NOTES", "OTHER"},
    "titleline": {"TRACK", "NOTES", "OTHER", "ARTIST", "TITLE", "VENUE", "MEMBER", "LINEAGE"},
    "boiler":    {"BOILERPLATE"},
    "sethdr":    {"SET_HEADER", "NOTES"},
}

EMISSION = [
    # ── ARTIST ────────────────────────────────────────────────────────────
    ("line0",             "ARTIST", 2.97),
    ("line1",             "ARTIST", -0.89),
    ("line2_3",           "ARTIST", -2.57),
    ("line4_8",           "ARTIST", -3.38),
    ("line_late",         "ARTIST", -2.93),
    ("block_later",       "ARTIST", 1.26),
    ("first_in_line",     "ARTIST", -0.6),
    ("lib_artist_exact",  "ARTIST", 2.96),
    ("lib_artist_core",   "ARTIST", 2.01),
    ("lib_musician",      "ARTIST", 1.89),
    ("agree_hint_artist", "ARTIST",  4.0),
    ("label_artist",      "ARTIST", 4.98),
    ("next_is_connector", "ARTIST", -0.79),
    ("title_case",        "ARTIST", 1.59),
    ("all_caps",          "ARTIST", -0.3),
    ("w5_7",              "ARTIST", -0.87),
    ("w8p",               "ARTIST", -3.0),
    ("venue_word",        "ARTIST", -1.38),
    ("event_word",        "ARTIST", -4.04),
    ("stage_word",        "ARTIST", -4.0),
    ("title_word",        "ARTIST", -1.52),
    ("quoted",            "ARTIST", -4.02),
    ("has_digit",         "ARTIST", -1.76),
    ("has_clock",         "ARTIST", -3.0),
    ("has_gt",            "ARTIST", -4.0),
    ("source_kw",         "ARTIST", -2.53),
    ("recording_verb",    "ARTIST", -3.0),
    ("equip_word",        "ARTIST", -3.01),
    ("notes_word",        "ARTIST", -1.96),
    ("in_place",          "ARTIST", -5.59),
    ("lib_venue",         "ARTIST", -3.0),
    ("lib_event",         "ARTIST", -3.0),
    ("has_instr",         "ARTIST", -2.0),
    ("label_other",       "ARTIST", -5.0),
    ("after_track_start", "ARTIST", -6.0),
    ("prev_is_connector", "ARTIST", -4.0),
    ("sec_personnel",     "ARTIST",  0.0),
    ("sec_lineage",       "ARTIST", -3.0),

    ("hard_after_first",  "ARTIST", -3.77),
    ("soft_line0",        "VENUE",  -4.0),
    ("soft_after_first",  "ARTIST", 0.87),     # "A, B, C" billed on one line: cancels ARTIST -> ARTIST
    ("after_at",          "ARTIST", -3.5),
    ("paren_tail",        "ARTIST", -4.72),

    # ── ATLAS (shipped reference data): evidence weaker than the library's ─
    # Every weight here is below the smallest lib_* weight for its role
    # (tests/test_atlas_features.py keeps that true), and reader/features.py gives
    # the Atlas no say wherever the library has already spoken.
    ("atl_artist",        "ARTIST", 0.12),
    ("atl_artist_fz",     "ARTIST", -0.63),
    ("atl_musician",      "ARTIST",  0.5),
    ("atl_musician",      "MEMBER",  1.5),
    ("atl_venue",         "VENUE",   2.0),
    ("atl_venue_fz",      "VENUE",   1.0),
    ("atl_show_place",    "VENUE",   3.8),
    ("atl_show_event",    "EVENT",   3.8),
    ("atl_show_event",    "VENUE",  -1.5),
    ("atl_event",         "EVENT",   2.5),
    ("atl_event_fz",      "EVENT",   1.2),
    ("atl_artist",        "VENUE",  -1.5),
    ("atl_artist",        "TITLE",  -1.0),
    ("atl_venue",         "ARTIST", -1.59),
    ("atl_event",         "ARTIST", -1.67),
    ("atl_event",         "VENUE",  -1.5),

    # ── TITLE (tour or show banner) ───────────────────────────────────────
    ("line0",             "TITLE",   0.5),
    ("quoted",            "TITLE",   3.5),
    ("title_word",        "TITLE",   3.0),
    ("all_lower",         "TITLE",   1.5),
    ("starts_lower",      "TITLE",   1.0),
    ("w5_7",              "TITLE",   0.5),
    ("w8p",               "TITLE",  -1.0),
    ("in_place",          "TITLE",  -3.0),
    ("venue_word",        "TITLE",  -1.5),
    ("lib_artist_exact",  "TITLE",  -3.0),
    ("after_track_start", "TITLE",  -4.0),
    ("line_late",         "TITLE",  -2.0),
    ("has_clock",         "TITLE",  -3.0),
    ("has_gt",            "TITLE",  -4.0),
    ("source_kw",         "TITLE",  -2.0),
    ("recording_verb",    "TITLE",  -3.0),

    # ── CONNECTOR ─────────────────────────────────────────────────────────
    ("connector_word",    "CONNECTOR", 6.0),

    # ── MEMBER ────────────────────────────────────────────────────────────
    ("person_name",       "MEMBER",  1.5),
    ("has_instr",         "MEMBER",  4.0),
    ("instrument_word",   "MEMBER",  5.0),
    ("lib_musician",      "MEMBER",  3.0),
    ("prev_is_connector", "MEMBER",  3.0),
    ("sec_personnel",     "MEMBER",  4.0),
    ("label_personnel",   "MEMBER",  4.0),
    ("line0",             "MEMBER", -3.0),
    ("line1",             "MEMBER", -0.5),
    ("w1",                "MEMBER", -1.5),
    ("w8p",               "MEMBER", -3.0),
    ("in_place",          "MEMBER", -4.0),
    ("venue_word",        "MEMBER", -4.0),
    ("event_word",        "MEMBER", -4.0),
    ("has_digit",         "MEMBER", -1.5),
    ("has_gt",            "MEMBER", -4.0),
    ("lib_venue",         "MEMBER", -3.0),
    ("source_kw",         "MEMBER", -3.0),
    ("after_track_start", "MEMBER", -3.0),

    # ── VENUE ─────────────────────────────────────────────────────────────
    ("venue_word",        "VENUE",   3.5),
    ("lib_venue",         "VENUE",   5.0),
    ("lib_venue_in",      "VENUE",   2.5),
    ("peel_left",         "VENUE",   3.5),
    ("peel_left_city",    "VENUE",   1.5),
    ("peel_at",           "VENUE",   3.0),
    ("next_line_place",   "VENUE",   3.0),
    ("prev_line_date",    "VENUE",   0.7),
    ("label_venue",       "VENUE",   5.0),
    ("label_location",    "VENUE",   0.5),
    ("title_case",        "VENUE",   0.5),
    ("all_caps",          "VENUE",   0.3),
    ("w2_4",              "VENUE",   0.5),
    ("w5_7",              "VENUE",  -0.5),
    ("w8p",               "VENUE",  -3.5),
    ("has_digit",         "VENUE",  -1.5),
    ("person_name",       "VENUE",  -2.0),
    ("line0",             "VENUE",  -1.5),
    ("block_later",       "VENUE",  -1.0),
    ("line4_8",           "VENUE",  -0.5),
    ("line_late",         "VENUE",  -3.0),
    ("event_word",        "VENUE",  -3.5),
    ("stage_word",        "VENUE",  -3.5),
    ("title_word",        "VENUE",  -2.0),
    ("quoted",            "VENUE",  -2.5),
    ("in_place",          "VENUE",  -5.0),
    ("has_gt",            "VENUE",  -5.0),
    ("source_kw",         "VENUE",  -3.0),
    ("recording_verb",    "VENUE",  -3.0),
    ("equip_word",        "VENUE",  -3.0),
    ("notes_word",        "VENUE",  -1.5),
    ("has_clock",         "VENUE",  -3.0),
    ("has_instr",         "VENUE",  -3.0),
    ("lib_artist_exact",  "VENUE",  -3.0),
    ("lib_musician",      "VENUE",  -2.0),
    ("agree_hint_artist", "VENUE",  -4.0),
    ("sec_personnel",     "VENUE",  -3.0),
    ("sec_lineage",       "VENUE",  -3.0),
    ("label_other",       "VENUE",  -5.0),
    ("after_track_start", "VENUE",  -6.0),
    ("agree_hint_venue",  "VENUE",   4.0),
    ("after_at",          "VENUE",   3.5),
    ("hard_after_first",  "VENUE",   1.0),
    ("paren_tail",        "VENUE",  -2.5),

    # ── EVENT ─────────────────────────────────────────────────────────────
    ("event_word",        "EVENT",   5.0),
    ("not_event",         "EVENT",  -8.0),   # added 2026-10-05 with the hand-set row; not fitted
    ("lib_event",         "EVENT",   5.0),
    ("label_event",       "EVENT",   5.0),
    ("w8p",               "EVENT",  -2.0),
    ("stage_word",        "EVENT",  -1.0),
    ("in_place",          "EVENT",  -4.0),
    ("line0",             "EVENT",  -1.0),
    ("after_track_start", "EVENT",  -5.0),
    ("has_gt",            "EVENT",  -4.0),
    ("recording_verb",    "EVENT",  -3.0),
    ("notes_word",        "EVENT",  -1.5),
    ("line_late",         "EVENT",  -2.0),

    # ── STAGE ─────────────────────────────────────────────────────────────
    ("stage_word",        "STAGE",   5.5),
    ("in_place",          "STAGE",  -4.0),
    ("w8p",               "STAGE",  -2.0),
    ("after_track_start", "STAGE",  -5.0),

    ("paren_tail",        "NOTES",   1.0),
    ("is_sethdr",         "SET_HEADER", 8.0),

    # ── PLACE ─────────────────────────────────────────────────────────────
    ("in_place",          "PLACE",   6.0),
    ("in_place_city",     "PLACE",  -4.5),     # applied on top of in_place when only a bare city
    ("line_is_place",     "PLACE",   1.5),
    ("label_location",    "PLACE",   2.0),
    ("label_place",       "PLACE",   4.0),
    ("w8p",               "PLACE",  -3.0),
    ("line0",             "PLACE",  -1.0),
    ("after_track_start", "PLACE",  -3.0),
    ("has_digit",         "PLACE",  -1.0),

    # ── DATE ──────────────────────────────────────────────────────────────
    ("date_show",         "DATE",    8.0),
    ("date_nonshow",      "DATE",   -4.0),
    ("date_nonshow",      "NOTES",   3.0),
    ("date_nonshow",      "BOILERPLATE", 3.0),
    ("date_show",         "NOTES",   0.0),

    # ── SOURCE ────────────────────────────────────────────────────────────
    ("source_kw",         "SOURCE",  3.0),
    ("label_source",      "SOURCE",  3.0),
    ("w1",                "SOURCE",  1.0),
    ("w2_4",              "SOURCE",  1.0),
    ("w5_7",              "SOURCE", -2.5),
    ("w8p",               "SOURCE", -3.0),
    ("has_gt",            "SOURCE", -4.0),
    ("recording_verb",    "SOURCE", -1.0),
    ("equip_word",        "SOURCE", -2.0),
    ("sec_lineage",       "SOURCE", -1.0),
    ("in_place",          "SOURCE", -3.0),
    ("after_track_start", "SOURCE", -1.0),

    # ── LINEAGE ───────────────────────────────────────────────────────────
    ("has_gt",            "LINEAGE", 4.5),
    ("label_lineage",     "LINEAGE", 4.0),
    ("sec_lineage",       "LINEAGE", 2.5),
    ("recording_verb",    "LINEAGE", 1.5),
    ("equip_word",        "LINEAGE", 1.5),
    ("w5_7",              "LINEAGE", 0.5),
    ("w8p",               "LINEAGE", 0.5),
    ("in_place",          "LINEAGE",-3.0),

    # ── EQUIPMENT ─────────────────────────────────────────────────────────
    ("equip_word",        "EQUIPMENT", 3.0),
    ("label_equipment",   "EQUIPMENT", 3.0),
    ("has_gt",            "EQUIPMENT",-1.0),
    ("in_place",          "EQUIPMENT",-3.0),

    # ── TRACK ─────────────────────────────────────────────────────────────
    ("numbered_track",    "TRACK",   9.0),
    ("in_title_run",      "TRACK",   5.0),
    ("run_matches_audio", "TRACK",   2.0),
    ("after_set_header",  "TRACK",   1.0),

    # ── NOTES ─────────────────────────────────────────────────────────────
    ("notes_word",        "NOTES",   1.5),
    ("w8p",               "NOTES",   1.5),
    ("prose",             "NOTES",   1.5),
    ("line_late",         "NOTES",   1.0),
    ("block_later",       "NOTES",   0.5),
    ("after_track_start", "NOTES",   1.0),
    ("label_notes",       "NOTES",   3.0),
    ("heading",           "NOTES",   4.0),
    ("in_title_run",      "NOTES",  -2.0),

    # ── OTHER ─────────────────────────────────────────────────────────────
    ("in_title_run",      "OTHER",  -2.0),
]

D = -0.6      # any transition not listed below

TRANSITIONS = {
    # start of file
    ("START", "ARTIST"): 1.0, ("START", "TITLE"): 0.5, ("START", "DATE"): 0.0,
    ("START", "VENUE"): -0.5, ("START", "BOILERPLATE"): 0.0, ("START", "TRACK"): 0.0,
    ("START", "NOTES"): 0.0, ("START", "EVENT"): -0.3,
    # billing
    ("ARTIST", "CONNECTOR"): 1.5, ("ARTIST", "DATE"): 1.0, ("ARTIST", "VENUE"): 1.0,
    ("ARTIST", "PLACE"): 0.5, ("ARTIST", "EVENT"): 0.6, ("ARTIST", "MEMBER"): 0.3,
    ("ARTIST", "TITLE"): 0.5, ("ARTIST", "SOURCE"): 0.0, ("ARTIST", "NOTES"): 0.0,
    ("ARTIST", "ARTIST"): -2.0, ("ARTIST", "STAGE"): 0.0,
    ("TITLE", "ARTIST"): 1.0, ("TITLE", "CONNECTOR"): 1.5, ("TITLE", "DATE"): 0.5,
    ("TITLE", "VENUE"): 0.5, ("TITLE", "TITLE"): -0.3, ("TITLE", "PLACE"): 0.3,
    ("TITLE", "EVENT"): 0.3, ("TITLE", "MEMBER"): 0.0,
    ("CONNECTOR", "MEMBER"): 3.0, ("CONNECTOR", "ARTIST"): 0.5, ("CONNECTOR", "CONNECTOR"): -2.0,
    ("MEMBER", "MEMBER"): 2.0, ("MEMBER", "CONNECTOR"): 1.0, ("MEMBER", "VENUE"): 0.5,
    ("MEMBER", "DATE"): 0.5, ("MEMBER", "PLACE"): 0.3, ("MEMBER", "EVENT"): 0.3,
    ("MEMBER", "NOTES"): 0.3, ("MEMBER", "SOURCE"): 0.3, ("MEMBER", "SET_HEADER"): 0.5,
    # where the show happened
    ("DATE", "VENUE"): 1.0, ("DATE", "PLACE"): 0.6, ("DATE", "EVENT"): 0.6, ("DATE", "ARTIST"): 0.0,
    ("DATE", "DATE"): 0.0, ("DATE", "CONNECTOR"): 0.3, ("DATE", "SOURCE"): 0.3, ("DATE", "STAGE"): 0.0,
    ("VENUE", "PLACE"): 1.5, ("VENUE", "DATE"): 0.6, ("VENUE", "EVENT"): 0.0, ("VENUE", "VENUE"): -1.5,
    ("VENUE", "STAGE"): 0.3, ("VENUE", "SOURCE"): 0.3, ("VENUE", "MEMBER"): 0.3,
    ("PLACE", "PLACE"): 1.0, ("PLACE", "VENUE"): 0.8, ("PLACE", "DATE"): 0.6, ("PLACE", "STAGE"): 0.3,
    ("PLACE", "EVENT"): 0.0, ("PLACE", "SOURCE"): 0.3, ("PLACE", "MEMBER"): 0.3, ("PLACE", "NOTES"): 0.0,
    ("EVENT", "VENUE"): 1.0, ("EVENT", "STAGE"): 0.8, ("EVENT", "PLACE"): 0.8, ("EVENT", "DATE"): 0.5,
    ("EVENT", "EVENT"): -1.0,
    ("STAGE", "VENUE"): 0.5, ("STAGE", "PLACE"): 0.6, ("STAGE", "DATE"): 0.5, ("STAGE", "EVENT"): 0.3,
    ("STAGE", "STAGE"): -1.0,
    # body of the file
    ("SOURCE", "SOURCE"): 0.3, ("SOURCE", "LINEAGE"): 1.0, ("SOURCE", "NOTES"): 0.3,
    ("LINEAGE", "LINEAGE"): 1.5, ("LINEAGE", "EQUIPMENT"): 1.0, ("LINEAGE", "SOURCE"): 0.3,
    ("LINEAGE", "NOTES"): 0.3, ("EQUIPMENT", "EQUIPMENT"): 1.5, ("EQUIPMENT", "LINEAGE"): 1.0,
    ("EQUIPMENT", "NOTES"): 0.3, ("EQUIPMENT", "SOURCE"): 0.3,
    ("SET_HEADER", "TRACK"): 2.0, ("SET_HEADER", "SET_HEADER"): 0.3, ("SET_HEADER", "NOTES"): 0.0,
    ("TRACK", "TRACK"): 2.0, ("TRACK", "SET_HEADER"): 1.0, ("TRACK", "NOTES"): 0.3, ("TRACK", "OTHER"): 0.3,
    ("NOTES", "NOTES"): 0.6, ("NOTES", "SET_HEADER"): 0.5, ("NOTES", "TRACK"): 0.3, ("NOTES", "LINEAGE"): 0.3,
    ("NOTES", "SOURCE"): 0.3, ("NOTES", "MEMBER"): 0.0, ("NOTES", "BOILERPLATE"): 0.3,
    ("OTHER", "OTHER"): 0.3, ("OTHER", "NOTES"): 0.3,
    ("BOILERPLATE", "BOILERPLATE"): 1.0, ("BOILERPLATE", "NOTES"): 0.3, ("BOILERPLATE", "TRACK"): 0.3,
    ("BOILERPLATE", "SET_HEADER"): 0.3,
}
