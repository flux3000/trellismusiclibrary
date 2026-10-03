"""
Strict date grammar for info text and tags (Resolver v2, chunk 1, 2026-10-03).

Why this exists: the old path handed lines to dateutil's fuzzy parser, which
fills any missing month, day or even year from today's date ("JULY 11,1981"
became 2026-07-11, "August 1974" became 1974-08-03). A made-up full date then
outranked a correct year-only tag. Here every mention records which components
the text actually wrote, and a component that was not written stays None.

Two layers:
  find_dates(text)      every mention, with span, written set and role hint
  best_show_date(text)  picks the show date and combines consistent mentions

Two-digit years (Ryan, 2026-10-03): yy at or below the current two-digit year
is 20yy, above it is 19yy. Live tapes from 1900-1926 essentially do not exist,
so a small yy is read as 20yy rather than left unknown.

Numeric dates with both parts 12 or under ("5/6/83") read month first (US
order): most tapers and seeders are in the US or use that style. Day first
applies when a part is above 12, or when another mention in the text writes
the month that only the day-first reading gives. They are not flagged ambiguous.
"""
import calendar
import datetime
import re
from dataclasses import dataclass, field

_CUR_YEAR = datetime.date.today().year
_CUR_YY = _CUR_YEAR % 100
_MIN_YEAR = 1900

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MON = (r"(?<![A-Za-z])(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)(?![A-Za-z])\.?")
_ORD = r"(?:st|nd|rd|th)?"
_YEAR4 = r"(?:19|20)\d{2}"
_RANGE_SEP = r"(?:-|–|—|&|and|thru|through|to)"
_ALT_SEP = r"or"

_ISO_RE = re.compile(
    rf"(?<![\d.\-/])({_YEAR4})[-./](\d{{2}}|\?\?)(?:[-./](\d{{1,2}}|\?\?))?(?![\d])")
_ISO_SP_RE = re.compile(rf"(?<![\d.\-/])({_YEAR4}) (\d{{2}}) (\d{{2}})(?![\d:])")
_ISO_RANGE_TAIL = re.compile(
    rf"[ \t]*(?:thru|through|to|until|-|–|—)[ \t]*({_YEAR4})[-./](\d{{1,2}})[-./](\d{{1,2}})(?!\d)")
_NUM_RE = re.compile(
    r"(?<![\d.:/\-])(\d{1,2}|\?\?|xx)[-./](\d{1,2}|\?\?|xx)[-./](\d{4}|\d{2}|\?\?\?\?|\?\?)(?![\d:])", re.I)
_MD_RE = re.compile(
    rf"({_MON})[ \t]*(\d{{1,2}})(?!\d){_ORD}"
    rf"(?:[ \t]*(?:({_RANGE_SEP})|({_ALT_SEP}))[ \t]*(\d{{1,2}})(?!\d){_ORD})?"
    rf"(?:[ \t]*,?[ \t]*(?:({_YEAR4})(?![\d:])|'(\d{{2}})(?!\d)))?", re.I)
_DM_RE = re.compile(
    rf"(?<![\d.:/\-])(\d{{1,2}}){_ORD}[ \t.\-]*(?:of[ \t]+)?({_MON})(?:[ \t.,]*({_YEAR4})(?![\d:]))?", re.I)
_YMD_RE = re.compile(rf"(?<![\d.:/\-])({_YEAR4})[ \t,]+({_MON})[ \t.]*(\d{{1,2}})(?!\d){_ORD}", re.I)
_MY_RE = re.compile(rf"({_MON})[ \t.,]*({_YEAR4})(?![\d:])", re.I)
_YEAR_RE = re.compile(rf"(?<![\d.:/\-,])({_YEAR4})(?![\d:]|[.\-/]\d)")

