"""app/utils/engineers.py -- Engineer lookup and the recording <-> engineer glue.

Engineers are credited on LIVE recordings only, one per role. A name from a
form, a tag or an info file resolves to an existing Engineer by name or alias
(norm_key: case, accents, punctuation and spacing ignored) and is otherwise
created on save. Nicknames ("Bornfree", "MIKE") are stored as written.
"""
from app.extensions import db
import re

# recording column -> role word used in "Name (role)" tags
ROLE_COLUMNS = {
    "recording": "recording_engineer",
    "transfer":  "transfer_engineer",
    "mastering": "mastering_engineer",
}
FIELDS = ("recording_engineer", "transfer_engineer", "mastering_engineer")


def engineer_key(name):
    """Comparison key: norm_key with spaces removed ("Born Free" == "Bornfree")."""
    from app.utils.reader.library import norm_key   # lazy: keeps this module cycle-free
    return norm_key(name).replace(" ", "")


def find_engineer(name):
    """Existing Engineer matching `name` by name or alias, else None."""
    from app.models.engineer import Engineer
    from app.models.alias import EngineerAlias
    name = (name or "").strip()
    key = engineer_key(name)
    if not key:
        return None
    for e in db.session.query(Engineer).all():
        if engineer_key(e.name) == key:
            return e
    for al in db.session.query(EngineerAlias).all():
        if engineer_key(al.alias) == key:
            return db.session.get(Engineer, al.engineer_id)
    return None


def find_or_create_engineer(name):
    """Engineer for `name` (existing by name/alias, or a new row, flushed). None for blank."""
    from app.models.engineer import Engineer
    name = (name or "").strip()
    if not name:
        return None
    e = find_engineer(name)
    if e is None:
        e = Engineer(name=name[:255])
        db.session.add(e)
        db.session.flush()
    return e


def engineer_ref(e):
    """{id, name} for a recording payload, or None."""
    return {"id": e.id, "name": e.name} if e is not None else None


def recording_engineer_payload(rec):
    """The three role keys for a recording payload. Live recordings only: a
    studio recording gets no keys at all."""
    if getattr(rec, "kind", "live") == "studio":
        return {}
    return {f: engineer_ref(getattr(rec, f)) for f in FIELDS}


def apply_engineer_names(rec, data, *, clear_blank=True):
    """Set rec's three engineer FKs from names in `data` (keys recording_engineer,
    transfer_engineer, mastering_engineer; each a string). A key absent from
    `data` is left alone; an empty string clears (when clear_blank). Studio
    recordings are never touched. Returns True when anything changed."""
    if getattr(rec, "kind", "live") == "studio":
        return False
    changed = False
    for f in FIELDS:
        if f not in data:
            continue
        raw = data.get(f)
        if isinstance(raw, dict):
            raw = raw.get("name")
        name = (raw or "").strip() if isinstance(raw, str) or raw is None else str(raw).strip()
        if not name:
            if clear_blank and getattr(rec, f + "_id") is not None:
                setattr(rec, f + "_id", None)
                changed = True
            continue
        e = find_or_create_engineer(name)
        if getattr(rec, f + "_id") != e.id:
            setattr(rec, f + "_id", e.id)
            changed = True
    return changed


def engineer_role_rows(engineer_id):
    """[(Recording, [roles])] for every recording that credits this engineer in
    any role, live only."""
    from app.models.recording import Recording
    rows = db.session.query(Recording).filter(
        (Recording.recording_engineer_id == engineer_id)
        | (Recording.transfer_engineer_id == engineer_id)
        | (Recording.mastering_engineer_id == engineer_id)).filter(
        Recording.kind != "studio").all()
    out = []
    for r in rows:
        roles = [role for role, col in ROLE_COLUMNS.items()
                 if getattr(r, col + "_id") == engineer_id]
        out.append((r, roles))
    return out


# ── Reading engineer credits out of tags and text ──────────────────────────
#
# Used by the resolver (info file, COMMENT tag, ENGINEER/TIPL/TAPER/TRANSFER
# tags) and by the tag writer. Everything here is pure text work: no DB.

ROLES = ("recording", "transfer", "mastering")

# Words that mean the "name" is really gear, a medium or a source type.
_GEAR_WORDS = {
    "mic", "mics", "microphone", "microphones", "dat", "sony", "neumann", "schoeps", "akg",
    "dpa", "nakamichi", "nak", "sennheiser", "sbd", "aud", "fm", "mtx", "matrix", "cd", "flac",
    "shn", "wav", "eac", "soundboard", "audience", "cassette", "reel", "dsd", "tascam",
    "marantz", "oade", "naiant", "sound", "devices", "zoom", "edirol", "roland", "pcm",
    "cmc", "cardioid", "omni", "khz", "bit", "tape", "deck", "preamp", "pre", "mixer", "board",
    "master", "source", "unknown", "unk", "n/a", "na", "none", "tbd", "lineage", "transfer",
    "audacity", "wavelab", "edit", "soundforge", "mk", "ck", "usb", "minidisc", "md",
}
_MAX_NAME = 40

