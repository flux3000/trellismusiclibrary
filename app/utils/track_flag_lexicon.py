"""
Thesaurus for suggesting track flags from a title.

Pure stdlib, no DB: `match_flags(title)` returns the set of flag keys the
title's wording implies (banter, tuning, audience, announcement, interview,
introduction, band_intros, outro). Structural markers ("//", "(x)") are not
handled here; see ingest.detect_track_flags.

Matching is on normalized text: accents, case, quotes, brackets, timestamps,
track numbers and trailing decoration are removed, then the title is split into
segments on separators ("/", ",", "&", ">", " - ", "and"). A segment matches
only as a WHOLE phrase (or a deliberately narrow pattern), never as a
substring, so "Dark Star Intro" and "Talking Heads" stay unflagged.

Two tiers, because some labels double as real song titles:
  * Tier A phrases flag on their own wherever they appear as a segment.
  * Tier B phrases ("goodnight", "talk", and the bare "intro"/"outro", which
    are as often a musical section as a spoken one) flag only when EVERY
    segment of the title is itself a flag phrase. "Goodnight, Irene" and
    "Intro > Deep Water" have a segment that is a song, so they stay
    unflagged; "Talk", "Intro" and "Intro / Tuning" flag.

To add a synonym, add it to PHRASES_A or PHRASES_B below. Phrases are written
in normalized form (lowercase, no apostrophes, singular or plural both fine;
a trailing "s" is tolerated on lookup).
"""
import re
import unicodedata

# ── Tier A: safe to flag a segment on its own ───────────────────────────────
PHRASES_A = {
    'banter': {
        'banter', 'stage banter', 'audience banter', 'crowd banter', 'chatter',
        'stage chatter', 'chat', 'chit chat', 'chitchat', 'crosstalk',
        'cross talk', 'dialogue', 'dialog', 'patter', 'stage patter',
        'stage talk', 'speech', 'speaking',
        'remarks', 'comments', 'comment', 'monologue',
        'technical difficulties', 'tech difficulties',
    },
    'tuning': {
        'tuning', 'tune up', 'tuneup', 'tuning up', 'retuning',
        'instrument tuning', 'tuning noodling', 'tuning and noodling',
        'line check', 'mic check', 'microphone check',
    },
    'audience': {
        'crowd', 'audience', 'applause', 'cheers', 'cheering', 'clapping',
        'ovation', 'standing ovation', 'crowd noise', 'audience noise',
        'crowd applause', 'audience applause', 'crowd cheering',
        'crowd response', 'audience response', 'crowd reaction',
        'audience reaction', 'crowd atmosphere', 'audience atmosphere',
        'crowd ambience', 'audience ambience', 'crowd ambiance',
        'audience ambiance', 'crowd chatter', 'crowd overlap',
        'encore break', 'encore applause', 'encore call', 'encore cheers',
        'crowd chant', 'crowd singing', 'audience singing',
        'crowd sing along', 'audience sing along', 'whistling', 'ambience',
    },
    'band_intros': {
        'band intro', 'band introduction', 'band member intro',
        'band member introduction', 'introduction of the band',
        'introducing the band', 'band intro s', 'band intros', 'member introduction', 'member intro',
        'player intro', 'musician intro', 'introductions of the band',
    },
    'introduction': {
        'song intro', 'song introduction',
        'set intro', 'show intro', 'stage intro', 'encore intro',
        'early show intro', 'late show intro', 'guest intro',
        'guests intro', 'guest introduction', 'fm intro', 'radio intro',
        'announcer intro', 'mc intro', 'emcee intro', 'dj intro', 'host intro',
        'festival mc intro', 'radio announcer intro', 'mc', 'emcee',
        'mc introduction', 'emcee introduction', 'radio announcer introduction',
        'intro by radio announcer', 'intro by festival mc',
    },
    'announcement': {
        'announcement', 'announcer', 'stage announcement', 'pa announcement',
        'public service announcement', 'psa', 'station id',
        'station identification', 'station break', 'promo', 'fm speaker',
        'radio announcer', 'radio host', 'dj', 'commentary',
        'radio commentary', 'sponsor message', 'pledge drive',
        'fund drive', 'setbreak announcement', 'set break announcement',
    },
    'interview': {
        'interview', 'radio interview', 'live interview', 'qanda',
        'question and answer', 'press conference',
    },
    'outro': {
        'fm outro', 'radio outro', 'mc outro',
        'emcee outro', 'announcer outro', 'show outro', 'set outro',
        'stage outro', 'closing', 'closing remarks', 'closing comments',
        'closing words', 'closing statement', 'closing announcements',
        'closing thanks', 'signoff', 'sign off',
        'end of show', 'see you next time', 'thanks for coming',
        'thank you for coming', 'thanks everybody', 'thank you everybody',
    },
}

