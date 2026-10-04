"""
app/utils/resolver_corpus/common.py -- helpers shared by the two collectors.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_CORPUS = Path(__file__).resolve().parents[3].parent / "resolver-corpus"
INFO_MAX_BYTES = 300 * 1024
SAMPLE_SIZE = 1000
INTERVAL = 2.0
JITTER = 0.5
AVG_REQUEST_SECONDS = INTERVAL + JITTER / 2          # mean spacing between requests


# -- sampling ----------------------------------------------------------------

def hash_key(key):
    return hashlib.sha1(str(key).encode("utf-8")).hexdigest()


def stable_sample(keys, n):
    """The n keys with the smallest SHA-1, ties by key. Depends only on the key
    strings: not on their order, not on a seed, not on anyone's choice."""
    uniq = sorted(set(keys), key=lambda k: (hash_key(k), k))
    return uniq[:n]


# -- century -----------------------------------------------------------------

_YEAR4 = re.compile(r"(?<!\d)(19|20)(\d{2})(?!\d)")


def _centuries(text, yy):
    return {int(m.group(1)) for m in _YEAR4.finditer(text or "") if int(m.group(2)) == yy}


def resolve_century(yy, folder_name="", info_text=""):
    """
    yy is a two-digit year from the catalog. Returns (year, how) where how is
    "folder", "info" or "inferred" (the 00-26 -> 20xx rule, otherwise 19xx).
    A four-digit yy is returned as it is ("written").
    """
    if yy >= 100:
        return yy, "written"
    for how, text in (("folder", folder_name), ("info", info_text)):
        cs = _centuries(text, yy)
        if len(cs) == 1:
            return cs.pop() * 100 + yy, how
    return (2000 + yy if yy <= 26 else 1900 + yy), "inferred"


# -- catalog date --------------------------------------------------------------

_CAT_DATE = re.compile(r"(?<!\d)(\d{1,2}|\?\?|xx|XX)\s*[-/.]\s*(\d{1,2}|\?\?|xx|XX)\s*[-/.]\s*(\d{4}|\d{2})(?!\d)")


def parse_catalog_date(text):
    """'MM-DD-YY' -> (month, day, yy) with 00 / ?? as None; None when it is not a date.
    yy may be a four-digit year when the page wrote one."""
    m = _CAT_DATE.search(text or "")
    if not m:
        return None

    def part(s, hi):
        if not s.isdigit():
            return None
        v = int(s)
        return None if v == 0 else (v if v <= hi else False)

    mo, d = part(m.group(1), 12), part(m.group(2), 31)
    if mo is False or d is False:
        return None
    return mo, d, int(m.group(3))


# -- text files ----------------------------------------------------------------

def decode_text(raw):
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", "replace").lstrip("﻿")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", "replace").lstrip("﻿")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def is_checksum_text(text):
    """The same content test Trellis applies to a .txt (ingest._sniff_fingerprint_type)."""
    from app.utils import ingest
    from app.utils.checksums import parse_checksum_file
    lines = [ln for ln in (l.strip() for l in (text or "").splitlines()) if ln]
    if len(lines) < ingest._FP_SNIFF_MIN_LINES:
        return False
    entries = parse_checksum_file(text)
    return len(entries) >= ingest._FP_SNIFF_MIN_LINES and len(entries) >= ingest._FP_SNIFF_RATIO * len(lines)


def checksum_name(name):
    from app.utils import ingest
    low = name.lower()
    return any(m in low for m in ingest.FINGERPRINT_MARKERS)


def rank_text_files(entries):
    """entries: [{name, size}] .txt candidates. Best first, by Trellis's own filename score;
    checksum-looking names and over-size files are dropped."""
    from app.utils import ingest
    keep = [e for e in entries
            if e["name"].lower().endswith(".txt") and not checksum_name(e["name"])
            and (e.get("size") is None or e["size"] <= INFO_MAX_BYTES)]
    return sorted(keep, key=lambda e: (-ingest._score_text_file(e["name"]), e["name"].lower()))


def pick_info_text(candidates, fetch_text, max_tries=3):
    """First ranked candidate whose text is not a checksum list. fetch_text(entry) -> str|None.
    Returns (entry, text) or (None, "")."""
    for e in rank_text_files(candidates)[:max_tries]:
        text = fetch_text(e)
        if text and text.strip() and not is_checksum_text(text):
            return e, text
    return None, ""


# -- progress and output ---------------------------------------------------------

def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class Progress:
    def __init__(self, label, total, out=None, every=5, clock=time.monotonic):
        self.label, self.total, self.out, self.every = label, total, out or sys.stdout, every
        self._clock, self._t0 = clock, clock()
        self.done = 0

    def tick(self, fetcher, extra=""):
        self.done += 1
        if self.done % self.every and self.done != self.total and self.done != 1:
            return
        el = self._clock() - self._t0
        eta = el / self.done * (self.total - self.done) if self.done else 0
        print(f"[{self.done:>5}/{self.total}] {self.label}  requests {fetcher.requests}  "
              f"cached {fetcher.cache_hits}  elapsed {fmt_duration(el)}  eta {fmt_duration(eta)}"
              f"{('  ' + extra) if extra else ''}", file=self.out, flush=True)


def write_corpus(out_dir, tier, items, meta):
    """g3.jsonl via a temp file (an aborted run leaves the previous file alone), then the meta."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{tier.lower()}.jsonl"
    tmp = path.with_suffix(".jsonl.partial")
    with open(tmp, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    meta = dict(meta, tier=tier, n=len(items), date=datetime.now().strftime("%Y-%m-%d"))
    (out / f"{tier.lower()}.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                                   encoding="utf-8")
    return path


def plan_text(title, rows, requests_left, note=""):
    secs = requests_left * AVG_REQUEST_SECONDS
    lines = [title] + [f"  {k:<34}{v}" for k, v in rows]
    lines.append(f"  {'network requests still needed':<34}{requests_left}")
    lines.append(f"  {'at one per ~' + format(AVG_REQUEST_SECONDS, '.2f') + ' s':<34}{fmt_duration(secs)}")
    if note:
        lines.append("  " + note)
    return "\n".join(lines)
