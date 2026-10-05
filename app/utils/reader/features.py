"""
Units and named features for the role decoder (Resolver v2, chunk 3, 2026-10-03).

build_doc(text, library, ...) turns info text into a Doc: lines classified, split
into units (the decoder's segments), each unit carrying its position, and the
named features that weights.py scores. Pure: no I/O, no app context; the
library arrives as a LibraryIndex.

A unit is one of
    seg        a piece of a line (see segment.py), the usual case
    date       one date mention (reader/dates.py), carved out of its line
    track      a numbered track line ("01. Title"), kept whole
    titleline  one line of an unnumbered setlist, kept whole
    boiler     checksums, filenames, EAC log lines, links, rulers of text
    sethdr     "Set 1", "Encore", "Disc 2", "Setlist:"

Features are listed by name in weights.py; a feature absent from a unit is zero.
"""
import re
from dataclasses import dataclass, field

from .dates import find_dates
from .library import LibraryIndex, act_core, norm_key
from .place import peel
from .segment import segment_line

# ── lexicons ─────────────────────────────────────────────────────────────────

VENUE_WORDS = {
    "theater", "theatre", "stadium", "arena", "amphitheater", "amphitheatre", "hall", "halle",
    "saal", "kursaal", "club", "studio", "studios", "pavilion", "auditorium", "center",
    "centre", "ballroom", "opera", "university", "college", "fillmore", "ryman", "birchmere",
    "inn", "coffeehouse", "tent", "café", "cafe", "lounge", "saloon", "fairground",
    "fairgrounds", "garden", "gardens", "park", "ranch", "farm", "museum", "coliseum",
    "field", "court", "bowl", "forum", "palace", "pier", "warehouse", "dome", "barn",
    "odeon", "kurhaus", "hallenstadion", "concertgebouw", "playhouse", "room", "tavern",
    "pub", "church", "cathedral", "school", "library", "hotel", "casino", "plaza",
    "academy", "conservatory", "symphony", "philharmonic", "tonhalle", "palais", "teatro",
    "zenith", "olympia", "troubadour", "whisky", "bluebird", "roxy", "paradise",
    "radio", "gymnasium", "gym", "armory", "auditorio", "konzerthaus", "koncerthuset",
    "kongresshalle", "kongresshaus", "schauspielhaus", "stadthalle", "festhalle", "house",
    "ballpark", "speedway", "racetrack", "lodge", "resort", "campground", "grounds",
    "salle", "sala", "kino", "haus", "casa", "teatr", "theatro", "tonhalle", "arena",
}
_EVENT_TOKENS = {"series", "jamboree", "rendezvous", "gathering", "convention", "fair",
                 "jubilee", "festspiele", "festival", "festivals", "fest", "carnival"}
_EVENT_SUFFIX = re.compile(r"[a-z]{3,}(?:fest|festival|woche|wochen|tage)$")
_TITLE_TOKENS = {"tour", "presents", "presenting", "unplugged", "benefit", "tribute", "live",
                 "anniversary", "songbook"}
_TITLE_PHRASES = re.compile(r"\bin concert\b|\bworld tour\b|\bcomplete show\b", re.I)
_RECORDING_TOKENS = {"recorded", "recording", "taped", "transferred", "transfer", "seeded",
                     "seed", "uploaded", "upload", "extraction", "extracted", "mastered",
                     "converted", "encoded", "ripped", "burned", "digitized", "digitised",
                     "edited", "remastered", "flac", "shn", "wav", "eac", "cdwave", "torrent",
                     "etree", "dime", "dimeadozen", "khz", "tracked", "tracking"}
_EQUIP_TOKENS = {"schoeps", "akg", "sennheiser", "neumann", "sony", "nakamichi", "nak",
                 "tascam", "panasonic", "marantz", "denon", "lunatec", "oade", "dpa",
                 "minidisc", "dat", "cdr", "cassette", "reel", "nbob", "cmc", "mk4", "usb",
                 "firewire", "soundforge", "wavelab", "audacity", "cooledit", "protools",
                 "mic", "mics", "microphones", "preamp", "recorder", "deck", "fob", "sbm",
                 "foobar", "tlh", "zoom", "edirol", "fostex", "rode", "behringer", "sound",
                 "devices", "grace", "apogee", "lynx", "audiophile", "montego"}
_EQUIP_MODEL = re.compile(r"\b[A-Za-z]{1,4}-?\d{2,4}[A-Za-z]?\b")
_NOTES_TOKENS = {"thanks", "thank", "please", "enjoy", "share", "note", "notes", "comment",
                 "comments", "quality", "rating", "excellent", "incomplete", "complete",
                 "missing", "cuts", "cut", "gap", "dropout", "fade", "applause", "crowd",
                 "unknown", "good", "poor", "fair", "great", "decent", "hiss", "distortion",
                 "tape", "flip", "complete", "partial", "edit", "edits", "setlist", "intro"}
_FUNCTION = {"the", "and", "of", "this", "is", "was", "a", "to", "from", "in", "for", "by",
             "on", "it", "with", "at", "are", "were", "be", "has", "have", "an", "not", "but"}
_INSTR = {"p", "b", "g", "d", "dr", "dm", "v", "vo", "vox", "voc", "tp", "tb", "ts", "as", "ss",
          "bs", "cl", "fl", "vn", "vln", "vc", "vib", "org", "key", "keys", "perc", "git", "gtr",
          "bass", "drums", "drum", "piano", "guitar", "vocals", "vocal", "sax", "trumpet",
          "trombone", "banjo", "fiddle", "mandolin", "dobro", "violin", "cello", "flute",
          "clarinet", "harmonica", "harp", "accordion", "synth", "synthesizer", "congas",
          "percussion", "saxophone", "tenor", "alto", "soprano", "baritone", "voice", "lead",
          "rhythm", "acoustic", "electric", "keyboards", "keyboard", "tuba", "horn", "cornet",
          "steel", "pedal", "uke", "ukulele", "bouzouki", "oud", "tabla", "sitar", "dobro",
          "mando", "gt", "el", "ac", "tpt", "tbn", "sop", "fh", "bj", "mdn", "fdl", "dbr", "vl"}
