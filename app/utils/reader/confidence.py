"""
Calibrated confidence for resolved fields (Resolver v2, chunk 6, 2026-10-04).

Every field the resolver fills has candidates, each with a score in the decoder's
log-score points (reader/weights.py), and a NONE candidate that always competes.
For the value the resolver chose:

    logit   its score minus the log-sum-exp of every rival's, NONE included;
            softmax probability p = sigmoid(logit)
    margin  its score minus the best rival's (a close runner-up shows here even
            when p is high)

The logit is mapped to a calibrated probability by the bins in calibration.json
(fitted on G1, folds by artist; see reader/calibrate.py). A field is

    confident   calibrated probability >= tau  and  margin >= m
    tentative   has a value but fails either
    empty       no value, or NONE outscores it (the value is dropped)

tau and m are per field and were fitted so that the held-out rate of
confident-but-wrong stays at or under the target. Nothing here is chosen by feel:
the score tables below are the evidence model, and the file holds what the
harness measured. A table change needs a refit (tests/test_confidence.py fails
while calibration.json was fitted against different tables).
"""
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

CALIBRATION_PATH = Path(__file__).with_name("calibration.json")

# Fields that carry a calibrated confidence. The others (source, lineage, source_tag,
# shnid, album) are read as stated and stay "confident" when they have a value.
CALIBRATED_FIELDS = ("artist", "date", "venue", "event", "stage", "city", "state", "country")

# What NONE scores, per field. The decoder fields compete on the decoder's own scale; a
# date is weighed on the source weights below.
NONE_SCORE = {"artist": 0.0, "date": 1.0, "venue": 0.0, "event": 0.0, "stage": 0.0,
              "city": 0.0, "state": 0.0, "country": 0.0}

# ── Evidence model: what a source adds to a candidate ────────────────────────
# Date: a source that states the same value adds its weight in full; a source that
# states a less precise value inside it adds PARTIAL of its weight. Two sources that
# agree are why a full date can be confident; one source alone is not enough.
DATE_WEIGHT = {"info": 3.0, "folder": 4.0, "tags": 10.0}
DATE_PARTIAL = 0.3
DATE_SUPPORT_EACH = 0.5        # another mention in the info text that agrees
DATE_SUPPORT_CAP = 1.0
DATE_AGAINST_EACH = 0.75       # a usable mention in the info text that states another full date
DATE_AGAINST_CAP = 1.5

# Artist, venue and location: a tag every track agrees on, a folder placed under its
# artist, and the info text (which brings the decoder's own score).
# The tag and folder-tree values cannot be measured on G1 (it has no tags and no artist
# folders); chunk 7 recalibrates them on G3. They are strong because someone wrote them.
# The artist tag sits above every logit the info text produced on G1 (the highest is 18.8):
# a tag every track agrees on is worth more than any reading of the text.
TAGS_SCORE = {"artist": 20.0, "venue": 10.0, "city": 8.0, "state": 8.0, "country": 8.0}
FOLDER_ARTIST_SCORE = 6.0
# A folder named in the Trellis template (reader/folder.py) corroborates what the info text
# says. Measured on G1, whose folders carry the names recordings had when they came in.
FOLDER_TEMPLATE_SCORE = {"artist": 6.0, "venue": 6.0, "city": 5.0, "state": 4.0}
# An info value that arrives without evidence (an older caller built the scan by hand).
INFO_NO_EVIDENCE_SCORE = 4.0

# Location, from where the place peeler (or an Atlas fill) got it.
CITY_SCORE = {("line", "high"): 9.0, ("line", "medium"): 6.0, ("line", "low"): 4.0,
              ("bare", ""): 4.0, ("decoder", ""): 6.0, ("atlas", ""): 5.0}
STATE_SCORE = {"text": 8.0, "atlas": 4.0}
COUNTRY_SCORE = {"text": 8.0, "derived": 7.0, "atlas": 4.0}

# Agreement is evidence: each independent source beyond the first that states the same
# value adds this much to it, up to AGREE_CAP extra sources. Independent sources are the
# ones below (plus a library row marked "independent": the text is exactly an act a person has
# confirmed, see resolve._resolve_artist_field); the library and Atlas features inside an info candidate's score are not.
INDEPENDENT_SOURCES = ("info", "tags", "folder", "parent", "archive")
AGREE_BONUS = {"artist": 3.0, "date": 2.0, "venue": 3.0, "city": 2.0, "state": 2.0, "country": 2.0}
AGREE_CAP = 2

