"""
Event names read off an info file, and the key two spellings of one event share.

clean_event_name() is what a header line becomes before anyone stores or shows it:
no leading ordinal ("30th", "27."), no enclosing parentheses, no lead-in
("Part of the"), and nothing at all when what is left is a bare generic word.

event_key() is for matching only. It also drops years, ordinals anywhere and "annual",
expands the usual short forms (Fest, BG, Intl) and squashes spacing, so "Telluride BG
Festival" and "30th Telluride Bluegrass Festival" meet at one key. Nothing is stored
under the key.
"""
import re
import unicodedata

_GENERIC = {"festival", "fest", "festivals", "concert", "concerts", "show", "shows", "live",
            "music", "event", "events", "gig", "jam", "the", "a", "an", "and", "of"}
_LEAD_INS = re.compile(r"^(?:as\s+)?part\s+of\s+(?:the\s+)?|^(?:live\s+)?(?:at|from)\s+the\s+", re.I)
_LEAD_ORDINAL = re.compile(r"^\d+\s*(?:st|nd|rd|th)\b\.?\s*|^\d+\.\s+|^annual\s+", re.I)
_ORDINAL_WORD = re.compile(r"^\d+(?:st|nd|rd|th)$", re.I)
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_SHORT = {"fest": "festival", "bg": "bluegrass", "intl": "international", "int": "international",
          "annual": "", "annueal": "", "the": ""}


def _enclosed(s):
    while len(s) > 1 and ((s[0], s[-1]) in {("(", ")"), ("[", "]")}):
        s = s[1:-1].strip()
    return s


def clean_event_name(text):
    """The event a header line names, tidied; None when there is nothing event-like left."""
    if not isinstance(text, str):
        return None
    s = re.sub(r"\s+", " ", text).strip()
    for _ in range(3):                                   # each pass can expose the next
        before = s
        s = _enclosed(s)
        s = _LEAD_INS.sub("", s).strip()
        s = _LEAD_ORDINAL.sub("", s).strip()
        if s == before:
            break
    s = s.strip(" ,;:-")
    words = [w for w in re.sub(r"[^\w\s]", " ", s.lower()).split() if w]
    if not words or all(w in _GENERIC or w.isdigit() for w in words):
        return None
    return s


def event_key(text):
    """The matching key for an event name; "" when the name is not event-like."""
    s = clean_event_name(text)
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("&", " and ")
    s = re.sub(r"#\s*\d+\s*$", " ", s)
    out = []
    for w in re.sub(r"[^\w\s]", " ", s).split():
        if _ORDINAL_WORD.match(w) or _YEAR.match(w):
            continue
        w = _SHORT.get(w, w)
        if w:
            out.append(w)
    key = "".join(out)
    if key.endswith("fest"):                              # Springfest, Spring Fest, Spring Festival
        key += "ival"
    return key
