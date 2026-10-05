"""
Fitting the decoder weights (Resolver v2, chunk 7b, 2026-10-05).

An averaged structured perceptron over the Viterbi decoder of reader/decode.py. The
parameters are exactly the rows of reader/weights.py: every (feature, role) emission
weight, every role's bias, and every listed transition. Nothing is added to the feature
set and no feature is keyed on an artist, a venue or a corpus.

Training labels come from the gold fields, not from a hand-labelled role per segment:

    align(doc, gold)   the segment whose text is the gold artist must be ARTIST; a segment
                       that is the gold venue, event or stage (G3: any comma part of the
                       catalog's venue string) must be one of VENUE, EVENT, STAGE; a segment
                       that is only the gold city, state or country must be PLACE. Every
                       other segment is free: the best labelling that keeps those
                       constraints is the gold path ("constrained Viterbi"), so a segment
                       nobody labelled is not forced to anything.

A perceptron step compares that path with the unconstrained best path under the current
weights and moves the weights by the difference of the two paths' feature counts. A row
whose artist matches no segment cannot be aligned and is skipped (and counted).

Which parameters move (ONLY): the ARTIST emission rows. Fitted over every row, the perceptron
raised the artist field (held-out wrong 89 -> 80 on G1, 93 -> 82 on G3 before recalibration) but
also filled the venue field with wrong text (132 -> 153 and 129 -> 164 wrong); the venue
has no headroom in the roles (the hand-set path already meets the gold on 84% of G1 and 95% of G3
rows whose venue is in the text), so the other rows, the biases and the transitions are left at
their hand-set values. `--only` widens the set for a later experiment.

Anchors: the inline cases of tests/test_reader_decode.py and the G4 fixture texts are pinned by unit tests, so
every fold trains on them too (anchor_instances: gold = their hand-set role path, ANCHOR_REPEAT copies each).
Without them the corpus-only fit broke four of those tests (a second name on the artist line, a line-two
artist) while moving the corpus numbers by a handful of rows.

Gold constraints include "negatives": a segment sharing no word with the gold artist cannot be the
ARTIST, one sharing none with the gold venue/event parts cannot be the VENUE (align(negatives=True)).

Regularised toward the hand-set weights (weights_handset.py), which are the starting point:

    * the fitted change of each weight is shrunk by rows/(rows + K), rows being the number
      of training rows that ever touched it, so a rare feature barely moves;
    * no weight moves more than CAP from its hand-set value;
    * an Atlas weight never reaches the library's twin (tests/test_atlas_features.py).

Deterministic: rows are visited in a seeded order, ties break the same way every run.
Pure: no network, no database; the app package carries it (python3 -m app.utils.reader.fit)
because the weights it writes are part of the product.
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

from . import weights_handset as H
from .library import act_core, norm_key

ROLES = H.ROLES
_R = {r: i for i, r in enumerate(ROLES)}
_N = len(ROLES)
_NEG = float("-inf")

EPOCHS = 10
ETA = 0.3              # points per mistake
MARGIN = 0.0           # >0: a role the gold forbids is scored this much higher while training (loss-augmented)
ONLY = ("E:ARTIST",)   # the parameters that move: the ARTIST emission rows (see the module docstring)
SCALE = 1.0            # how much of the fitted move is kept (1 = all)
SHRINK_K = 10          # rows that must touch a weight before its change counts in full
CAP = 3.0              # largest move from the hand-set value
SEED = 7

# Atlas features are the weaker twin of these library features (tests/test_atlas_features.py).
ATLAS_TWINS = {
    "atl_artist": ("lib_artist_exact", "lib_artist_core"), "atl_artist_fz": ("lib_artist_exact", "lib_artist_core"),
    "atl_musician": ("lib_musician",),
    "atl_venue": ("lib_venue", "lib_venue_in"), "atl_venue_fz": ("lib_venue", "lib_venue_in"),
    "atl_show_place": ("lib_venue",), "atl_show_event": ("lib_event",),
    "atl_event": ("lib_event",), "atl_event_fz": ("lib_event",),
}
ATLAS_MARGIN = 0.1


# ── the parameter vector ─────────────────────────────────────────────────────

class Model:
    """The weights as one list, with the name of each slot: ("E", feature, role),
    ("B", role) or ("T", previous role or "START", role)."""

    def __init__(self, keys, vec, d):
        self.keys = keys
        self.vec = vec
        self.d = d
        self.index = {k: i for i, k in enumerate(keys)}

    @classmethod
    def from_tables(cls, emission, bias, transitions, d):
        keys, vec = [], []
        for f, r, w in emission:
            keys.append(("E", f, r))
            vec.append(float(w))
        for r in ROLES:
            keys.append(("B", r))
            vec.append(float(bias[r]))
        for (a, b), w in transitions.items():
            keys.append(("T", a, b))
            vec.append(float(w))
        return cls(keys, vec, d)

    @classmethod
    def handset(cls):
        return cls.from_tables(H.EMISSION, H.BIAS, H.TRANSITIONS, H.D)

    @classmethod
    def fitted(cls):
        """The last fitted table kept beside the shipped one (weights_fitted.py)."""
        from . import weights_fitted as W
        return cls.from_tables(W.EMISSION, W.BIAS, W.TRANSITIONS, W.D)

    def copy(self, vec=None):
        return Model(self.keys, list(self.vec if vec is None else vec), self.d)

    def tables(self):
        """(emission, bias, transitions, d) in the shapes decode.set_weights takes."""
        em, bias, tr = [], {}, {}
        for k, w in zip(self.keys, self.vec):
            if k[0] == "E":
                em.append((k[1], k[2], w))
            elif k[0] == "B":
                bias[k[1]] = w
            else:
                tr[(k[1], k[2])] = w
        return em, bias, tr, self.d

    def to_json(self):
        return {"keys": [list(k) for k in self.keys], "vec": self.vec, "d": self.d}

    @classmethod
    def from_json(cls, o):
        return cls([tuple(k) for k in o["keys"]], list(o["vec"]), o["d"])


# ── a document prepared for fast decoding ────────────────────────────────────

class PDoc:
    """Per unit: the roles it may take and, per role, [(slot, feature value)]."""
    __slots__ = ("allowed", "feats", "kinds", "n")

    def __init__(self, allowed, feats):
        self.allowed = allowed          # [[role index]]
        self.feats = feats              # [{role index: [(slot, value)]}]
        self.n = len(allowed)


def prepare(doc, model):
    by_feature = {}
    for i, k in enumerate(model.keys):
        if k[0] == "E":
            by_feature.setdefault(k[1], []).append((_R[k[2]], i))
    bias_slot = {_R[k[1]]: i for i, k in enumerate(model.keys) if k[0] == "B"}
    allowed_by_kind = {kind: sorted(_R[r] for r in v) for kind, v in H.ALLOWED.items()}
    allowed, feats = [], []
    for u in doc.units:
        al = [r for r in allowed_by_kind[u.kind]]
        if not u.feats.get("connector_word"):
            al = [r for r in al if r != _R["CONNECTOR"]]
        per = {r: [(bias_slot[r], 1.0)] for r in al}
        for name, val in u.feats.items():
            if not val:
                continue
            for r, slot in by_feature.get(name, ()):
                if r in per:
                    per[r].append((slot, val))
        allowed.append(al)
        feats.append(per)
    return PDoc(allowed, feats)


def _tr_tables(model):
    d = model.d
    slot_tr = {}
    for i, k in enumerate(model.keys):
        if k[0] == "T":
            if k[1] == "START":
                slot_tr[("START", _R[k[2]])] = i
            else:
                slot_tr[(_R[k[1]], _R[k[2]])] = i
    return slot_tr, d


def viterbi(pdoc, vec, slot_tr, d, constraint=None, augment=None, rho=0.0):
    """Best role path (list of role indexes) under `vec`. `constraint` maps unit index to
    the set of roles the gold allows there (others are not considered). `augment` is the same
    map, but the other roles stay in play and score `rho` higher (loss-augmented decoding)."""
    n = pdoc.n
    if not n:
        return []
    em = []
    for i in range(n):
        allowed = pdoc.allowed[i]
        if constraint and i in constraint:
            allowed = [r for r in allowed if r in constraint[i]] or allowed
        fs = pdoc.feats[i]
        row = {r: sum(vec[s] * v for s, v in fs[r]) for r in allowed}
        if augment and i in augment and rho:
            for r in row:
                if r not in augment[i]:
                    row[r] += rho
        em.append(row)
    def t(a, b):
        s = slot_tr.get((a, b))
        return vec[s] if s is not None else d
    prev = {r: e + (vec[slot_tr[("START", r)]] if ("START", r) in slot_tr else d) for r, e in em[0].items()}
    back = []
    for i in range(1, n):
        cur, bp = {}, {}
        for r, e in em[i].items():
            best, arg = _NEG, -1
            for p, pv in prev.items():
                v = pv + t(p, r)
                if v > best or (v == best and p < arg):
                    best, arg = v, p
            cur[r] = best + e
            bp[r] = arg
        prev = cur
        back.append(bp)
    r = max(sorted(prev), key=lambda k: prev[k])
    path = [r]
    for bp in reversed(back):
        r = bp[r]
        path.append(r)
    path.reverse()
    return path


def path_counts(pdoc, path, slot_tr, out, sign):
    """Add sign * (feature counts of `path`) into the dict `out` {slot: count}."""
    prev = None
    for i, r in enumerate(path):
        for s, v in pdoc.feats[i][r]:
            out[s] = out.get(s, 0.0) + sign * v
        key = ("START", r) if prev is None else (prev, r)
        s = slot_tr.get(key)
        if s is not None:
            out[s] = out.get(s, 0.0) + sign
        prev = r


def satisfies(path, constraint):
    return all(path[i] in roles for i, roles in constraint.items())


# ── alignment: gold fields -> allowed roles per segment ──────────────────────

_VENUE_WORDS = {"theatre": "theater", "centre": "center", "grounds": "ground", "fairgrounds": "fairground"}
_STATE_CODE = re.compile(r"^[A-Za-z]{2}$")
_VENUEISH = ("VENUE", "EVENT", "STAGE")


def _canon(s):
    return " ".join(_VENUE_WORDS.get(w, w) for w in norm_key(s).split())


def _tokens(*vals):
    out = set()
    for v in vals:
        out.update(norm_key(v).split())
    return out


def _us_state_names():
    try:
        from app.utils.ingest import _US_STATES
        return {k.lower(): v["name"] for k, v in _US_STATES.items()}
    except Exception:  # noqa: BLE001 -- the fitter runs without the app only on a bare fixture
        return {}


def gold_strings(gold):
    """{"artist": [str], "venueish": [(str, roles)], "place": set of tokens}"""
    artists = [p.strip() for p in str(gold.get("artist") or "").split(" / ") if p.strip()]
    venueish, place_toks = [], set()
    states = _us_state_names()

    def add_place(*vals):
        for v in vals:
            if not v:
                continue
            place_toks.update(norm_key(v).split())
            full = states.get(str(v).strip().lower())
            if full:
                place_toks.update(norm_key(full).split())
    if gold.get("venue_raw"):
        parts = [x.strip() for x in str(gold["venue_raw"]).split(",") if x.strip()]
        if len(parts) >= 3 and _STATE_CODE.match(parts[-1]):
            add_place(parts[-2], parts[-1])
            parts = parts[:-2]
            roles = _VENUEISH
        elif len(parts) >= 2 and _STATE_CODE.match(parts[-1]):
            add_place(parts[-1])
            parts = parts[:-1]
            roles = _VENUEISH
        else:
            roles = _VENUEISH + ("PLACE",)
        from .features import is_festival_like
        extra = tuple(r for r in roles if r == "PLACE")
        for p in parts:
            # a festival-like part is the event (or a venue the assembler moves to Event); another part
            # is the venue, or its stage when the string also names a place of its own
            pr = ("EVENT", "VENUE") if is_festival_like(p) else (("VENUE", "STAGE") if len(parts) > 1 else ("VENUE",))
            venueish.append((p, pr + extra))
    else:
        for field, role in (("venue", "VENUE"), ("event", "EVENT"), ("stage", "STAGE")):
            if gold.get(field):
                venueish.append((str(gold[field]), (role,)))
    add_place(gold.get("city") if "venue_raw" not in gold else None,
              gold.get("state") if "venue_raw" not in gold else None,
              gold.get("country") if "venue_raw" not in gold else None)
    return {"artist": artists, "venueish": venueish, "place": place_toks}


def align(doc, gold, negatives=True):
    """{unit index: set of role indexes} for the units the gold names, or None when the
    gold artist is in no segment (the row cannot be aligned)."""
    gs = gold_strings(gold)
    units = doc.units
    header = [i for i, u in enumerate(units)
              if u.kind in ("seg", "titleline") and not u.feats.get("after_track_start")]
    cons = {}

    def put(i, roles):
        cons.setdefault(i, set()).update(_R[r] for r in roles)

    keys = {norm_key(a) for a in gs["artist"]}
    cores = {act_core(a) for a in gs["artist"]}
    hit = next((i for i in header if norm_key(units[i].text) in keys or act_core(units[i].text) in cores), None)
    if hit is None:
        # A billing of several acts ("A, B and C"): the segment that is one of them.
        parts = {norm_key(p) for a in gs["artist"] for p in re.split(r",|\s+(?:and|&)\s+", a) if p.strip()}
        parts.discard("")
        hit = next((i for i in header if norm_key(units[i].text) in parts), None)
    if hit is None:
        # The artist's line also says something else ("DANNY GATTON  HUNTER COLLEGE"): the
        # segment starts with the artist's name and adds at most three words.
        for i in header:
            words = norm_key(units[i].text).split()
            for k in keys:
                kw = k.split()
                if kw and words[:len(kw)] == kw and len(words) - len(kw) <= 3:
                    hit = i
                    break
            if hit is not None:
                break
    if hit is None:
        return None
    put(hit, ("ARTIST",))
    for text, roles in gs["venueish"]:
        k = _canon(text)
        if not k:
            continue
        hit = next((i for i in header if _canon(units[i].text) == k), None)
        if hit is not None and hit not in cons:
            put(hit, roles)
    pt = gs["place"]
    if pt:
        for i in header:
            toks = set(norm_key(units[i].text).split())
            if toks and toks <= pt and i not in cons:
                put(i, ("PLACE",))
    if negatives:
        _forbid_unrelated(doc, header, cons, gs)
    return cons


def _near(toks, token_sets):
    """The segment may be a variant of one of the gold strings: it contains it or is contained in
    it (word-wise), or the two share at least half of their words."""
    for g in token_sets:
        if not toks or not g:
            continue
        inter = len(toks & g)
        if inter and (toks <= g or g <= toks or inter / len(toks | g) >= 0.5):
            return True
    return False


def _forbid_unrelated(doc, header, cons, gs):
    """A segment that is none of the gold artist's words cannot be the ARTIST; one that is none of
    the gold venue/event parts cannot be the VENUE (when the gold has a venue at all). Without this
    the perceptron only ever learns to ADD roles, and the fitted decoder fills fields with
    whatever segment is left (a track title, a line of the taper's notes)."""
    units = doc.units
    a_sets = [set(norm_key(a).split()) for a in gs["artist"]]
    v_sets = [set(norm_key(t).split()) for t, _ in gs["venueish"]]
    for i, u in enumerate(units):
        if i in cons or u.kind not in ("seg", "titleline"):
            continue
        toks = set(norm_key(u.text).split())
        forbid = set()
        if not _near(toks, a_sets):
            forbid.add(_R["ARTIST"])
        if v_sets and not _near(toks, v_sets):
            forbid.add(_R["VENUE"])
        if forbid:
            cons[i] = {_R[r] for r in H.ALLOWED[u.kind]} - forbid


# ── training ─────────────────────────────────────────────────────────────────

def fit(instances, model0=None, epochs=EPOCHS, eta=ETA, shrink_k=SHRINK_K, cap=CAP, seed=SEED, margin=MARGIN,
        scale=SCALE, freeze=(), only=ONLY):
    """Averaged perceptron. instances: [(PDoc, constraint, row id)]. Returns (Model, info)."""
    model0 = model0 or Model.handset()
    slot_tr, d = _tr_tables(model0)
    w0 = list(model0.vec)
    # freeze: kinds that stay at their hand-set value ("B" bias, "T" transition, "E:ROLE" a role's emission rows);
    # only: when given, the only parameters that move (same tokens, plus "E" for every emission row)
    def _tok(k):
        return (k[0], f"E:{k[2]}") if k[0] == "E" else (k[0],)
    frozen = {i for i, k in enumerate(model0.keys)
              if (set(_tok(k)) & set(freeze)) or (only and not (set(_tok(k)) & set(only)))}
    w = list(w0)
    u = [0.0] * len(w)
    c = 1
    touched = {}
    mistakes = []
    order = list(range(len(instances)))
    rng = random.Random(seed)
    for ep in range(epochs):
        rng.shuffle(order)
        wrong = 0
        for ix in order:
            pdoc, cons, rid = instances[ix]
            pred = viterbi(pdoc, w, slot_tr, d, augment=cons, rho=margin)
            if not satisfies(pred, cons):
                gold = viterbi(pdoc, w, slot_tr, d, cons)
                delta = {}
                path_counts(pdoc, gold, slot_tr, delta, +1.0)
                path_counts(pdoc, pred, slot_tr, delta, -1.0)
                for s, v in delta.items():
                    if v and s not in frozen:
                        w[s] += eta * v
                        u[s] += c * eta * v
                        touched.setdefault(s, set()).add(rid)
                wrong += 1
            c += 1
        mistakes.append(wrong)
    avg = [w[i] - u[i] / c for i in range(len(w))]
    out = []
    for i, k in enumerate(model0.keys):
        n = len(touched.get(i, ()))
        step = scale * (avg[i] - w0[i]) * n / (n + shrink_k)
        step = max(-cap, min(cap, step))
        out.append(w0[i] + step)
    fitted = model0.copy(out)
    _clip_atlas(fitted)
    info = {"rows": len(instances), "epochs": epochs, "mistakes_per_epoch": mistakes,
            "weights_moved": sum(1 for a, b in zip(w0, fitted.vec) if abs(a - b) >= 0.005)}
    return fitted, info


def _clip_atlas(model):
    """An Atlas weight stays below the smallest positive weight of its library twin for the role."""
    w = {(k[1], k[2]): model.vec[i] for i, k in enumerate(model.keys) if k[0] == "E"}
    for i, k in enumerate(model.keys):
        if k[0] != "E" or not k[1].startswith("atl_") or model.vec[i] <= 0:
            continue
        twins = [w[(t, k[2])] for t in ATLAS_TWINS.get(k[1], ()) if w.get((t, k[2]), 0) > 0]
        if twins and model.vec[i] >= min(twins) - ATLAS_MARGIN:
            model.vec[i] = round(min(twins) - ATLAS_MARGIN, 2)


def build_instances(items, model, library_for=None, atlas=None, library_modes=("loo", "none")):
    """(instances, skipped ids). One instance per aligned item and library mode."""
    from .features import build_doc
    from .library import LibraryIndex
    inst, skipped = [], []
    for it in items:
        for mode in library_modes:
            lib = (library_for(it) if (library_for and mode == "loo") else LibraryIndex.empty())
            doc = build_doc(it.get("info_text") or "", lib, int(it.get("n_audio") or 0) or None, None, atlas)
            cons = align(doc, it.get("gold") or {})
            if cons is None:
                skipped.append(str(it.get("id")))
                continue
            pdoc = prepare(doc, model)
            # a constraint no role of the unit's kind can meet says nothing
            cons = {i: roles for i, roles in cons.items() if roles & set(pdoc.allowed[i])}
            if not any(roles == {_R["ARTIST"]} for roles in cons.values()):
                skipped.append(str(it.get("id")))
                continue
            inst.append((pdoc, cons, f"{it.get('id')}:{mode}"))
    return inst, skipped


ANCHOR_REPEAT = 200    # each anchor text is trained on this many times per epoch


def anchor_texts(root=None):
    """[(id, info text)] of the hand-written cases the fit must not break: the inline texts of
    tests/test_reader_decode.py (tests/fixtures/resolver_anchors/anchors.jsonl) and the G4 fixtures'
    .txt files. Empty when the tests folder is not there (a packaged app)."""
    root = Path(root) if root else Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    out = []
    p = root / "resolver_anchors" / "anchors.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                out.append((o["id"], o["info_text"]))
    for t in sorted((root / "resolver").glob("*.txt")):
        out.append((t.stem, t.read_text(encoding="utf-8", errors="replace")))
    return out


def anchor_instances(model, repeat=ANCHOR_REPEAT, root=None):
    """Instances whose gold is the hand-set role path of each anchor text (those cases are pinned by
    unit tests, so the hand-set reading of them is the answer). `model` is the hand-set model."""
    from .features import build_doc
    from .library import LibraryIndex
    slot_tr, d = _tr_tables(model)
    inst = []
    for aid, text in anchor_texts(root):
        doc = build_doc(text, LibraryIndex.empty(), None, None, None)
        pdoc = prepare(doc, model)
        path = viterbi(pdoc, model.vec, slot_tr, d)
        cons = {i: {r} for i, r in enumerate(path)}
        inst += [(pdoc, cons, f"anchor:{aid}:{k}") for k in range(repeat)]
    return inst


def top_changes(handset, fitted, n=15):
    rows = [(abs(b - a), k, a, b) for k, a, b in zip(handset.keys, handset.vec, fitted.vec) if abs(b - a) >= 0.005]
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [{"param": list(k), "handset": round(a, 2), "fitted": round(b, 2)} for _, k, a, b in rows[:n]]


# ── writing weights.py ───────────────────────────────────────────────────────

_NUM = r"-?\d+(?:\.\d+)?"


def _fmt(x):
    s = f"{round(x, 2):.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _col(space, old, new):
    """The whitespace before a number, adjusted so the column keeps its width."""
    if not space:
        return ""
    return " " * max(1, len(space) + len(old) - len(new))


def render_weights(model, template_text, note):
    """weights file text: the hand-set file with every number replaced by the fitted one."""
    w = {k: v for k, v in zip(model.keys, model.vec)}
    t = template_text
    i_bias, i_allowed, i_em = t.index("BIAS = {"), t.index("ALLOWED = {"), t.index("EMISSION = [")
    head, bias, mid, rest = t[:i_bias], t[i_bias:i_allowed], t[i_allowed:i_em], t[i_em:]
    em_end = rest.index("\n]\n") + 3
    em, tail = rest[:em_end], rest[em_end:]

    def em_sub(m):
        f, s1, r, s2, old = m.groups()
        new = _fmt(w[("E", f, r)])
        return f'("{f}",{s1}"{r}",{_col(s2, old, new)}{new})'
    em = re.sub(r'\("(\w+)",(\s*)"(\w+)",(\s*)(' + _NUM + r')\)', em_sub, em)

    def bias_sub(m):
        r, s, old = m.groups()
        new = _fmt(w[("B", r)])
        return f'"{r}":{_col(s, old, new)}{new}'
    bias = re.sub(r'"(\w+)":(\s*)(' + _NUM + r')', bias_sub, bias)

    j = tail.index("TRANSITIONS = {")
    tail_d, tail_tr = tail[:j], tail[j:]

    def tr_sub(m):
        a, b2, s, old = m.groups()
        new = _fmt(w[("T", a, b2)])
        return f'("{a}", "{b2}"):{_col(s, old, new)}{new}'
    tail_tr = re.sub(r'\("(\w+)",\s*"(\w+)"\):(\s*)(' + _NUM + r')', tr_sub, tail_tr)
    out = head + bias + mid + em + tail_d + tail_tr
    doc_end = out.index('"""', 3) + 3
    return '"""\n' + note.strip() + '\n"""' + out[doc_end:]


NOTE = '''
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
This file is the FITTED table kept for comparison (--weights fitted). The table that ships is
weights.py, the hand-set one: Ryan chose it because the fit gained little on held-out acts.
'''


def write_weights(model, path, template=None, note=NOTE):
    template = Path(template or Path(__file__).with_name("weights_handset.py"))   # the --out target decides where it lands
    text = render_weights(model, template.read_text(encoding="utf-8"), note)
    Path(path).write_text(text, encoding="utf-8")


# ── CLI ──────────────────────────────────────────────────────────────────────

def act_group(gold):
    """Group key for folds: the act, billing variants together."""
    from app.utils.artists import _act_key
    a = str((gold or {}).get("artist") or "").split(" / ")[0]
    return _act_key(a) or norm_key(a) or "?"


def assign_folds(groups, k):
    """{group: fold}, balanced by group size, deterministic (same rule as calibrate.assign_folds)."""
    counts = {}
    for g in groups:
        counts[g] = counts.get(g, 0) + 1
    sizes = [0] * k
    out = {}
    for g in sorted(counts, key=lambda g: (-counts[g], str(g))):
        f = min(range(k), key=lambda i: (sizes[i], i))
        out[g] = f
        sizes[f] += counts[g]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m app.utils.reader.fit",
                                 description="Fit the decoder weights on a jsonl corpus (items with info_text and gold)")
    ap.add_argument("corpus", nargs="+", help="one or more jsonl files (G1, G3 shape)")
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--only", default=",".join(ONLY),
                    help='parameters that move: "B", "T", "E:ROLE" tokens, comma separated (default %(default)s)')
    ap.add_argument("--out", help="write the fitted table here (default: print the top changes only)")
    ap.add_argument("--json", help="write the fitted vector as JSON here")
    args = ap.parse_args(argv)
    items = []
    for p in args.corpus:
        with open(p, encoding="utf-8") as f:
            items += [json.loads(line) for line in f if line.strip()]
    model = Model.handset()
    inst, skipped = build_instances(items, model)
    inst += anchor_instances(model)
    fitted, info = fit(inst, model, epochs=args.epochs, only=tuple(x for x in args.only.split(',') if x))
    print(json.dumps({"items": len(items), "instances": len(inst), "skipped": len(skipped), **info}))
    for r in top_changes(model, fitted):
        print(r)
    if args.out:
        write_weights(fitted, args.out)
    if args.json:
        Path(args.json).write_text(json.dumps(fitted.to_json()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
