"""
Post-filters Lomax applies to what the model returns, before anything is stored or shown.

The prompt states each rule; these enforce it, because a prompt is a request and a filter is a
guarantee. Every filter prefers dropping a value to inventing or keeping a doubtful one.
"""
import re
import unicodedata

# ── State and province: always the abbreviation ──────────────────────────────

_US = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d c": "DC",
}
_CA = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "newfoundland": "NL", "nova scotia": "NS",
    "northwest territories": "NT", "nunavut": "NU", "ontario": "ON", "prince edward island": "PE",
    "quebec": "QC", "saskatchewan": "SK", "yukon": "YT", "yukon territory": "YT",
}
_AU = {
    "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD", "south australia": "SA",
    "western australia": "WA", "tasmania": "TAS", "australian capital territory": "ACT",
    "northern territory": "NT",
}
# Traditional abbreviations, periods already stripped ("Tenn." -> "tenn"). Short words that are also
# ordinary words ("man", "mont") are left out on purpose.
_OLD = {
    "US": {"ala": "AL", "ariz": "AZ", "ark": "AR", "calif": "CA", "cal": "CA", "colo": "CO", "conn": "CT",
           "del": "DE", "fla": "FL", "ill": "IL", "ind": "IN", "kan": "KS", "kans": "KS", "mass": "MA",
           "mich": "MI", "minn": "MN", "miss": "MS", "neb": "NE", "nebr": "NE", "nev": "NV", "okla": "OK",
           "ore": "OR", "oreg": "OR", "penn": "PA", "penna": "PA", "tenn": "TN", "tex": "TX", "wash": "WA",
           "wis": "WI", "wisc": "WI", "wyo": "WY"},
    "CA": {"que": "QC", "ont": "ON", "alta": "AB", "sask": "SK"},
    "AU": {"n s w": "NSW", "q l d": "QLD"},
}
_GROUPS = {"US": {**_US, **_OLD["US"]}, "CA": {**_CA, **_OLD["CA"]}, "AU": {**_AU, **_OLD["AU"]}}
_CODES = {c: set(_GROUPS[c].values()) for c in _GROUPS}
_ALL_CODES = set().union(*_CODES.values())
_AMBIGUOUS = {c for c in _ALL_CODES if sum(c in _CODES[g] for g in _CODES) > 1}      # WA, NT
_COUNTRIES = {"us": "US", "usa": "US", "u s": "US", "u s a": "US", "united states": "US",
              "united states of america": "US", "america": "US",
              "ca": "CA", "can": "CA", "canada": "CA", "au": "AU", "aus": "AU", "australia": "AU"}