# A runner-up is kept on the field only when it is this close (log-score points).
CLOSE_MARGIN = 4.0

# Fail-safe model for a missing, unreadable or invalid calibration: nothing is ever
# confident, so nothing auto-ingests on a broken file. (tau above 1 no probability reaches.)
DEFAULT_MODEL = {"bins": [], "tau": 1.01, "m": 0.0, "min_logit": 1000.0}

_log = logging.getLogger(__name__)
_warned = set()


def _warn_once(why):
    if why not in _warned:
        _warned.add(why)
        _log.warning("confidence calibration unusable (%s): every field will be tentative", why)


def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def logsumexp(xs):
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


@dataclass
class Assessment:
    confidence: str            # "confident" | "tentative" | "empty"
    logit: float
    margin: float
    p: float                   # calibrated probability that the value is right
    runner_key: object = None  # the closest real rival (never NONE), or None
    none_wins: bool = False


def raw_scores(scores, chosen, field):
    """(logit, margin, runner_key) of `chosen` against the rest of `scores` and NONE."""
    s = scores[chosen]
    rivals = [(v, k) for k, v in scores.items() if k != chosen]
    none = NONE_SCORE.get(field, 0.0)
    pool = [v for v, _ in rivals] + [none]
    best_rival = max(pool)
    runner = max(rivals, key=lambda t: t[0])[1] if rivals else None
    return s - logsumexp(pool), s - best_rival, runner


# ── Calibration file ─────────────────────────────────────────────────────────

_CAL = {"loaded": None}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def valid_model(m):
    """A field entry of the right shape: numeric tau and m, bins a list of {p, lo?, hi?}."""
    if not isinstance(m, dict) or not _num(m.get("tau")) or not _num(m.get("m")):
        return False
    if "min_logit" in m and not _num(m["min_logit"]):
        return False
    bins = m.get("bins", [])
    return isinstance(bins, list) and all(
        isinstance(b, dict) and _num(b.get("p"))
        and (b.get("lo") is None or _num(b["lo"])) and (b.get("hi") is None or _num(b["hi"]))
        for b in bins)


def _checked(data):
    """`data` when it has the calibration.json shape, else None."""
    if isinstance(data, dict) and isinstance(data.get("fields"), dict):
        return data
    return None


def load_calibration(path=None, force=False):
    if path is None and _CAL["loaded"] is not None and not force:
        return _CAL["loaded"]
    p = Path(path) if path else CALIBRATION_PATH
    try:
        data = _checked(json.loads(p.read_text(encoding="utf-8")))
        if data is None:
            _warn_once(f"{p.name} has the wrong shape")
    except (OSError, ValueError) as exc:
        data = None
        _warn_once(f"{p.name}: {type(exc).__name__}")
    if data is None:
        data = {"fields": {}}
    if path is None:
        _CAL["loaded"] = data
    return data


def set_calibration(data):
    """Use `data` (the calibration.json shape) instead of the file. None goes back to the file."""
    _CAL["loaded"] = None if data is None else (_checked(data) or {"fields": {}})


def _model(field, cal=None):
    cal = cal if cal is not None else load_calibration()
    fields = cal.get("fields") if isinstance(cal, dict) else None
    m = fields.get(field) if isinstance(fields, dict) else None
    if valid_model(m):
        return m
    _warn_once(f"no valid model for {field}")
    return DEFAULT_MODEL


def calibrated_p(model, logit):
    bins = model.get("bins") or []
    for b in bins:
        lo, hi = b.get("lo"), b.get("hi")
        if (lo is None or logit >= lo) and (hi is None or logit < hi):
            return b["p"]
    return sigmoid(logit)


def classify(model, logit, margin):
    p = calibrated_p(model, logit)
    ok = p >= model["tau"] and margin >= model["m"] and logit >= model.get("min_logit", float("-inf"))
    return p, ("confident" if ok else "tentative")


def assess(field, scores, chosen, cal=None):
    """Confidence of `chosen` (a key of `scores`) for `field`."""
    logit, margin, runner = raw_scores(scores, chosen, field)
    if scores[chosen] <= NONE_SCORE.get(field, 0.0):
        return Assessment("empty", logit, margin, 0.0, runner, none_wins=True)
    p, label = classify(_model(field, cal), logit, margin)
    return Assessment(label, logit, margin, p, runner)