# ── Tier B: also real song titles, so only flag when nothing else is left ───
PHRASES_B = {
    'banter': {
        'talk', 'talking', 'talks', 'welcome', 'greeting', 'greetings',
        'thank yous', 'thank you very much',
    },
    'tuning': {
        'warm up', 'warming up',
    },
    'introduction': {'intro', 'introduction'},
    'outro': {
        'outro', 'outtro', 'goodnight', 'good night', 'goodnights', 'gnight', 'good night all',
        'goodnight everybody', 'good night everybody', 'say goodnight',
        'farewell',
    },
}

# ── Words that flag a segment wherever they sit in it ───────────────────────
# Only words that essentially never occur inside a song title.
_ANYWHERE = [
    ('banter',       re.compile(r'\b(?:banter|chatter|crosstalk|chit chat)\b')),
    ('announcement', re.compile(r'\b(?:announcements?|announcer|announce)\b')),
    ('interview',    re.compile(r'\binterviews?\b')),
]

# ── Narrow whole-segment patterns ───────────────────────────────────────────
# "<name> speaks" is spoken word; the veto list holds the song titles of the
# same shape ("Money Talks", "Pillow Talk" is not matched at all: singular).
_NAME_SPEAKS = re.compile(r'^(?P<who>(?:[a-z]+ ){1,2})(?:speaks|talks|talking|chats)$')
_NAME_SPEAKS_VETO = frozenset({
    'money', 'actions', 'silence', 'love', 'music', 'it', 'its', 'he', 'she',
    'they', 'walk', 'baby', 'sweet', 'smooth', 'small', 'trash', 'sleep',
    'big', 'cheap', 'dirty', 'fast', 'jive', 'straight', 'pillow', 'pep',
    'shop', 'girl', 'boy', 'double', 'back', 'sales', 'locker', 'rap', 'real',
    'free', 'sky', 'idle', 'tough', 'plain', 'sweet', 'baby', 'trash',
    'dont', 'cant', 'wont', 'didnt', 'lets', 'just', 'i', 'you', 'we', 'to',
    'not', 'never', 'no', 'stop', 'please', 'quit', 'let', 'do', 'can', 'the', 'a', 'my',
    'your', 'our', 'his', 'her', 'blues', 'hearts', 'heart',
})
_REMARKS = re.compile(r'^(?:[a-z]+ ){1,3}(?:remarks|comments)$')
_TUNING_SUFFIX = re.compile(r'^(?:[a-z]+ ){1,2}(?:tuning|tuneup|tune up)$')
_APPLAUSE_LEAD = re.compile(r'^applause(?: [a-z]+){1,3}$')
_CROWD_LEAD = re.compile(
    r'^(?:crowd|audience) (?:noise|atmosphere|ambience|ambiance|chatter|'
    r'cheers|cheering|applause|overlap|response|reaction|singing|chanting|'
    r'sing along|participation)(?: [a-z0-9]+){0,3}$')