_CONNECTORS = {"with", "featuring", "feat", "ft", "w", "plus", "and", "&", "+", "joined by",
               "special guest", "special guests", "guest", "guests", "and friends",
               "with special guests", "and his band", "and her band", "and the band"}
_PARTICLES = {"de", "di", "da", "van", "von", "der", "den", "la", "le", "del", "della", "dos",
              "du", "bin", "al", "el", "y", "ten", "ter", "st", "mac"}

_SET_LABEL_RE = re.compile(
    r"^[\W_]*(?:(?P<kind>set|disc|disk|cd|tape)\s*(?:#|no\.?)?\s*(?P<n>\d{1,2}|[ivx]{1,4}|one|two|three|four|five|six)\b"
    r"|(?P<ord>1st|2nd|3rd|first|second|third|fourth|opening|early|late)\s+(?:set|show)\b"
    r"|(?P<enc>encores?)\b)", re.I)
_SETHDR_RE = re.compile(
    r"^[\W_]*(?:(?:set|disc|disk|cd|tape|part|show)\s*(?:#|no\.?)?\s*(?:\d{1,2}|[ivx]{1,4}|one|two|three|four|five|six)"
    r"|(?:1st|2nd|3rd|first|second|third|fourth|opening|early|late)\s+(?:set|show)"
    r"|encores?"
    r"|set\s*list|setlist|track\s*list|tracklist|track\s*listing|songs?|songlist|song\s*list|program(?:me)?)"
    r"\b[\W_]*(?:\(?[\d:]+\)?)?[\W_]*(?:\([^)]*\))?[\W_]*$", re.I)
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3,
             "opening": 1, "early": 1, "late": 2}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}

_TRACK_LINE_RE = re.compile(r"^\s*[\(\[]?(\d{1,3})[\)\]]?(?:[.:\-\s]\s*|(?<=[\)\]])\s*)(.+)$")
_BARE_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_TRAILING_TS_RE = re.compile(r"\s+\d*:[\d:]+$|\s*\(\d{1,2}:\d{2}(?::\d{2})?\)\s*$")
_BOILER_RE = re.compile(
    r"[0-9a-f]{16,}|\.(?:flac|shn|wav|ffp|md5|st5|mp3|aif|aiff|ape|wv)\b|"
    r"exact audio copy|\beac\b.*\b(?:log|extraction|version)|extraction logfile|track peak|"
    r"test crc|copy crc|used drive|read mode|utilize accurate|gap handling|"
    r"https?://|www\.|\bftp://|^\s*(?:ffp|md5|st5)\s*:|"
    r"please (?:do not|don't|support|share|seed)|do not (?:convert|transcode)|no mp3|"
    r"^\s*[\w.+-]+@[\w-]+\.[\w.]+\s*$", re.I)
_RULER_RE = re.compile(r"^[\W_]{3,}$")
_QUOTE_RE = re.compile(r"[\"“„]([^\"”“]{4,70})[\"”]")
_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z /&'\-]{1,32}:\s*$")
_LEAD_CONNECTOR_RE = re.compile(r"^(?:(w/)\s*|(with|featuring|feat\.?|plus)\s+)(?=\S)", re.I)
_MID_CONNECTOR_RE = re.compile(r"\s+(with|featuring|feat\.?|ft\.?)\s+(?=\S)", re.I)
_LEAD_AND_RE = re.compile(r"^(and|&)\s+(?=[A-Z])", re.I)
_PAREN_TAIL_RE = re.compile(r"\s*[\(\[]([^()\[\]]{1,30})[\)\]]\s*$")
_HYPHEN_INSTR_RE = re.compile(r"(?<=[A-Za-z])\s?[-–]\s?([A-Za-z]{2,12})\s*$")
_WEEKDAY_BEFORE = re.compile(
    r"\b(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs?)?|Fri|Sat(?:ur)?|Sun)(?:day)?\b\.?[\s,]*$", re.I)

_LABELS = {
    "venue": {"venue", "where", "site", "hall", "theatre", "theater", "concert hall"},
    "location": {"location", "loc"},
    "place": {"city", "town", "country", "city/state", "place", "city state"},
    "date": {"date", "when", "performance date", "show date"},
    "artist": {"artist", "band", "performer", "group", "act", "artists"},
    "source": {"source", "src", "recording type", "type", "source type", "recorded from"},
    "lineage": {"lineage", "transfer", "transferred", "chain", "recording info", "recorded by",
                "taper", "taped by", "recorder", "conversion", "converted", "mastered",
                "mastering", "remaster", "remastered", "encoding", "encoded", "edited",
                "edited by", "transfer info", "digitized", "transferred by", "seeded by",
                "uploaded by", "recording", "taper info", "tracking", "tracked by", "flac"},
    "equipment": {"equipment", "gear", "mics", "microphones", "mic", "config", "setup",
                  "recording equipment", "microphone", "rig", "mic setup"},
    "notes": {"notes", "note", "comments", "comment", "info", "show notes", "description",
              "quality", "rating", "grade", "sound quality", "taper notes", "recording notes",
              "misc", "extra", "bonus", "remarks", "review", "details"},
    "personnel": {"personnel", "lineup", "line-up", "line up", "musicians", "band members",
                  "players", "performers", "members", "featuring", "with"},
    "event": {"event", "festival"},
}
_LABEL_KIND = {w: k for k, ws in _LABELS.items() for w in ws}
_SECTION_FOR_HEADING = {"personnel": "personnel", "lineage": "lineage", "equipment": "lineage",
                        "source": "lineage", "notes": "notes", "location": "lineage"}