# Words that make a date something other than the performance date. The nearest
# keyword before the date wins; one just after it counts only if none came before.
_NON_SHOW = re.compile(
    r"transfer|seed|\beac\b|exact audio copy|extraction|log ?file|upload|ripp?ed|"
    r"encod|convert|burn|digiti[sz]|remaster|mastered|dubbed|copied|posted|"
    r"created|flac'?d|\bshn'?d|checksum|updated", re.I)
_SHOW = re.compile(r"perform|record(?:ed|ing)|concert|\bshow\b|\blive\b|taped|played", re.I)
_WEAK_SHOW = re.compile(r"\bdate\b", re.I)


@dataclass
class DateMention:
    year: int | None
    month: int | None
    day: int | None
    end_day: int | None = None
    written: set = field(default_factory=set)
    span: tuple = (0, 0)
    ambiguous: bool = False
    role_hint: str | None = None          # "performance", "transfer" or None
    alt: tuple | None = None              # (month, day) other reading of a numeric date


def _valid_day(y, m, d):
    if d is None or not 1 <= d <= 31:
        return False
    if m and y:
        return d <= calendar.monthrange(y, m)[1]
    if m:
        return d <= calendar.monthrange(2000, m)[1]
    return True


def _year_ok(y):
    return y is not None and _MIN_YEAR <= y <= _CUR_YEAR


def _mon(token):
    return _MONTHS[token.lower().rstrip(".")[:3]]


def _written(y, m, d):
    return {n for n, v in (("year", y), ("month", m), ("day", d)) if v is not None}


def _role(text, span):
    ls = text.rfind("\n", 0, span[0]) + 1
    le = text.find("\n", span[1])
    le = len(text) if le < 0 else le
    before = text[max(ls, span[0] - 30):span[0]]
    after = text[span[1]:min(le, span[1] + 30)]
    nb = list(_NON_SHOW.finditer(before))
    sb = list(_SHOW.finditer(before))
    if nb or sb:
        n_end = nb[-1].end() if nb else -1
        s_end = sb[-1].end() if sb else -1
        return "transfer" if n_end > s_end else "performance"
    if _NON_SHOW.search(after):
        return "transfer"
    if _SHOW.search(after) or _WEAK_SHOW.search(before):
        return "performance"
    return None


def _blank(s, a, b):
    return s[:a] + " " * (b - a) + s[b:]


