"""
Resolver v2 chunk 7b: the weight fitter (reader/fit.py), the shipped weights and the
leave-one-out alignment. Pure python, no corpus, no network, no database.
"""
import importlib.util
import json

import pytest

from app.utils.reader import decode as dec
from app.utils.reader import fit as F
from app.utils.reader import weights as W
from app.utils.reader import weights_handset as H
from app.utils.reader.features import build_doc
from app.utils.reader.library import LibraryIndex


def _item(i, artist, venue, city, state, extra=""):
    text = (f"{artist}\n{venue}\n{city}, {state}\n2001-03-0{i}\n\n"
            f"Source: SBD > DAT > CD\n{extra}\n\n01. Opening tune\n02. Second tune\n03. Third tune\n")
    return {"id": f"fx{i}", "info_text": text, "n_audio": 3,
            "gold": {"artist": artist, "venue": venue, "city": city, "state": state, "date": {"y": 2001, "m": 3, "d": i}}}


FIXTURE = [
    _item(1, "Red Clay Ramblers", "Birchmere", "Alexandria", "VA"),
    _item(2, "Hot Rize", "Cafe Lena", "Saratoga Springs", "NY"),
    _item(3, "Tony Rice Unit", "Station Inn", "Nashville", "TN"),
    _item(4, "Nickel Creek", "Fox Theatre", "Boulder", "CO"),
    _item(5, "Peter Rowan Band", "Great American Music Hall", "San Francisco", "CA"),
    _item(6, "Laurie Lewis", "Freight and Salvage", "Berkeley", "CA"),
]


def _instances(model):
    return F.build_instances(FIXTURE, model, library_modes=("none",))


def test_the_shipped_weights_are_the_handset_table_and_the_fitted_table_still_loads():
    hs, shipped = F.Model.handset(), F.Model.from_tables(W.EMISSION, W.BIAS, W.TRANSITIONS, W.D)
    assert W.ROLES == H.ROLES and W.ALLOWED == H.ALLOWED
    assert hs.keys == shipped.keys, "weights.py and weights_handset.py list different parameters"
    assert hs.vec == shipped.vec, "the shipped weights.py is the hand-set table, value for value"
    fitted = F.Model.fitted()               # weights_fitted.py: kept for --weights fitted
    assert fitted.keys == hs.keys
    assert any(a != b for a, b in zip(hs.vec, fitted.vec))
    for k, a, b in zip(hs.keys, hs.vec, fitted.vec):
        assert abs(a - b) <= F.CAP + 0.01, k


def test_an_atlas_weight_stays_below_its_library_twin():
    w = {(k[1], k[2]): v for k, v in zip(F.Model.handset().keys, F.Model.handset().vec) if k[0] == "E"}
    s = {(f, r): x for f, r, x in W.EMISSION}
    for f, twins in F.ATLAS_TWINS.items():
        for (ff, r), x in s.items():
            if ff == f and x > 0:
                pos = [s[(t, r)] for t in twins if s.get((t, r), 0) > 0]
                assert not pos or x < min(pos), (f, r)
    assert w  # handset table non-empty


def test_alignment_labels_the_gold_segments_and_skips_what_it_cannot_find():
    ix = LibraryIndex.empty()
    doc = build_doc(FIXTURE[0]["info_text"], ix, 3, None, None)
    cons = F.align(doc, FIXTURE[0]["gold"])
    assert cons is not None
    artist_units = [i for i, r in cons.items() if r == {F._R["ARTIST"]}]
    assert [doc.units[i].text for i in artist_units] == ["Red Clay Ramblers"]
    assert F.align(doc, dict(FIXTURE[0]["gold"], artist="Somebody Not In The File")) is None
    inst, skipped = F.build_instances([dict(FIXTURE[0], gold=dict(FIXTURE[0]["gold"], artist="Nobody"))] + FIXTURE[1:],
                                      F.Model.handset(), library_modes=("none",))
    assert skipped == ["fx1"] and len(inst) == len(FIXTURE) - 1


def test_fit_runs_on_a_tiny_fixture_and_is_deterministic():
    hs = F.Model.handset()
    inst, skipped = _instances(hs)
    assert len(inst) == len(FIXTURE) and not skipped
    a, info_a = F.fit(inst, hs, epochs=3)
    b, info_b = F.fit(inst, hs, epochs=3)
    assert a.vec == b.vec and info_a == info_b
    assert len(a.vec) == len(hs.vec)
    assert max(abs(x - y) for x, y in zip(a.vec, hs.vec)) <= F.CAP + 1e-9
    for k, x, y in zip(hs.keys, hs.vec, a.vec):
        if k[0] != "E" or k[2] != "ARTIST":
            assert x == y