_SOURCE_KW = re.compile(r"(?<![a-z])[a-z]?(?:sbd|soundboard|sound board|aud|audience|mtx|matrix|fm|broadcast)(?![a-z])", re.I)


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class Unit:
    text: str
    start: int
    end: int
    line: int                      # physical line index
    nb: int                        # non-blank line ordinal
    block: int
    kind: str = "seg"
    sep_before: str = ""
    hard_before: bool = False
    label: str | None = None
    pos: int = 0                   # unit index within its line
    n_in_line: int = 1
    mention: object = None         # DateMention for kind "date"
    set_label: str | None = None   # for sethdr
    feats: dict = field(default_factory=dict)

    @property
    def span(self):
        return (self.start, self.end)


@dataclass
class Line:
    idx: int
    nb: int
    block: int
    text: str
    offset: int
    kind: str = "seg"
    units: list = field(default_factory=list)
    place: object = None           # PlaceResult or None
    place_span: tuple = (0, 0)     # relative to the line
    has_date: bool = False
    section: str = ""


@dataclass
class Doc:
    text: str
    lines: list
    units: list
    n_audio: int | None = None
    track_start: int | None = None      # nb of the first track-like line
    title_runs: list = field(default_factory=list)


# ── small helpers ────────────────────────────────────────────────────────────

def _tokens(text):
    return re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?|\d+", text.lower())


def _words(text):
    return re.findall(r"\S+", text)


def _strip_wrap(text):
    t = text.strip().strip("\"'“”‘’*_~[]()").strip()
    return t


def parse_set_label(text):
    """Canonical label for a set header ("Set 2", "Encore", "Disc 1"), or None
    for a plain "Setlist:" heading."""
    m = _SET_LABEL_RE.match(text or "")
    if not m:
        return None
    if m.group("enc"):
        return "Encore"
    if m.group("kind"):
        k = m.group("kind").lower()
        n = m.group("n").lower()
        num = int(n) if n.isdigit() else (_ROMAN.get(n) or _WORD_NUM.get(n))
        if not num:
            return None
        return ("Disc" if k in ("disc", "disk", "cd", "tape") else "Set") + f" {num}"
    if m.group("ord"):
        num = _WORD_NUM.get(m.group("ord").lower())
        return f"Set {num}" if num else None
    return None


def is_track_noise(title):
    low = title.lower()
    if ".flac" in low or re.search(r"\bflac\b|\bkhz\b", low):
        return True
    if re.match(r"^[\da-f]{8,}", low):
        return True
    if re.match(r"^\d{1,2}[-./]\d{4}$", title):
        return True
    if re.match(r"^\d{1,2}[-./]\d{1,2}", title) and len(title) < 15:
        return True
    if _BARE_DURATION_RE.match(title):
        return True
    return False


def person_shaped(text):
    """Two to four name-like tokens: capitalised (or ALL CAPS, or a particle), no digits,
    no venue, event, title or notes words."""
    t = _PAREN_TAIL_RE.sub("", text).strip().strip(",;")
    t = re.sub(r"\s*[-–]\s*[A-Za-z]{2,12}$", lambda m: "" if m.group(0).strip(" -–").lower() in _INSTR else m.group(0), t)
    if not t or re.search(r"\d|[&@/:;]", t):
        return False
    ws = t.split()
    if not 2 <= len(ws) <= 4:
        return False
    caps = 0
    for i, w in enumerate(ws):
        core = w.strip(".,'’\"")
        lc = core.lower()
        if lc in _PARTICLES and 0 < i < len(ws) - 1:
            continue
        if not core or not (core[0].isupper()):
            return False
        caps += 1
    if caps < 2:
        return False
    toks = set(_tokens(t))
    if toks & (VENUE_WORDS | _EVENT_TOKENS | _TITLE_TOKENS | _NOTES_TOKENS | _FUNCTION - {"y", "a"}):
        return False
    return True


def instrument_annotation(text):
    """True for a name carrying an instrument: "Bill Evans (p)", "Eddie Gomez - bass",
    "Oscar Peterson-piano"."""
    m = _PAREN_TAIL_RE.search(text)
    if m:
        toks = _tokens(m.group(1))
        if toks and all(t in _INSTR for t in toks):
            return True
    m = _HYPHEN_INSTR_RE.search(text)
    if m and m.group(1).lower() in _INSTR and len(m.group(1)) >= 3:
        return True
    return False


def _instrument_only(text):
    toks = _tokens(text)
    return bool(toks) and len(toks) <= 3 and all(t in _INSTR for t in toks)


