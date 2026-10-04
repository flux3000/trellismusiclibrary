"""
Tracks, sets and timestamps from an info file (Resolver v2, chunk 3, 2026-10-03).

  unnumbered_titles(doc, dec)   an unnumbered setlist: the TRACK-role lines
  set_label_at(sets, line)      the SET_HEADER in force at a line (the set carrier)
  printed_seconds(text)         the trailing "7:27" or "(7:27)" a taper printed
  align_durations(...)          do the printed times fit the real track lengths
"""
import re



def printed_seconds(text):
    """Seconds from a trailing timestamp ("Dark Star 12:34", "Carry On (4:59)", "Intro :45"), or None."""
    t = (text or "").rstrip()
    m = re.search(r"\(\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*\)\s*$", t)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
    else:
        m = re.search(r"(?:^|\s)(\d{0,2}):(\d{2})(?::(\d{2}))?\s*$", t)
        if not m:
            return None
        a, b, c = m.group(1) or "0", m.group(2), m.group(3)
    a, b = int(a), int(b)
    if c is not None:
        return a * 3600 + b * 60 + int(c)
    return a * 60 + b


def unnumbered_titles(dec):
    """[(line index, text)] of TRACK-role whole lines, in order."""
    return [(d.unit.line, d.unit.text) for d in dec
            if d.role == "TRACK" and d.unit.kind == "titleline"]


def set_label_at(sets, line):
    """Label of the last set header above `line`, or None when there is none."""
    label = None
    for s in sets:
        if s["line"] < line:
            label = s["label"] or label
        else:
            break
    return label


def _tol(d):
    return max(3.0, 0.03 * d)


def align_durations(printed, durations):
    """
    Compare printed running times with the real track lengths.

    printed    seconds per info-file track in order (None where a track printed none)
    durations  seconds per audio file in order

    Returns {"status", "offset", "matched", "compared"}:
      confirmed  at least 80% of the compared tracks fit, position for position
      shifted    they fit at another offset (an info file listing extra or missing tracks)
      rejected   enough times were compared and they do not fit anywhere
      none       fewer than three printed times to compare, or no durations
    """
    d = [x for x in (durations or [])]
    p = list(printed or [])
    if not d or sum(1 for x in p if x) < 3:
        return {"status": "none", "offset": 0, "matched": 0, "compared": 0}

    def score(off):
        m = c = 0
        for i, x in enumerate(p):
            j = i + off
            if x is None or not (0 <= j < len(d)) or not d[j]:
                continue
            c += 1
            if abs(x - d[j]) <= _tol(d[j]):
                m += 1
        return m, c

    m0, c0 = score(0)
    if c0 >= 3 and m0 >= 0.8 * c0:
        return {"status": "confirmed", "offset": 0, "matched": m0, "compared": c0}
    best = max(((score(o), o) for o in range(-4, 5)), key=lambda t: (t[0][0], -abs(t[1])))
    (mb, cb), ob = best
    if cb >= 3 and mb >= 0.8 * cb and ob != 0:
        return {"status": "shifted", "offset": ob, "matched": mb, "compared": cb}
    return {"status": "rejected", "offset": 0, "matched": m0, "compared": c0}
