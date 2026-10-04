"""
Segmenter (Resolver v2, chunk 2, 2026-10-03).

Splits info text into segments and keeps what later steps need as evidence:
the line index, the blank-line block, the separator that preceded the segment
and the character span in the original text. Pure function, no I/O.

Separators:
  soft   ","  (not between digits)
  hard   ";"  " - "  " – "  " — "  " @ "  " / "  " | "
  label  a leading "Label:" prefix on a line (kept on the first segment)

A comma with no space after it still splits ("BLUES ALLEY,WASH.D.C."), a comma
between two digits does not ("1,000").
"""
import re
from dataclasses import dataclass

_SEP_RE = re.compile(
    r"(?P<soft>(?<!\d),|,(?!\d))"
    r"|(?P<hard>;|\s+[–—-]\s+|\s+@\s*|\s*@\s+|\s+/\s+|\s+\|\s+)")
# "Label:" at the start of a line. Letters and spaces only, so times, URLs and
# "Set 1:" style headers are not read as labels.
_LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]{0,24}):[ \t]+(?=\S)")


@dataclass
class Segment:
    text: str                 # stripped text
    start: int                # span in the original text (stripped bounds)
    end: int
    line: int = 0             # line index within the text
    block: int = 0            # blank-line separated block index
    sep_before: str = ""      # separator text that preceded this segment ("" at line start)
    hard_before: bool = False  # True when that separator was hard (not a comma)
    label: str | None = None  # "Label:" prefix, on the first segment of a line

    @property
    def span(self):
        return (self.start, self.end)


def segment_line(line, line_index=0, block=0, offset=0, labels=True):
    """Segments of one line. `offset` shifts spans into a larger text. `labels=False`
    leaves a leading "Label:" in the first segment (the caller handles labels)."""
    segs = []
    pos = 0
    label = None
    m = _LABEL_RE.match(line) if labels else None
    if m:
        label = m.group(1).strip()
        pos = m.end()

    def add(a, b, sep, hard, lab):
        raw = line[a:b]
        txt = raw.strip()
        if not txt:
            return
        lead = len(raw) - len(raw.lstrip())
        s = a + lead
        segs.append(Segment(txt, offset + s, offset + s + len(txt), line_index, block,
                            sep, hard, lab))

    cur, sep, hard = pos, "", False
    for sm in _SEP_RE.finditer(line, pos):
        add(cur, sm.start(), sep, hard, label if not segs else None)
        sep, hard = sm.group(0), sm.lastgroup == "hard"
        cur = sm.end()
    add(cur, len(line), sep, hard, label if not segs else None)
    return segs


def segment_text(text):
    """Segments of a whole text, in order, with absolute spans. Blank lines
    start a new block."""
    out = []
    block = 0
    in_blank = False
    off = 0
    for i, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\r")
        if not line.strip():
            if not in_blank and out:
                block += 1
            in_blank = True
        else:
            in_blank = False
            out.extend(segment_line(line, i, block, off))
        off += len(raw) + 1
    return out