def find_dates(text):
    """Every date mention in `text`, in text order, never filling an unwritten part."""
    text = text or ""
    work = text
    out = []

    def add(m, y, mo, d, span, **kw):
        nonlocal work
        out.append(DateMention(year=y, month=mo, day=d, written=_written(y, mo, d),
                               span=span, role_hint=_role(text, span), **kw))
        work = _blank(work, *span)

    # ISO: 1977-05-08, 1996-12-??, 1977-05
    for m in list(_ISO_RE.finditer(work)):
        if work[m.start():m.end()].strip() == "" or work[m.start()] == " ":
            continue
        y = int(m.group(1))
        mo = None if m.group(2) == "??" else int(m.group(2))
        d = None if (m.group(3) in (None, "??")) else int(m.group(3))
        if not _year_ok(y) or (mo is not None and not 1 <= mo <= 12) or \
                (d is not None and not _valid_day(y, mo, d)):
            continue
        end, end_day = m.end(), None
        tail = _ISO_RANGE_TAIL.match(work, m.end())
        if tail and d and int(tail.group(1)) == y and int(tail.group(2)) == mo \
                and int(tail.group(3)) > d:
            end, end_day = tail.end(), int(tail.group(3))     # range: start day stays, tentative
        add(m, y, mo, d, (m.start(), end), end_day=end_day)

    # "2017 11 14": spaces only when month and day are both two digits, so a year
    # followed by a count ("1975 12 songs") is not read as a date.
    for m in list(_ISO_SP_RE.finditer(work)):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _year_ok(y) and 1 <= mo <= 12 and _valid_day(y, mo, d):
            add(m, y, mo, d, (m.start(), m.end()))

    # Numeric a/b/c with . - / separators
    for m in list(_NUM_RE.finditer(work)):
        a_s, b_s, c_s = m.groups()
        if a_s.isdigit() and int(a_s) > 31 and len(a_s) == 2 and b_s.isdigit() and c_s.isdigit() \
                and len(c_s) == 2 and 1 <= int(b_s) <= 12 and _valid_day(None, int(b_s), int(c_s)):
            # "81-04-20": year first with two digits (only when a cannot be a day).
            y = 1900 + int(a_s)
            if _year_ok(y):
                mo, d = int(b_s), int(c_s)
                out.append(DateMention(year=y, month=mo, day=d, written=_written(y, mo, d),
                                       span=(m.start(), m.end()),
                                       role_hint=_role(text, (m.start(), m.end()))))
                work = _blank(work, m.start(), m.end())
            continue
        a = None if a_s in ("??",) or a_s.lower() == "xx" else int(a_s)
        b = None if b_s == "??" or b_s.lower() == "xx" else int(b_s)
        y, yy, ambiguous, alt = None, None, False, None
        if "?" in c_s:
            y = None
        elif len(c_s) == 4:
            y = int(c_s)
            if not _year_ok(y):
                continue
        else:
            yy = int(c_s)
            y = 1900 + yy if yy > _CUR_YY else 2000 + yy
        mo = d = None
        if a is not None and b is not None:
            if a > 12 and b <= 12:
                d, mo = a, b
            elif b > 12 and a <= 12:
                mo, d = a, b
            elif a <= 12 and b <= 12:
                mo, d = a, b
                if a != b:
                    alt = (b, a)          # day-first reading, used only if another mention settles it
            else:
                continue
        elif a is not None and b is None:
            if a <= 12:
                mo = a                    # "01-??-1953": month first, day unknown
            else:
                d = a
        elif b is not None:
            ambiguous = True              # "??/05/1964": order unknowable, keep year only
            mo = d = None
        if d is not None and not _valid_day(y, mo, d):
            continue
        if y is None and yy is None and mo is None and d is None:
            continue                      # "??/??/??" says nothing
        mm = DateMention(year=y, month=mo, day=d, written=_written(y, mo, d),
                         span=(m.start(), m.end()), ambiguous=ambiguous, alt=alt,
                         role_hint=_role(text, (m.start(), m.end())))
        out.append(mm)
        work = _blank(work, m.start(), m.end())

    # Year first: "1976 September 18"
    for m in list(_YMD_RE.finditer(work)):
        y, mo, d = int(m.group(1)), _mon(m.group(2)), int(m.group(3))
        if _year_ok(y) and _valid_day(y, mo, d):
            add(m, y, mo, d, (m.start(), m.end()))

    # "Month D[-D2| or D2][, YYYY]"
    for m in list(_MD_RE.finditer(work)):
        mo, d = _mon(m.group(1)), int(m.group(2))
        sep_range, sep_alt, d2 = m.group(3), m.group(4), m.group(5)
        y = int(m.group(6)) if m.group(6) else None
        if m.group(7):                     # 'YY
            yy = int(m.group(7))
            y = 1900 + yy if yy > _CUR_YY else 2000 + yy
        if y is not None and not _year_ok(y):
            continue
        if not _valid_day(y, mo, d):
            continue
        end_day, ambiguous = None, False
        if d2 is not None:
            d2 = int(d2)
            if sep_alt:
                ambiguous = True           # "6th or 7th": the day is not one value
            elif d2 > d and _valid_day(y, mo, d2):
                end_day = d2
        mm = DateMention(year=y, month=mo, day=None if ambiguous else d, end_day=end_day,
                         written=_written(y, mo, None if ambiguous else d),
                         span=(m.start(), m.end()), ambiguous=ambiguous,
                         role_hint=_role(text, (m.start(), m.end())))
        out.append(mm)
        work = _blank(work, m.start(), m.end())

    # "D Month [YYYY]", "29.August 1979"
    for m in list(_DM_RE.finditer(work)):
        d, mo = int(m.group(1)), _mon(m.group(2))
        y = int(m.group(3)) if m.group(3) else None
        if (y is not None and not _year_ok(y)) or not _valid_day(y, mo, d):
            continue
        add(m, y, mo, d, (m.start(), m.end()))

    # "Month YYYY"
    for m in list(_MY_RE.finditer(work)):
        y = int(m.group(2))
        if _year_ok(y):
            add(m, y, _mon(m.group(1)), None, (m.start(), m.end()))

    # Bare year
    for m in list(_YEAR_RE.finditer(work)):
        y = int(m.group(1))
        if _year_ok(y):
            add(m, y, None, None, (m.start(), m.end()))

    out.sort(key=lambda d: d.span[0])
    return out