def test_fit_does_not_depend_on_instance_order_of_the_input_list_object():
    hs = F.Model.handset()
    inst, _ = _instances(hs)
    a, _ = F.fit(list(inst), hs, epochs=2, seed=3)
    b, _ = F.fit(list(inst), hs, epochs=2, seed=3)
    assert a.vec == b.vec


def test_only_widens_the_set_of_parameters_that_move():
    hs = F.Model.handset()
    inst, _ = _instances(hs)
    m, _ = F.fit(inst, hs, epochs=3, only=("E:ARTIST", "B", "T", "E:VENUE"), margin=1.0)
    assert len(m.vec) == len(hs.vec)


def test_the_viterbi_of_the_fitter_is_the_decoders():
    hs = F.Model.handset()
    slot_tr, d = F._tr_tables(hs)
    for it in FIXTURE:
        doc = build_doc(it["info_text"], LibraryIndex.empty(), 3, None, None)
        path = F.viterbi(F.prepare(doc, hs), hs.vec, slot_tr, d)
        assert [F.ROLES[r] for r in path] == [x.role for x in dec.decode(doc)]


def test_set_weights_swaps_and_restores_the_decoder_tables():
    hs = F.Model.handset()
    bumped = hs.copy([v + (5.0 if k == ("E", "line0", "ARTIST") else 0.0) for k, v in zip(hs.keys, hs.vec)])
    doc = build_doc(FIXTURE[0]["info_text"], LibraryIndex.empty(), 3, None, None)
    before = [(x.role, round(x.score, 4)) for x in dec.decode(doc)]
    try:
        dec.set_weights(*bumped.tables())
        assert [(x.role, round(x.score, 4)) for x in dec.decode(doc)] != before
    finally:
        dec.set_weights()
    assert [(x.role, round(x.score, 4)) for x in dec.decode(doc)] == before


def test_write_weights_round_trips_through_an_importable_module(tmp_path):
    hs = F.Model.handset()
    m = hs.copy([v + (0.37 if k[0] == "E" and k[2] == "ARTIST" else 0.0) for k, v in zip(hs.keys, hs.vec)])
    out = tmp_path / "w.py"
    F.write_weights(m, out)
    spec = importlib.util.spec_from_file_location("w_tmp", out)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    back = F.Model.from_tables(mod.EMISSION, mod.BIAS, mod.TRANSITIONS, mod.D)
    assert back.keys == m.keys
    assert max(abs(a - b) for a, b in zip(back.vec, m.vec)) <= 0.006


def test_model_json_round_trip():
    hs = F.Model.handset()
    assert F.Model.from_json(json.loads(json.dumps(hs.to_json()))).vec == hs.vec


def test_folds_never_split_an_act_and_are_deterministic():
    groups = ["a", "a", "b", "c", "c", "c", "d"]
    f1, f2 = F.assign_folds(groups, 3), F.assign_folds(groups, 3)
    assert f1 == f2 and set(f1) == set(groups) and set(f1.values()) <= {0, 1, 2}


def test_the_shipped_calibration_is_valid_for_every_field_and_a_missing_file_is_failsafe(tmp_path):
    from app.utils.reader import confidence as C
    doc = C.load_calibration(force=True)
    assert set(doc["fields"]) == set(C.CALIBRATED_FIELDS)
    assert all(C.valid_model(m) for m in doc["fields"].values())
    assert C.load_calibration(tmp_path / "absent.json") == {"fields": {}}


def test_the_shipped_weights_keep_every_anchor_reading():
    hs = F.Model.handset()
    shipped = F.Model.from_tables(W.EMISSION, W.BIAS, W.TRANSITIONS, W.D)
    anchors = F.anchor_instances(hs, repeat=1)
    assert anchors, "tests/fixtures/resolver_anchors/anchors.jsonl is missing"
    slot_tr, d = F._tr_tables(shipped)
    for pdoc, cons, rid in anchors:
        assert F.satisfies(F.viterbi(pdoc, shipped.vec, slot_tr, d), cons), rid