# Phrase -> roles it credits. Matched anywhere in a line, case-insensitive; the
# name is the text after it. Longer phrases first so "recorded and mastered by"
# is not read as plain "recorded by".
_PHRASES = [
    (r"recorded\s*(?:and|&|\+|/|,)\s*(?:transferred|transfered|transfer)\s+by", ("recording", "transfer")),
    (r"recorded\s*(?:and|&|\+|/|,)\s*mastered\s+by",                         ("recording", "mastering")),
    (r"(?:transferred|transfered|transfer)\s*(?:and|&|\+|/|,)\s*master(?:ed|ing)?\s+by", ("transfer", "mastering")),
    (r"transfer\s*/\s*master\s+by",                                          ("transfer", "mastering")),
    (r"(?:edited|edit)\s*(?:and|&|\+|/|,)\s*mastered\s+by",                  ("mastering",)),
    (r"mics?\s*/\s*recorded\s+by",                                           ("recording",)),
    (r"source\s*(?:&|and)\s*taper\s*[:\-\u2013]?",                           ("recording",)),
    (r"taped\s+by",                                                           ("recording",)),
    (r"taper\s*(?:by\b|[:\-\u2013])",                                        ("recording",)),
    (r"recorded\s+by",                                                        ("recording",)),
    (r"recording\s+by",                                                       ("recording",)),
    (r"(?:transferred|transfered)\s+by",                                      ("transfer",)),
    (r"(?:transfer|xfer|xfered)\s+by",                                        ("transfer",)),
    (r"transfer\s*:",                                                         ("transfer",)),
    (r"mastered\s+by",                                                        ("mastering",)),
    (r"mastering\s+by",                                                       ("mastering",)),
    (r"mastering(?:\s+engineer)?\s*:",                                        ("mastering",)),
]
_PHRASE_RE = re.compile(r"\b(?:" + "|".join(f"(?P<p{i}>{pat})" for i, (pat, _r) in enumerate(_PHRASES)) + r")",
                        re.I)
# Where a name stops.
_NAME_CUT_RE = re.compile(r"[,;(\[\"]|->|>|\s(?:using|with|on|at|from)\s|\s[-\u2013\u2014]\s", re.I)


def clean_engineer_name(text, *, cut=True):
    """A usable engineer name from `text`, or None. With cut=True the text is
    trimmed at the first delimiter (comma, semicolon, bracket, quote, "using",
    "with", "on", "at", " - ", "->"); gear, source types, digits-only and
    over-long strings are rejected."""
    t = (text or "").strip().lstrip(":-\u2013\u2014 \t").strip()
    if cut:
        m = _NAME_CUT_RE.search(t)
        if m:
            t = t[:m.start()]
    t = re.sub(r"\s+", " ", t).strip(" \t.:;-\u2013\u2014*_'")
    if not t or len(t) > _MAX_NAME or not re.search(r"[A-Za-z]", t):
        return None
    if re.fullmatch(r"[\d\W]+", t) or "=" in t or ">" in t:
        return None
    for tok in re.findall(r"[A-Za-z0-9/]+", t.lower()):
        if tok in _GEAR_WORDS:
            return None
        # model numbers and specs: "mk4", "d5", "cm300", "24bit"
        if re.search(r"\d", tok) and re.search(r"[a-z]", tok) and len(tok) <= 7:
            return None
    return t


def extract_engineers(text):
    """{'recording': name, 'transfer': name, 'mastering': name} found in free text
    (an info file or a COMMENT tag). The first valid name per role wins; roles
    with nothing valid are absent. Never raises."""
    out = {}
    for line in (text or "").splitlines():
        for m in _PHRASE_RE.finditer(line):
            idx = next((int(k[1:]) for k, v in m.groupdict().items() if v is not None), None)
            if idx is None:
                continue
            name = clean_engineer_name(line[m.end():])
            if not name:
                continue
            for role in _PHRASES[idx][1]:
                out.setdefault(role, name)
    return out


_TAG_ROLE_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def _role_word(word):
    w = (word or "").lower()
    if any(x in w for x in ("record", "tap")):
        return "recording"
    if "transfer" in w or "xfer" in w:
        return "transfer"
    if "master" in w:
        return "mastering"
    if w.strip() in ("engineer", ""):
        return "recording"
    return None


def parse_engineer_values(values):
    """Roles from ENGINEER tag values: "Name (recording)" -> that role, a bare
    "Name" -> recording; a parenthesised word that is not one of our roles
    ("Bob (mixing)") is skipped. First value per role wins."""
    out = {}
    for v in values or []:
        v = str(v or "").strip()
        if not v:
            continue
        m = _TAG_ROLE_RE.match(v)
        if m:
            role, name = _role_word(m.group(2)), m.group(1)
        else:
            role, name = "recording", v
        name = clean_engineer_name(name, cut=False)
        if role and name:
            out.setdefault(role, name)
    return out


def engineers_from_tags(tags):
    """{'recording_engineer':..., ...} from one file's tag mapping: ENGINEER
    (already includes MP3 TIPL via the adapter), then TAPER / TRANSFER style
    tags fill any role still empty. `tags` is anything with .get(KEY) -> list."""
    def vals(key):
        v = tags.get(key)
        return list(v) if v else []

    found = parse_engineer_values(vals("ENGINEER"))
    for key, role in (("TAPER", "recording"), ("TAPER_NAME", "recording"), ("RECORDEDBY", "recording"),
                      ("TRANSFER", "transfer"), ("TRANSFERRED_BY", "transfer"), ("TRANSFERREDBY", "transfer"),
                      ("MASTERED_BY", "mastering"), ("MASTERING", "mastering")):
        if role in found:
            continue
        for v in vals(key):
            name = clean_engineer_name(v)
            if name:
                found[role] = name
                break
    return {f"{r}_engineer": found[r] for r in ROLES if r in found}


def engineer_tag_values(recording):
    """The multi-valued ENGINEER tag for a recording: ["Name (recording)", ...].
    Empty for a studio record or one with no engineers."""
    if getattr(recording, "kind", "live") == "studio":
        return []
    out = []
    for role in ROLES:
        e = getattr(recording, f"{role}_engineer", None)
        if e is not None and (e.name or "").strip():
            out.append(f"{e.name.strip()} ({role})")
    return out