_APPLAUSE_TRAIL = re.compile(r'^(?:[a-z]+ ){1,2}applause$')
_INTRO_ROLE = (r'(?:song|set|show|stage|fm|radio|announcer|mc|emcee|dj|host|guests?|'
               r'encore|festival mc|radio announcer|early show|late show|opening)')
_ROLE_INTRO = re.compile(rf'^(?:{_INTRO_ROLE})(?: (?:\d+|i{{1,3}}|iv|one|two|three|first|second|third))? intros?$')
# A title that is entirely bracketed is an annotation, never a song, so
# "(Cox Family Intro)" can name the speaker without risking "Dark Star Intro".
_BRACKETED = re.compile(r'^\s*[(\[<{]\s*([^()\[\]<>{}]+?)\s*[)\]>}]\s*[,>-]*\s*$')
_BRACKET_INTRO = re.compile(r'^(?:[a-z]+ ){1,4}intro(?:duction)?s?$')
_BRACKET_OUTRO = re.compile(r'^(?:[a-z]+ ){1,4}outros?$')
_NAME_TALK = re.compile(r'^(?P<who>[a-z]+) talk$')
_BAND_INTRO_BY = re.compile(r'^band intro(?:duction)?s? by [a-z ]+$')
_INTRO_BY = re.compile(r'^intro(?:duction)?s? (?:by|of|to) (?:[a-z0-9]+ ?){1,5}$')
_INTRODUCES = re.compile(r'^(?:[a-z]+ ){1,2}(?:introduces|introducing) [a-z0-9 ]+$')
_BAND_INTRO = re.compile(r'^(?:[a-z]+ ){0,2}(?:band|group) (?:member )?intro(?:duction)?s?$')
_OUTRO_ROLE = re.compile(r'^(?:fm|radio|radio announcer|mc|emcee|announcer|show|set|stage|final|band|dj|host)s? outros?$')
_OUTRO_PLURAL = re.compile(r'^outros?$')

_FILLER_PREFIX = frozenset({
    'the', 'a', 'an', 'more', 'even', 'final', 'last', 'brief', 'short', 'little',
    'some', 'extra', 'additional', 'another', 'quick', 'long', 'few',
})

_TIMESTAMP = re.compile(
    r"[\[(<]?\s*(?:\d{1,2}:)?\d{0,2}:\d{2}(?:[.:]\d+)?\s*[\])>]?"
    r"|\b\d{1,2}'\d{2}\"?")
_LEAD_TRACKNO = re.compile(r'^\s*(?:d\d+\s*)?(?:trk|track|t)?\s*\d{1,3}\s*[-.):\]]\s+')
# "Q & A" would otherwise be split on its ampersand.
_QA = re.compile(r'\bq\s*(?:&|and)\s*a\b')
_SPLIT = re.compile(
    r'\s*(?:->|-+>|>|<|/|\\|,|;|&|\+|–|—|:|~|\.{2,}|…|\s-+\s|\band\b|\bthen\b)\s*')
_SEPARATOR_END = ('>', '-', '/', ',', '&', '+', ':', '~')
_TRAILING_PAREN = re.compile(r'^(.*?)\s*\([^)]*\)\s*$')


def _fold(text):
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return text.lower().replace('’', '').replace('‘', '').replace("'", '').replace('`', '')


def _clean_segment(seg):
    seg = re.sub(r'[^a-z0-9& ]', ' ', seg.replace('-', ' '))
    tokens = [t for t in seg.split() if t != 'etc']
    # drop a trailing counter: "banter 2", "intro #1", "part 2"
    while tokens and (tokens[-1].isdigit() or
                      (len(tokens) > 1 and tokens[-2] in ('part', 'pt', 'no') and tokens[-1].isdigit())):
        tokens.pop()
        if tokens and tokens[-1] in ('part', 'pt', 'no'):
            tokens.pop()
    return ' '.join(tokens)


