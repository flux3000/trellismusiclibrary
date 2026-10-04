"""
Role decoder (Resolver v2, chunk 3, 2026-10-03): approach A2 of the design
proposal, a Viterbi search over the file's units.

Every unit gets one role; the labelling that scores best over the whole file
wins. A unit's emission score is the sum of weight * feature value for the role
(weights.EMISSION) plus the role's bias; moving from one unit's role to the next
costs or earns a transition weight (weights.TRANSITIONS). So "members follow
with" and "place follows venue" are weights, not special cases.

Each decoded unit keeps what the Resolver pane will show later: its role and
local score, the three features that contributed most to that role, and the
runner-up role with its score. The local score of a role is its emission plus the
transitions into and out of it from the neighbours the best path chose.
"""
from dataclasses import dataclass, field

from . import weights as W
from .features import Doc, build_doc

_R = {r: i for i, r in enumerate(W.ROLES)}
_N = len(W.ROLES)

# feature -> [(role index, weight)]
_BY_FEATURE = {}
for _f, _role, _w in W.EMISSION:
    _BY_FEATURE.setdefault(_f, []).append((_R[_role], _w))
_BIAS = [W.BIAS[r] for r in W.ROLES]
_TRANS = [[W.D] * _N for _ in range(_N)]
_START = [W.D] * _N
for (_a, _b), _w in W.TRANSITIONS.items():
    if _a == "START":
        _START[_R[_b]] = _w
    else:
        _TRANS[_R[_a]][_R[_b]] = _w
_ALLOWED = {k: sorted(_R[r] for r in v) for k, v in W.ALLOWED.items()}
_NEG = float("-inf")


@dataclass
class Decoded:
    unit: object
    role: str
    score: float
    features: list = field(default_factory=list)     # top three features, bias, transition, other
    runner_up: tuple = ("", 0.0)

    @property
    def text(self):
        return self.unit.text

    def to_dict(self):
        u = self.unit
        return {"text": u.text, "span": [u.start, u.end], "line": u.line, "role": self.role,
                "score": round(self.score, 2),
                "features": [[n, round(c, 2)] for n, c in self.features],
                "runner_up": {"value": self.runner_up[0], "score": round(self.runner_up[1], 2)}}


def emission(unit):
    """Scores of every role for one unit: list of floats (-inf where not allowed)."""
    sc = [_NEG] * _N
    for r in _ALLOWED[unit.kind]:
        sc[r] = _BIAS[r]
    for name, val in unit.feats.items():
        for r, w in _BY_FEATURE.get(name, ()):
            if sc[r] != _NEG:
                sc[r] += w * val
    if not unit.feats.get("connector_word"):
        sc[_R["CONNECTOR"]] = _NEG      # a connector is only ever a connector word
    return sc


def _contributions(unit, role_idx, transition, own):
    """Top three features, then the bias and the transition terms by name, then
    whatever feature weight is left, so the entries sum to the segment score."""
    out = []
    for name, val in unit.feats.items():
        for r, w in _BY_FEATURE.get(name, ()):
            if r == role_idx and w * val:
                out.append((name, w * val))
    out.sort(key=lambda c: -abs(c[1]))
    top = out[:3]
    named = [("bias", _BIAS[role_idx]), ("transition", transition)]
    rest = own - sum(c for _, c in top) - sum(c for _, c in named)
    if abs(rest) > 0.005:
        named.append(("other", rest))
    return top + named


def decode(doc):
    """Best labelling of doc.units -> [Decoded]."""
    units = doc.units
    n = len(units)
    if not n:
        return []
    em = [emission(u) for u in units]
    best = [[_NEG] * _N for _ in range(n)]
    back = [[-1] * _N for _ in range(n)]
    for r in range(_N):
        if em[0][r] != _NEG:
            best[0][r] = em[0][r] + _START[r]
    for i in range(1, n):
        prev = best[i - 1]
        pr_roles = [p for p in range(_N) if prev[p] != _NEG]
        for r in range(_N):
            e = em[i][r]
            if e == _NEG:
                continue
            bs, bp = _NEG, -1
            for p in pr_roles:
                v = prev[p] + _TRANS[p][r]
                if v > bs:
                    bs, bp = v, p
            best[i][r] = bs + e
            back[i][r] = bp
    r = max(range(_N), key=lambda k: best[n - 1][k])
    path = [0] * n
    for i in range(n - 1, -1, -1):
        path[i] = r
        r = back[i][r]
    out = []
    for i, u in enumerate(units):
        ri = path[i]
        pr = path[i - 1] if i else None
        nx = path[i + 1] if i + 1 < n else None

        def local(k):
            v = em[i][k]
            if v == _NEG:
                return _NEG
            v += _START[k] if pr is None else _TRANS[pr][k]
            if nx is not None:
                v += _TRANS[k][nx]
            return v
        own = local(ri)
        trans = own - em[i][ri]
        alts = [(local(k), k) for k in range(_N) if k != ri and em[i][k] != _NEG]
        ru = max(alts) if alts else (_NEG, ri)
        out.append(Decoded(u, W.ROLES[ri], own, _contributions(u, ri, trans, own),
                           (W.ROLES[ru[1]], ru[0] if ru[0] != _NEG else 0.0)))
    return out


def decode_text(text, library=None, n_audio=None, hints=None):
    doc = build_doc(text, library, n_audio, hints)
    return doc, decode(doc)