def _key(text):
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[.,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def state_code(value, country=None):
    """'Tennessee', 'Tenn.' or 'tn' -> 'TN' for US states, Canadian provinces and territories and
    Australian states and territories. The country decides which list applies: with a country that
    is none of US, CA or AU (Georgia the country) nothing is mapped, and with no country only a name
    that is unambiguous is. A bare code that two countries share (WA, NT) is left as given until a
    country decides it. Anything unrecognised is returned as it came, trimmed."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    ck = _key(country)
    if ck:
        c = _COUNTRIES.get(ck)
        if c is None:
            return raw
        groups = [c]
    else:
        groups = list(_GROUPS)
    k = _key(raw)
    for g in groups:
        if k in _GROUPS[g]:
            return _GROUPS[g][k]
    squashed = k.replace(" ", "")
    code = squashed.upper()
    if len(squashed) <= 3 and any(code in _CODES[g] for g in groups):
        if code in _AMBIGUOUS and not ck:
            return raw
        return code
    return raw


def same_text(a, b):
    return " ".join(str(a or "").lower().split()) == " ".join(str(b or "").lower().split())


# ── Track notes ──────────────────────────────────────────────────────────────

NOTE_MAX_CHARS = 140

_STOP = frozenset("""a an the of on in at to for with by and or but as is was were be been it its this that
these those from into over under out up down off per via we he she they his her their our your you i
also then than so very just""".split())

# Words that only describe what a track self-evidently is. A note made of nothing else, or of
# these plus the track's own title, says nothing the listener cannot hear.
_SELF_EVIDENT = frozenset("""tuning tune tunes tuned intro introduction introductions band banter crowd audience
applause cheers opening instrumental improvisation improvised improv jam jams drums drum space
encore set sets break talk talking chatter announcement announcements noise outro ending end
interlude segue segues song songs track tracks stage soundcheck sound check solo solos
instrumental medley cover live version performance performed played play playing music
begins start starts starting beginning continues continued followed""".split())

_WORD = re.compile(r"[a-z0-9']+")


def _words(text):
    out = []
    for w in _WORD.findall(unicodedata.normalize("NFKD", str(text or "")).lower()):
        w = w.strip("'")
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w:
            out.append(w)
    return out


def _content(text):
    return [w for w in _words(text) if w not in _STOP]


_MARK = re.compile(r"\*+|[\u2020\u2021\u00a7^]+|\[\d+\]")
_NUMBERED = re.compile(r"^\s*(?:disc\s*\d+\s*)?(?:d\d+)?[ -]*0*(\d{1,3})[.)\-:\s]")


def _fold_text(text):
    return " ".join(_words(text))


def _track_line(lines, title, number):
    """The info file line for this track: the one holding its title, else the one numbered for it."""
    want = _fold_text(title)
    if want and not re.match(r"^track \d+$", want):
        for ln in lines:
            if want in _fold_text(ln):
                return ln
    if number is not None:
        for ln in lines:
            m = _NUMBERED.match(ln)
            if m and int(m.group(1)) == int(number):
                return ln
    return None


def _marks_after_title(line, title):
    t = _fold_text(title)
    rest = line
    # cut at the title's last word when it can be found, so a mark in the number prefix is not read
    last = (_words(title) or [""])[-1]
    idx = line.lower().rfind(last) if last else -1
    if idx >= 0:
        rest = line[idx + len(last):]
    return _MARK.findall(rest)


def _footnotes(lines, marks, own_line):
    out = []
    for ln in lines:
        if ln is own_line:
            continue
        stripped = ln.lstrip()
        for m in marks:
            if stripped.startswith(m) and not stripped[len(m):len(m) + 1] == m[-1]:
                out.append(stripped[len(m):])
    return out


_DURATION = re.compile(r"^\s*[\d:.,'\"\s]*(min(ute)?s?|sec(ond)?s?|m|s)?\.?\s*$", re.I)
_PAREN = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")


def _inline_annotation(line, title):
    """The parenthetical or bracketed annotations on the track's own line that are neither a duration,
    a bare footnote number, nor part of the title. Text written plainly after a title is not one."""
    rest = _NUMBERED.sub("", line, count=1)
    own = set(_words(title))
    out = []
    for m in _PAREN.finditer(rest):
        inner = (m.group(1) if m.group(1) is not None else m.group(2)) or ""
        if not inner.strip() or _DURATION.match(inner) or re.fullmatch(r"\s*\d{1,2}\s*", inner):
            continue
        extra = [w for w in _content(inner) if w not in _SELF_EVIDENT and w not in own]
        if extra:
            out.append(inner.strip())
    return " ".join(out)


def _initials_match(note, support):
    """Footnote initials ("JG") that fit a run of capitalised names in the note ("Jerry Garcia")."""
    toks = re.findall(r"\b[A-Z]{2,3}\b", support)
    if not toks:
        return set()
    caps = [w for w in re.findall(r"[A-Za-z']+", note)]
    covered = set()
    for size in (2, 3):
        for i in range(len(caps) - size + 1):
            run = caps[i:i + size]
            if all(w[:1].isupper() for w in run) and "".join(w[0] for w in run).upper() in toks:
                covered.update(_words(" ".join(run)))
    return covered


def filter_track_note(note, title, info_text, current_note=None, number=None):
    """The note, or '' when it must be dropped. A note exists only to carry what the info file says about
    THIS track: through a footnote mark on the track's own line that resolves to a footer line, or a
    parenthetical or bracketed annotation on that line that is not a duration. Every content word of the
    note must come from that text (footnote initials such as "JG" stand for the names they fit). No mark
    and no annotation, or any word from outside, drops the note: what the track is (tuning, announcer,
    applause), song facts (album, year) and outside knowledge are never notes. `current_note` is unused."""
    note = " ".join(str(note or "").split())
    if not note:
        return ""
    if len(note) > NOTE_MAX_CHARS:
        return ""
    words = _content(note)
    if not words:
        return ""
    own = set(_content(title))
    if all(w in _SELF_EVIDENT or w in own for w in words):
        return ""
    lines = str(info_text or "").splitlines()
    line = _track_line(lines, title, number) if lines else None
    if line is None:
        return ""
    support = _footnotes(lines, _marks_after_title(line, title), line)
    inline = _inline_annotation(line, title)
    if inline:
        support.append(inline)
    support_text = " ".join(support)
    if not support_text.strip():
        return ""
    have = set(_words(support_text)) | _initials_match(note, support_text)
    new = list(words)
    if not new or any(w not in have for w in new):
        return ""            # drawn from the footnote or the inline annotation alone, word for word
    caps = {w for tok in re.findall(r"[A-Za-z']+", note)[1:] if tok[:1].isupper() for w in _words(tok)}
    if any(c not in have for c in caps if c not in _STOP):
        return ""
    return note


# ── Lineage: the recording chain only ────────────────────────────────────────

_CHAIN = re.compile(
    r"(>|->|\bdat\b|\bflac\b|\bshn\b|\bwav\b|\bcd\b|\bcdr\b|\bcassette\b|\breel\b|\bmp3\b|\bmic\b|\bmics\b|"
    r"\bmicrophone|\brecorder\b|\bpreamp\b|\bsoundboard\b|\bboard\b|\bmatrix\b|\bsbd\b|\baud\b|\bfm\b|"
    r"\btransfer|\bconverted\b|\bresampled\b|\bnormali[sz]ed\b|\bsplit\b|\btracked\b|\btracking\b|"
    r"\baudacity\b|\bwavelab\b|\bsound forge\b|\badobe\b|\bizotope\b|\bxld\b|\btlh\b|\btrader'?s little helper\b|"
    r"\bencoded\b|\bdeck\b|\bsource\b|\bkhz\b|\b\d+\s*bit\b|\b\d{2}/\d{2}\b|\bschoeps\b|\bneumann\b|\bakg\b|\bdpa\b|"
    r"\bsennheiser\b|\bnagra\b|\bzoom\b|\btascam\b|\bsound devices\b|\bmixpre\b|\bsony\b|\bedited\b|"
    r"\bmastered\b|\bdithered\b|\bdeclicked\b|\bcapture[d]?\b|\bdigiti[sz]ed\b)", re.I)
_CHAIN_TERMS = re.compile(_CHAIN.pattern.replace("(>|->|", "(", 1), re.I)
_LABEL = re.compile(r"^\s*(caveat|caveats|note|notes|comment|comments|nb|n\.b\.|ps|p\.s\.|warning|"
                    r"disclaimer|please note|fyi|remark|remarks|lineup|line-up|personnel|setlist)\b", re.I)
_COMMENTARY = re.compile(
    r"\b(said|says|told|claimed|apparently|reportedly|first performance|first time|performance|"
    r"performed|banter|crowd|audience member|rough edges|tuning|guest|sat in|joined|show|concert|"
    r"band played|they played|played|encore|opening|opener|setlist)\b", re.I)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|;\s+")


_LINEAGE_STEP_MAX = 60
_LINEAGE_STEPS_MAX = 12
_TAPER = re.compile(r"^[Tt]aper:\s*[A-Z][\w.'\u2019-]*(\s+[A-Za-z][\w.'\u2019-]*){0,3}$")
_REPORTING = re.compile(
    r"\b(per|said|says|say|refers?|referring|notes?|noted|according|uploaders?|unconfirmed|actually|"
    r"however|because|which|that|this|these|those|separate|probably|likely|maybe|perhaps|appears?|"
    r"believed|claimed?|stated|mentions?|mentioned|listed|reported(ly)?|apparently|same|also|but|"
    r"was|were|is|are|has|have|had|not|show|shows|concert|performance|broadcast of|the file|info file)\b", re.I)
_DATEISH = re.compile(
    r"\b(1[89]|20)\d{2}\b|\b(january|february|march|april|june|july|august|september|october|november|"
    r"december)\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s*\d{1,2}\b|"
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
_PLACEISH = re.compile(r"\b(hall|theat(er|re)|club|arena|stadium|auditorium|ballroom|venue|city|town|"
                       r"festival|radio station)\b", re.I)
_MODEL = re.compile(r"\b[A-Za-z]{1,6}-?\d{1,4}[A-Za-z]{0,2}\b")
_GEAR = re.compile(
    r"\b(aud|audience|sbd|soundboard|fm|eac|cdr|cd-r|cdda|dat|lossless|vinyl|lp|turntable|pcm|sony|tascam|"
    r"nakamichi|marantz|denon|panasonic|teac|jvc|roland|edirol|fostex|rode|audio-technica|sonosax|"
    r"mono|stereo)\b", re.I)


def _lineage_atom_ok(atom):
    if not atom or len(atom) > _LINEAGE_STEP_MAX:
        return False
    if _TAPER.match(atom):
        return True
    if re.search(r"[!?;]|\.\s|\.$|:", atom):
        return False
    if _REPORTING.search(atom) or _DATEISH.search(atom) or _PLACEISH.search(atom):
        return False
    if len(atom.split()) > 8:
        return False
    return bool(_CHAIN_TERMS.search(atom) or _GEAR.search(atom) or _MODEL.search(atom))


def clean_lineage(value):
    """The recording chain as steps joined by ' > ', or '' when no valid step remains. `value` is a list
    of steps (what the model submits) or a string (an older value, or a model that sent one); a step may
    itself hold "A > B". A step survives only if it is short, a noun phrase for a source, gear, transfer
    or format with no reporting words, dates, venues or sentences; "Taper: Name" is the one credit
    allowed, as its own step. Everything else (discrepancies, explanations) is dropped."""
    atoms = []
    if isinstance(value, (list, tuple)):
        chunks = [str(v or "") for v in value]
    else:
        chunks = []
        for seg in _SPLIT.split(str(value or "")):
            seg = seg.strip()
            if seg and not _LABEL.match(seg):
                chunks.append(seg)
    for chunk in chunks:
        for atom in re.split(r"\s*(?:->|>|\u2192)\s*", chunk):
            atom = " ".join(atom.split()).strip(" ,;").rstrip(".")
            if _lineage_atom_ok(atom) and (not atoms or atoms[-1].lower() != atom.lower()):
                atoms.append(atom)
    return " > ".join(atoms[:_LINEAGE_STEPS_MAX])


# ── An answer to a question nobody asked ─────────────────────────────────────

_ABSENT_Q = re.compile(
    r"\bno\s+(specific\s+|particular\s+)?questions?\s+(was|were|has\s+been|had\s+been)\s+(asked|provided|given|posed|attached)\b"
    r"|\bno\s+(specific|particular)\s+questions?\b"
    r"|\byou\s+(did\s*n[o']?t|did\s+not)\s+(ask|pose|provide)\b"
    r"|\bwithout\s+a\s+(specific\s+|particular\s+)?question\b"
    r"|\bsince\s+no\s+(specific\s+|particular\s+)?questions?\b"
    r"|\b(question|questions)\s+(was|were)\s+(not|n't)\s+(asked|provided|given|posed)\b",
    re.I)
_SENT = re.compile(r"(?<=[.!?])\s+")


def drop_absent_question_sentences(text):
    """Remove every sentence that refers to the absence of a question ("No question was asked;
    this run focuses on ..."). The rest of the text is untouched."""
    if not text:
        return text
    paras = []
    for para in str(text).split("\n"):
        kept = [s for s in _SENT.split(para) if s and not _ABSENT_Q.search(s)]
        paras.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(paras)).strip()