def title_segments(title):
    """Normalized, non-empty segments of a title (see module docstring)."""
    raw = title.strip()
    # One trailing parenthetical is an attribution ("(Bobby)") unless it is
    # the whole title ("(Chatter)").
    # A parenthetical that follows a separator ("20-20 Vision > (Talking)") is
    # a segment of its own, not an attribution.
    m = _TRAILING_PAREN.match(raw)
    if m and m.group(1).strip() and not m.group(1).rstrip().endswith(_SEPARATOR_END):
        raw = m.group(1).strip()
    raw = _fold(raw)
    raw = _TIMESTAMP.sub(' ', raw)
    raw = _QA.sub('qanda', raw)
    raw = _LEAD_TRACKNO.sub('', raw)
    out = []
    for part in _SPLIT.split(raw):
        seg = _clean_segment(part)
        if seg:
            out.append(seg)
    return out


def _variants(seg):
    seen = []
    def add(s):
        if s and s not in seen:
            seen.append(s)
    add(seg)
    tokens = seg.split()
    while len(tokens) > 1 and tokens[0] in _FILLER_PREFIX:
        tokens = tokens[1:]
        add(' '.join(tokens))
    for s in list(seen):
        if s.endswith('s'):
            add(s[:-1])
    return seen


def _lookup(table, variants):
    hits = set()
    for flag, phrases in table.items():
        if any(v in phrases for v in variants):
            hits.add(flag)
    return hits


def _pattern_flags(variants):
    hits = set()
    for v in variants:
        m = _NAME_SPEAKS.match(v)
        if m and m.group('who').split()[0] not in _NAME_SPEAKS_VETO:
            hits.add('banter')
        t = _NAME_TALK.match(v)
        if t and t.group('who') not in _NAME_SPEAKS_VETO:
            hits.add('banter')
        if _REMARKS.match(v) and not v.startswith(('closing ', 'opening ')):
            hits.add('banter')
        if _TUNING_SUFFIX.match(v):
            hits.add('tuning')
        if _APPLAUSE_LEAD.match(v) or _CROWD_LEAD.match(v) or _APPLAUSE_TRAIL.match(v):
            hits.add('audience')
        if _ROLE_INTRO.match(v) or _INTRO_BY.match(v) or _INTRODUCES.match(v):
            hits.add('introduction')
        if _BAND_INTRO.match(v) or _BAND_INTRO_BY.match(v):
            hits.add('band_intros')
        if _OUTRO_ROLE.match(v) or _OUTRO_PLURAL.match(v):
            hits.add('outro')
    return hits


def match_flags(title):
    """Set of flag keys implied by a title's wording; empty when none."""
    if not title:
        return set()
    flags = set()
    segments = title_segments(title)
    unresolved = 0
    tier_b = set()
    for seg in segments:
        variants = _variants(seg)
        a = _lookup(PHRASES_A, variants) | _pattern_flags(variants)
        for flag, rx in _ANYWHERE:
            if any(rx.search(v) for v in variants):
                a.add(flag)
        if a:
            flags |= a
            continue
        b = _lookup(PHRASES_B, variants)
        if b:
            tier_b |= b
        else:
            unresolved += 1
    # A long parenthetical after a tier B phrase is part of a song title
    # ("Thank You (Falettinme Be Mice Elf Agin)"), not an attribution.
    m = _TRAILING_PAREN.match(title.strip())
    paren_words = len(title[title.rfind('('):].split()) if m and '(' in title else 0
    if tier_b and unresolved == 0 and paren_words <= 2:
        flags |= tier_b
    if not flags:
        b = _BRACKETED.match(_fold(title))
        if b:
            inner = _clean_segment(b.group(1))
            if _BRACKET_INTRO.match(inner):
                flags.add('introduction')
            if _BRACKET_OUTRO.match(inner):
                flags.add('outro')
    return flags


# Single-word labels, for callers that need to recognise "this word is an
# annotation, not a name" (songwriter extraction).
SINGLE_WORDS = frozenset(
    p for table in (PHRASES_A, PHRASES_B) for phrases in table.values()
    for p in phrases if ' ' not in p
)