def _case_stats(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False, False, False
    upper = all(c.isupper() for c in letters) and len(letters) >= 3
    lower = all(c.islower() for c in letters)
    ws = [w for w in re.findall(r"[^\W\d_][\w'’.\-]*", text) if w.lower() not in _FUNCTION | {"de", "la", "le"}]
    cap = sum(1 for w in ws if w[0].isupper())
    title = bool(ws) and cap / len(ws) >= 0.7
    return upper, lower, title


def _is_boiler(line):
    return bool(_BOILER_RE.search(line))


# ── building the doc ─────────────────────────────────────────────────────────

def _carve(line, spans):
    """Split `line` at the carved spans [(a, b, kind)], returning pieces
    [(a, b, kind_or_None)] covering the line in order."""
    pieces, pos = [], 0
    for a, b, kind in sorted(spans):
        if a < pos:
            continue
        if a > pos:
            pieces.append((pos, a, None))
        pieces.append((a, b, kind))
        pos = b
    if pos < len(line):
        pieces.append((pos, len(line), None))
    return pieces


_AT_RE = re.compile(r"\s+(?:live\s+)?at\s+(?:the\s+)?(?=[A-Z])")
_LEADIN_RE = re.compile(r"^\s*(?:recorded\s+live\s+at|recorded\s+at|live\s+at|live\s+on|at(?=\s+the\s)|[A-Z]{2}\s+--)\s+(?=\S)", re.I)
_TAIL_PAREN_RE = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")


_CITY_VENUEISH = None
_CITY_SPLIT_RE = re.compile(r"\s*(?:--+|\s[-\u2013\u2014]\s|@)\s*")


def _trim_place_span(raw, pr):
    """The peeler takes any text before a region as the city, venue name and all
    ("Cellar Door Club, Washington", "Great American Music Hall--San Francisco, CA").
    Shrink the place span to the city proper and leave the rest as ordinary text."""
    a, b = pr.span
    city = (pr.city or "").strip()
    if not city:
        return a, b
    idx = raw.find(city, a)
    if idx < 0 or idx > a + 2:
        return a, b
    from .place import _city_index, _norm_city
    if _norm_city(city) in _city_index()[2]:
        return a, b                 # a real gazetteer city ("State College"), whatever its words
    ms = list(_CITY_SPLIT_RE.finditer(city))
    if ms:
        m = ms[-1]
        pr.city = city[m.end():].strip()
        return idx + m.end(), b
    toks = {w.lower().strip(".") for w in re.findall(r"[^\W\d_]+\.?", city)}
    if toks & (VENUE_WORDS | {"club", "university", "college", "park", "museum", "center", "centre"}):
        rest = raw[idx + len(city):b]
        cm = re.match(r"\s*,\s*", rest)
        if cm:
            pr.city = ""
            return idx + len(city) + cm.end(), b
        if toks & {"university", "college", "club", "museum"}:
            return 0, 0
    return a, b


_BUILDING_WORDS = {"hall", "halle", "haus", "theatre", "theater", "centre", "center", "arena", "grounds", "ground"}


def is_festival_like(text):
    """True when a festival token names an event, venue words in the segment or not.
    A festival token directly before a building word is part of a building's name
    ("Royal Festival Hall"); Festhalle, Festspielhaus and Festspiele are halls."""
    toks = _tokens(text)
    for i, w in enumerate(toks):
        if w in ("festhalle", "festspielhaus", "festspielhalle", "festspiele"):
            continue
        if w in _EVENT_TOKENS or _EVENT_SUFFIX.match(w):
            if i + 1 < len(toks) and toks[i + 1] in _BUILDING_WORDS:
                continue
            return True
    return False


# What an Event is (Ryan, 2026-10-05): a collection of performances comprising a festival
# or other single ticketed-or-free event. Its purpose is the historical context of the
# night: where, with whom, what the experience was. A tour ("Fall Tour 2019"), a
# residency or a billing note ("Opened for The Strokes") is not an event.
_NOT_EVENT_RE = re.compile(r"\btours?\b|\bresidency\b|\bopen(?:ed|ing|er|s)?\s+for\b"
                           r"|\bsupporting\b|\bsupport\s+(?:for|slot|act)\b", re.I)
_EVENT_KINDS = {"festival", "convention/expo"}     # MusicBrainz event types that are events


def counts_as_event(name, kind=None):
    """The one test for "is this an Event", for Atlas events (with their MusicBrainz
    `kind`) and for text alike. MusicBrainz "concert" is mostly single shows and tours
    ("The Strokes at Glasgow Barrowland"), so it counts only when the name itself reads
    like a festival."""
    if not name or _NOT_EVENT_RE.search(name):
        return False
    return (kind or "").lower() in _EVENT_KINDS or is_festival_like(name)


def _mk_units(line_obj, library):
    """Units of a normal line: dates, quotes, a trailing parenthetical and the
    place carved out first, then each remaining piece segmented."""
    raw = line_obj.text
    spans = []
    mentions = find_dates(raw)
    for m in mentions:
        a, b = m.span
        wd = _WEEKDAY_BEFORE.search(raw[:a])
        if wd and re.search(r"\b(?:mr|mrs|ms|dr|st)\.?\s*$", raw[:wd.start()], re.I):
            wd = None               # "Mr. Sun 2023-04-20": a name, not a weekday
        if wd:
            a = wd.start()
        spans.append((a, b, ("date", m)))
    line_obj.has_date = bool(mentions)
    # a trailing "(FM reseed w/ mp3-sample)" is a remark, not part of the place
    peel_text = raw
    tm = _TAIL_PAREN_RE.search(raw)
    if tm and tm.start() > 0 and len(_words(tm.group(1))) >= 1 and not any(tm.start(1) - 1 < sb and tm.end() > sa for sa, sb, _ in spans):
        spans.append((tm.start(), len(raw.rstrip()), ("paren", None)))
        peel_text = raw[:tm.start()] + " " * (len(raw) - tm.start())
    pr = peel(peel_text, city_only=True)
    if pr.recognised and pr.span != (0, 0):
        a, b = _trim_place_span(raw, pr)
        if b <= a:
            pr = None
            a = b = 0
        if pr is not None and not any(a < sb and b > sa for sa, sb, _ in spans):
            spans.append((a, b, ("place", pr)))
            line_obj.place = pr
            line_obj.place_span = (a, b)
    for qm in _QUOTE_RE.finditer(raw):
        a, b = qm.span()
        if not any(a < sb and b > sa for sa, sb, _ in spans):
            spans.append((a, b, ("quote", None)))

    units = []
    label = None
    base = 0
    lm = re.match(r"^\s*([A-Za-z][A-Za-z ]{0,24}):[ \t]+(?=\S)", raw)
    if lm and not any(a < lm.end() for a, _, _ in spans):
        if lm.group(1).strip().lower() in _LABEL_KIND:
            label = lm.group(1).strip()
            base = lm.end()
        else:
            # "Lafayette Square: Buffalo, NY": not a label, a separator
            spans.append((lm.end(1), lm.end(), ("sep", None)))
    leadin = _LEADIN_RE.match(raw[base:]) if base == 0 else None
    if leadin and not any(a < base + leadin.end() for a, _, _ in spans):
        spans.append((0, leadin.end(), ("sep", None)))
    nwords = len(_words(raw))
    for a, b, kind in _carve(raw, [(a, b, k) for a, b, k in spans if a >= base]):
        if a < base:
            continue
        if kind is not None and kind[0] == "sep":
            continue
        if kind is None or kind[0] in ("place", "paren"):
            pieces = [(a, b, False)]
            if kind is None and line_obj.nb <= 3 and nwords <= 9:
                am = _AT_RE.search(raw, a, b)
                if am and am.start() > a and len(_words(raw[a:am.start()])) <= 5 \
                        and raw[a:am.start()].strip()[:1].isupper():
                    pieces = [(a, am.start(), False), (am.end(), b, True)]
            for pa, pb, after_at in pieces:
                segs = segment_line(raw[pa:pb], line_obj.idx, line_obj.block,
                                    line_obj.offset + pa, labels=False)
                for si, sg in enumerate(segs):
                    u = Unit(sg.text, sg.start, sg.end, line_obj.idx, line_obj.nb, line_obj.block, "seg",
                             sg.sep_before, sg.hard_before, None)
                    if kind is not None:
                        u.feats["in_place" if kind[0] == "place" else "paren_tail"] = 1.0
                    if after_at and si == 0:
                        u.feats["after_at"] = 1.0
                    if si == 0 and a > 0 and pa == a and kind is None:
                        u.hard_before = True
                    units.append(u)
        elif kind[0] == "date":
            txt = raw[a:b].strip()
            u = Unit(txt, line_obj.offset + a, line_obj.offset + b, line_obj.idx, line_obj.nb,
                     line_obj.block, "date", mention=kind[1])
            units.append(u)
        else:  # quote
            txt = raw[a:b].strip()
            u = Unit(txt, line_obj.offset + a, line_obj.offset + b, line_obj.idx, line_obj.nb,
                     line_obj.block, "seg")
            u.feats["quoted"] = 1.0
            units.append(u)
    # drop pieces with no letters or digits (leftover punctuation)
    units = [u for u in units if re.search(r"\w", u.text)]
    if label is not None:
        for u in units:
            u.label = label
    # split a leading connector off a segment: "with Pat Metheny" -> "with" + "Pat Metheny";
    # and a "with" or "feat." inside a billing line: "Kentucky Colonels with The Spencers"
    out = []
    for u in units:
        if u.kind == "seg" and u.nb <= 3 and "in_place" not in u.feats and "quoted" not in u.feats \
                and "paren_tail" not in u.feats:
            mm = _MID_CONNECTOR_RE.search(u.text)
            if mm and u.text[:mm.start()].strip() and u.text[mm.end():].strip():
                left = Unit(u.text[:mm.start()].strip(), u.start, u.start + len(u.text[:mm.start()].rstrip()),
                            u.line, u.nb, u.block, "seg", u.sep_before, u.hard_before, u.label)
                cw = mm.group(1)
                cs = u.start + mm.start(1)
                conn = Unit(cw, cs, cs + len(cw), u.line, u.nb, u.block, "seg", "", False, None)
                conn.feats["connector_word"] = 1.0
                rt = u.text[mm.end():].strip()
                right = Unit(rt, u.end - len(rt), u.end, u.line, u.nb, u.block, "seg", "", False, None)
                out.extend([left, conn, right])
                continue
        if u.kind == "seg" and "in_place" not in u.feats and "quoted" not in u.feats:
            m = _LEAD_CONNECTOR_RE.match(u.text)
            m2 = None if m else _LEAD_AND_RE.match(u.text)
            m = m or m2
            if m and len(u.text) > m.end() + 1:
                cw = u.text[:m.end()].strip()
                head = Unit(cw, u.start, u.start + len(cw), u.line, u.nb, u.block, "seg",
                            u.sep_before, u.hard_before, u.label)
                head.feats["connector_word"] = 1.0
                rest_txt = u.text[m.end():].strip()
                rest = Unit(rest_txt, u.end - len(rest_txt), u.end, u.line, u.nb, u.block, "seg",
                            "", False, None)
                out.extend([head, rest])
                continue
        out.append(u)
    return out, label


def build_doc(text, library=None, n_audio=None, hints=None, atlas=None):
    library = library or LibraryIndex.empty()
    hints = hints or {}
    text = re.sub(r"\r(?!\n)", "\n", text or "")      # old-Mac line ends; same length, spans stay valid
    lines = []
    off = 0
    nb = -1
    block = 0
    in_blank = False
    header_count = 0       # non-track, non-blank lines seen (Pass-1 rule for "is this a track")
    in_tracks = False
    track_start = None
    for i, raw in enumerate((text or "").split("\n")):
        ln = raw.rstrip("\r")
        start_off = off
        off += len(raw) + 1
        s = ln.strip()
        if not s:
            if not in_blank and lines:
                block += 1
            in_blank = True
            continue
        in_blank = False
        if _RULER_RE.match(s):
            continue
        nb += 1
        L = Line(i, nb, block, ln, start_off)
        lead = len(ln) - len(ln.lstrip())
        if _is_boiler(s):
            L.kind = "boiler"
        elif _SETHDR_RE.match(s) and len(s) < 60:
            L.kind = "sethdr"
        elif _HEADING_RE.match(s):
            L.kind = "heading"
        else:
            m = _TRACK_LINE_RE.match(s)
            if m and not _BARE_DURATION_RE.match(s):
                title = _TRAILING_TS_RE.sub("", m.group(2).strip())
                if not is_track_noise(title) and (in_tracks or header_count >= 2):
                    L.kind = "track"
                    in_tracks = True
                    if track_start is None:
                        track_start = nb
        if L.kind not in ("track",):
            header_count += 1
        lines.append(L)

    # a lone numbered line ("01. nov 2001") is not a track list: it needs a numbered
    # neighbour (or a set header above it), or a folder with a single audio file
    kinds = {L.nb: L.kind for L in lines}
    for L in lines:
        if L.kind != "track":
            continue
        ok = (n_audio == 1 or kinds.get(L.nb - 1) in ("track", "sethdr") or kinds.get(L.nb + 1) == "track"
              or kinds.get(L.nb + 1) == "sethdr")
        if ok:
            title = _TRAILING_TS_RE.sub("", _TRACK_LINE_RE.match(L.text.strip()).group(2).strip())
            dm = find_dates(title)
            if dm and sum(m.span[1] - m.span[0] for m in dm) >= 0.6 * len(title):
                ok = False
        if not ok:
            L.kind = "seg"
    track_start = None
    for L in lines:
        if L.kind == "track":
            track_start = L.nb
            break

    # sections: headings set a section until the next blank line or heading
    sec = ""
    prev_block = None
    for L in lines:
        if L.block != prev_block:
            sec = ""
            prev_block = L.block
        if L.kind == "heading":
            w = norm_key(L.text.rstrip(": ")).replace(" ", "")
            hk = _LABEL_KIND.get(L.text.rstrip(": ").strip().lower())
            sec = _SECTION_FOR_HEADING.get(hk, "")
            L.section = "heading"
            L.units = [Unit(L.text.strip(), L.offset, L.offset + len(L.text), L.idx, L.nb, L.block, "seg")]
            L.units[0].feats["heading"] = 1.0
            if sec:
                L.units[0].feats["heading_" + sec] = 1.0
            continue
        L.section = sec

    # title runs: lines of an unnumbered setlist
    cand = []
    for L in lines:
        ok = False
        if L.kind == "seg":
            s = L.text.strip()
            w = _words(s)
            ok = (1 <= len(w) <= 9 and len(s) <= 70 and not s.endswith(":")
                  and ">" not in s and not find_dates(s)
                  and not re.match(r"^\s*[A-Za-z][A-Za-z ]{0,24}:[ \t]+\S", s)
                  and not re.search(r"\d{1,2}:\d{2}", re.sub(r"(?:\s+\d*:[\d:]+|\s*\(\d{1,2}:\d{2}(?::\d{2})?\))\s*$", "", s))
                  and not _SOURCE_KW.search(s) and not _BOILER_RE.search(s))
            if ok:
                pr = peel(s, city_only=False)
                if pr.has_region_or_country:
                    ok = False
        cand.append(ok)
    runs = []   # [(first idx in lines, last idx, after_sethdr)]
    i = 0
    while i < len(lines):
        if cand[i]:
            j = i
            while j + 1 < len(lines) and cand[j + 1] and lines[j + 1].block == lines[i].block:
                j += 1
            after_hdr = i > 0 and lines[i - 1].kind in ("sethdr", "heading") and (
                lines[i - 1].kind == "sethdr" or _LABEL_KIND.get(lines[i - 1].text.rstrip(": ").strip().lower()) in (None, "notes"))
            n = j - i + 1
            first_block = lines[i].block == 0
            if (after_hdr and n >= 2) or (not first_block and n >= 5) or (first_block and not after_hdr and n >= 8):
                runs.append((i, j, after_hdr))
            i = j + 1
        else:
            i += 1
    total = sum(j - i + 1 for i, j, _ in runs)
    for (i, j, after_hdr) in runs:
        for k in range(i, j + 1):
            L = lines[k]
            L.kind = "titleline"
            s = L.text.strip()
            lead = len(L.text) - len(L.text.lstrip())
            u = Unit(s, L.offset + lead, L.offset + lead + len(s), L.idx, L.nb, L.block, "titleline")
            u.feats["in_title_run"] = 1.0
            if after_hdr:
                u.feats["after_set_header"] = 1.0
            if n_audio and total == n_audio:
                u.feats["run_matches_audio"] = 1.0
            L.units = [u]
        if lines[i].nb is not None and (track_start is None or lines[i].nb < track_start):
            track_start = lines[i].nb

    for L in lines:
        if L.kind in ("boiler", "track", "sethdr") and not L.units:
            s = L.text.strip()
            lead = len(L.text) - len(L.text.lstrip())
            u = Unit(s, L.offset + lead, L.offset + lead + len(s), L.idx, L.nb, L.block, L.kind)
            if L.kind == "sethdr":
                u.set_label = parse_set_label(s)
            L.units = [u]

    # remaining normal lines -> units
    for L in lines:
        if L.kind == "seg" and not L.units:
            units, label = _mk_units(L, library)
            L.units = units
    units = []
    for L in lines:
        n = len(L.units)
        for p, u in enumerate(L.units):
            u.pos, u.n_in_line = p, n
        units.extend(L.units)
    doc = Doc(text or "", lines, units, n_audio, track_start, runs)
    _features(doc, library, hints, atlas)
    return doc


# ── features ─────────────────────────────────────────────────────────────────

def _unit_features(u, L, doc, library, hints, idx):
    f = u.feats
    t = u.text
    # position
    nbn = u.nb
    if nbn == 0:
        f["line0"] = 1.0
    elif nbn == 1:
        f["line1"] = 1.0
    elif nbn <= 3:
        f["line2_3"] = 1.0
    elif nbn <= 8:
        f["line4_8"] = 1.0
    else:
        f["line_late"] = 1.0
    if u.block > 0:
        f["block_later"] = 1.0
    if u.pos == 0:
        f["first_in_line"] = 1.0
    elif u.hard_before:
        f["hard_after_first"] = 1.0
    else:
        f["soft_after_first"] = 1.0
        if nbn == 0:
            f["soft_line0"] = 1.0
    if doc.track_start is not None and nbn > doc.track_start:
        f["after_track_start"] = 1.0
    if L.section in ("personnel", "lineage", "notes"):
        f["sec_" + L.section] = 1.0
    if u.kind == "date":
        m = u.mention
        f["date_nonshow" if m.role_hint == "transfer" else "date_show"] = 1.0
        return
    if u.kind == "boiler":
        return
    if u.kind in ("track", "sethdr"):
        f["numbered_track" if u.kind == "track" else "is_sethdr"] = 1.0
        return
    if u.kind == "titleline":
        w = len(_words(t))
        if w == 1:
            f["w1"] = 1.0
        return

    # label
    if u.label:
        k = _LABEL_KIND.get(u.label.strip().lower())
        if k:
            f["label_" + k] = 1.0
            if k in ("source", "lineage", "equipment", "notes", "date"):
                f["label_other"] = 1.0
        else:
            f["label_unknown"] = 1.0
    # shape
    ws = _words(t)
    n = len(ws)
    f["w1" if n == 1 else "w2_4" if n <= 4 else "w5_7" if n <= 7 else "w8p"] = 1.0
    upper, lower, title = _case_stats(t)
    if upper:
        f["all_caps"] = 1.0
    if lower:
        f["all_lower"] = 1.0
    if title and not upper:
        f["title_case"] = 1.0
    first_alpha = next((c for c in t if c.isalpha()), "")
    if first_alpha and first_alpha.islower():
        f["starts_lower"] = 1.0
    if re.search(r"\d", t):
        f["has_digit"] = 1.0
    if re.search(r"\d{1,2}:\d{2}", t):
        f["has_clock"] = 1.0
    if ">" in t:
        f["has_gt"] = 1.0
    toks = _tokens(t)
    tset = set(toks)
    if _NOT_EVENT_RE.search(t):
        f["not_event"] = 1.0
    elif is_festival_like(t):
        f["event_word"] = 1.0
    elif tset & VENUE_WORDS:
        f["venue_word"] = 1.0
    if toks and toks[-1] == "stage" or (len(toks) >= 2 and "stage" in toks):
        f["stage_word"] = 1.0
    if tset & _TITLE_TOKENS or _TITLE_PHRASES.search(t):
        f["title_word"] = 1.0
    if tset & _RECORDING_TOKENS:
        f["recording_verb"] = 1.0
    if tset & _EQUIP_TOKENS or _EQUIP_MODEL.search(t):
        f["equip_word"] = 1.0
    if tset & _NOTES_TOKENS:
        f["notes_word"] = 1.0
    if _SOURCE_KW.search(t):
        f["source_kw"] = 1.0
    nfn = sum(1 for x in toks if x in _FUNCTION)
    if (n >= 7 and nfn >= 3) or (n >= 5 and t.rstrip().endswith(".") and nfn >= 2):
        f["prose"] = 1.0
    tl = t.lower().strip(" .,:;")
    if tl in _CONNECTORS or u.feats.get("connector_word"):
        f["connector_word"] = 1.0
    if _instrument_only(t):
        f["instrument_word"] = 1.0
    if instrument_annotation(t):
        f["has_instr"] = 1.0
    if person_shaped(t):
        f["person_name"] = 1.0
    # place marks
    if L.place is not None and "in_place" not in f:
        a, b = L.place_span
        rs, re_ = u.start - L.offset, u.end - L.offset
        pr = L.place
        if re_ <= a:
            f["peel_left" if pr.has_region_or_country else "peel_left_city"] = 1.0
        elif rs >= b and "@" in u.sep_before + "".join(
                x.sep_before for x in L.units[:u.pos + 1] if x.start >= L.offset + b):
            f["peel_at"] = 1.0
    if "in_place" in f and L.place is not None and not L.place.has_region_or_country:
        f["in_place_city"] = 1.0
    if "in_place" in f and L.place is not None:
        a, b = L.place_span
        if (b - a) >= 0.7 * len(L.text.strip()) and L.place.has_region_or_country:
            f["line_is_place"] = 1.0
    # library
    if not library.is_empty:
        core = _strip_wrap(_PAREN_TAIL_RE.sub("", t))
        am = library.artist_match(core)
        if am:
            f["lib_artist_exact" if am[1] == "exact" else "lib_artist_core"] = 1.0
        if library.musician_match(core):
            f["lib_musician"] = 1.0
        # an older library Venue row may hold a festival name: that is event evidence
        if library.venue_match(core):
            f["lib_event" if (is_festival_like(core) or library.event_match(core)) else "lib_venue"] = 1.0
        elif n >= 2 and library.venue_names_in(core) and not is_festival_like(core):
            f["lib_venue_in"] = 1.0
        if library.event_match(core):
            f["lib_event"] = 1.0
    ha = hints.get("artist")
    if ha and act_core(_strip_wrap(t)) == act_core(ha):
        f["agree_hint_artist"] = 1.0
    hv = hints.get("venue")
    if hv and norm_key(_strip_wrap(t)) == norm_key(hv):
        f["agree_hint_venue"] = 1.0


def _features(doc, library, hints, atlas=None):
    lines_by_nb = {L.nb: L for L in doc.lines}
    for idx, u in enumerate(doc.units):
        L = lines_by_nb[u.nb]
        _unit_features(u, L, doc, library, hints, idx)
    # neighbours
    units = doc.units
    for i, u in enumerate(units):
        if u.kind != "seg":
            continue
        if i + 1 < len(units) and units[i + 1].feats.get("connector_word") and units[i + 1].kind == "seg":
            u.feats["next_is_connector"] = 1.0
        if i > 0 and units[i - 1].feats.get("connector_word") and units[i - 1].kind == "seg":
            u.feats["prev_is_connector"] = 1.0
        nxt = lines_by_nb.get(u.nb + 1)
        prv = lines_by_nb.get(u.nb - 1)
        L = lines_by_nb[u.nb]
        if u.pos == u.n_in_line - 1 and (u.n_in_line == 1 or u.hard_before) and nxt is not None and nxt.place is not None \
                and nxt.place.has_region_or_country \
                and (nxt.place_span[1] - nxt.place_span[0]) >= 0.7 * len(_PAREN_TAIL_RE.sub("", nxt.text).strip()) \
                and "in_place" not in u.feats:
            u.feats["next_line_place"] = 1.0
        if u.pos == 0 and prv is not None and prv.has_date:
            u.feats["prev_line_date"] = 1.0
    if atlas is not None:
        _atlas_features(doc, atlas, hints)


# ── the Atlas (shipped reference data) ───────────────────────────────────────
#
# The Atlas is evidence, never an authority, and the library always outranks it:
#   - a unit the library has already spoken for (any lib_* feature) gets no atl_* feature;
#   - if the library names an artist, a venue or an event anywhere in the file, the
#     Atlas adds no evidence of that kind anywhere in the file;
#   - every atl_* weight is smaller than the smallest lib_* weight for the same role
#     (weights.py; tests/test_atlas_features.py keeps it so).
# Exact matches are cheap and run for every header unit; fuzzy matches run for a few.

_ATL_SKIP = ("source_kw", "recording_verb", "equip_word", "notes_word", "has_gt", "has_clock",
             "prose", "w8p")
_ATL_FUZZY_BUDGET = 4
_ATL_MIN_ONE_WORD_ACT = 3          # a one-word act must have this much MusicBrainz history


def _atlas_features(doc, atlas, hints):
    units = [u for u in doc.units if u.kind == "seg"]
    lib = {
        "artist": any("lib_artist_exact" in u.feats or "lib_artist_core" in u.feats for u in units),
        "venue": any("lib_venue" in u.feats or "lib_venue_in" in u.feats for u in units),
        "event": any("lib_event" in u.feats for u in units),
    }
    show_keys, show_events = _atlas_show(atlas, hints) if not (lib["venue"] and lib["event"]) else (set(), set())
    if lib["venue"]:
        show_keys = set()
    if lib["event"]:
        show_events = set()
    doc_cities, doc_countries = _doc_places(doc)
    budget = _ATL_FUZZY_BUDGET
    for u in units:
        f = u.feats
        if any(k.startswith("lib_") for k in f) or "in_place" in f:
            continue
        if f.get("after_track_start") or f.get("line_late") or any(f.get(k) for k in _ATL_SKIP):
            continue
        core = _strip_wrap(_PAREN_TAIL_RE.sub("", u.text))
        n = len(_words(core))
        if not core or n > 6:
            continue
        if show_keys and _key_in(norm_key(core), show_keys):
            f["atl_show_place"] = 1.0
        if show_events and n >= 2 and _key_in(norm_key(core), show_events, contained=True):
            f["atl_show_event"] = 1.0
        found = False
        if not lib["artist"]:
            for c in atlas.artist(core, limit=3, fuzzy=False):
                if n == 1 and (c.extra.get("popularity") or 0) < _ATL_MIN_ONE_WORD_ACT:
                    continue
                f["atl_artist"] = 1.0 if c.how in ("exact", "squashed") else 0.8
                found = True
                break
        if not lib["venue"]:
            for c in atlas.venue(core, limit=3, fuzzy=False):
                if not _atlas_place_fits(atlas, c, f, doc_cities, doc_countries):
                    continue
                f["atl_venue"] = 1.0 if n >= 2 else 0.5
                found = True
                break
        if not lib["event"]:
            for c in atlas.event(core, limit=3, fuzzy=False):
                if not counts_as_event(c.name, c.extra.get("event_kind")):
                    continue
                f["atl_event"] = 1.0 if n >= 2 else 0.5
                found = True
                break
        if f.get("person_name") or f.get("has_instr"):
            if atlas.musician(core, limit=1):
                f["atl_musician"] = 1.0
                found = True
        if found or budget <= 0 or n < 2 or len(core) < 6 or f.get("has_digit") or u.nb > 6:
            continue
        # nothing matched exactly: one fuzzy try, at the kind this unit looks like
        budget -= 1
        venue_like = bool(f.get("venue_word"))
        event_like = bool(f.get("event_word"))
        if not lib["event"] and event_like:
            c = [x for x in atlas.event(core, limit=3)
                 if counts_as_event(x.name, x.extra.get("event_kind"))]
            if c:
                f["atl_event_fz"] = c[0].score
        elif not lib["venue"] and venue_like:
            c = [x for x in atlas.venue(core, limit=3)
                 if _atlas_place_fits(atlas, x, f, doc_cities, doc_countries)]
            if c:
                f["atl_venue_fz"] = c[0].score
        elif not lib["artist"] and not venue_like and n <= 4:
            c = atlas.artist(core, limit=1)
            if c:
                f["atl_artist_fz"] = c[0].score


def _atlas_show(atlas, hints):
    """(place keys, event keys) of the shows the Atlas lists for this act on this exact day.
    hints: show_artist and show_date (y, m, d), set by parse_info_file from the text itself; the
    plain artist and date hints (tags, folder name) work too. A partial date gives nothing."""
    h = hints or {}
    art, date = h.get("show_artist") or h.get("artist"), h.get("show_date") or h.get("date")
    if not art or not date or not all(date):
        return set(), set()
    places, events = set(), set()
    for ep in atlas.event_place(art, date):
        if not ep.get("exact"):
            continue
        if ep.get("place_id") is not None:
            places.update(atlas.place_keys(ep["place_id"]))
        if counts_as_event(ep.get("event"), ep.get("kind")):
            events.add(norm_key(ep.get("event") or ""))
    events.discard("")
    return places, events


def _atlas_show_place_keys(atlas, hints):
    return _atlas_show(atlas, hints)[0]


def _key_in(key, keys, contained=False):
    """key is one of keys; with `contained`, or a whole-word run inside one of them."""
    if key in keys:
        return True
    if contained:
        pad = f" {key} "
        return any(pad in f" {k} " for k in keys)
    return False


def _doc_places(doc):
    """Normalised city names and country names the text's own place lines state."""
    cities, countries = set(), set()
    for L in doc.lines:
        pl = L.place
        if pl is None:
            continue
        if pl.city:
            cities.add(norm_key(pl.city))
        if pl.country:
            countries.add(norm_key(pl.country))
    return cities, countries


_ATL_INSTITUTION_KINDS = {"school"}


def _atlas_place_fits(atlas, c, f, doc_cities, doc_countries):
    """An Atlas place is evidence for a text segment only when the text does not contradict it:
    it sits in a town the text names (or, text naming only a country, in that country), and an
    institution (a university) needs a venue word as well. No place in the text: no objection."""
    if c.extra.get("place_kind") in _ATL_INSTITUTION_KINDS and not f.get("venue_word"):
        return False
    if doc_cities:
        keys = atlas.place_area_keys(c.id)
        if c.extra.get("city"):
            keys.add(norm_key(c.extra["city"]))
        if keys:
            return bool(keys & doc_cities) or any(f" {d} " in f" {k} " or f" {k} " in f" {d} "
                                                  for d in doc_cities for k in keys)
    elif doc_countries and c.extra.get("country"):
        from .place import _country_display
        try:
            return norm_key(_country_display(c.extra["country"])) in doc_countries
        except KeyError:
            return True
    return True