def _agrees(a, b):
    """True when the components both mentions state do not conflict, and they share one."""
    shared = False
    for x, y in ((a.year, b.year), (a.month, b.month), (a.day, b.day)):
        if x is not None and y is not None:
            if x != y:
                return False
            shared = True
    return shared


def _settle(mentions):
    """Switch a month-first numeric date to day-first when another mention writes that month."""
    for m in mentions:
        if m.alt is None:
            continue
        amo, ad = m.alt
        others = [o for o in mentions if o is not m and o.alt is None and o.month]
        if any(o.month == amo for o in others) and not any(o.month == m.month for o in others):
            m.month, m.day = amo, ad
        m.alt = None


def _effective(m):
    """(year, month, day) a mention supports, dropping parts that are still ambiguous."""
    return m.year, m.month, m.day


def best_show_date(text):
    """
    Pick the show date from `text`: (DateMention | None, all_mentions).

    Transfer/seed/EAC/upload mentions are never chosen. Mentions combine when
    they agree (a year in the title plus "June 18" below it); the most specific
    performance-role mention leads and supplies day and month, other agreeing
    mentions supply a missing year. Never fills a component nobody wrote.
    """
    mentions = find_dates(text)
    _settle(mentions)
    usable = [m for m in mentions if m.role_hint != "transfer"]

    def prec(m):
        y, mo, d = _effective(m)
        return sum(v is not None for v in (y, mo, d))

    scored = []
    for idx, m in enumerate(usable):
        if prec(m) == 0:
            continue
        agree = sum(1 for o in usable if o is not m and _agrees(m, o))
        # Position breaks ties: the header comes first; a later 'recorded ... May 6'
        # remark must not outrank the date line (role only excludes transfers).
        score = prec(m) * 10 + agree * 3
        # An earlier month-bearing mention outranks a later one that contradicts its
        # year (the later one is usually a copy or upload date with no keyword).
        if m.year and any(o.year and o.year != m.year and prec(o) >= 2 and o.span[0] < m.span[0]
                          for o in usable):
            score -= 15
        scored.append((score, -idx, m))
    if not scored:
        return None, mentions
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    lead = scored[0][2]
    y, mo, d = _effective(lead)

    if y is None:
        # Year from the best-supported mention that does not conflict.
        for _s, _i, o in scored:
            if o is lead or o.year is None:
                continue
            if (mo is None or o.month in (None, mo)) and (d is None or o.day in (None, d)):
                y = o.year
                break
    elif mo is None:
        # Year-only lead takes month/day from a yearless mention that is consistent.
        for _s, _i, o in scored:
            if o is lead or o.year not in (None, y) or o.alt is not None:
                continue
            if o.month and o.year is None:
                mo, d = o.month, o.day
                break

    written = set()
    for n, v in (("year", y), ("month", mo), ("day", d)):
        if v is not None:
            written.add(n)
    keep_end = lead.end_day if (d is not None and d == lead.day) else None
    best = DateMention(year=y, month=mo, day=d, end_day=keep_end, written=written,
                       span=lead.span, ambiguous=lead.ambiguous,
                       role_hint=lead.role_hint)
    return best, mentions
