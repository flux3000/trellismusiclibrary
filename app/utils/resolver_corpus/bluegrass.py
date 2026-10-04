"""
app/utils/resolver_corpus/bluegrass.py -- the G3 collector.

bluegrassarchive.com is a frameset over gdarchive.net/Bluegrass/ (the owner approved
this use). Four catalog pages (blue_AF, blue_GL, blue_MR, blue_SZ) hold one table per
artist, one row per show:  Date (MM-DD-YY) | Venue | Source. Almost no row links to its show
folder; the artist heading links to /AppData/<Artist>/ instead. So: sample ROWS by hash, list the
artist folders those rows belong to, match each row to the one subfolder carrying its date
(two candidates = unmatched), then read that show's listing and info text.
The catalog is the answer key: written by the site owner, separately from the taper's
text. Per sampled show we read the folder listing and one info text (<= 300 KB). No audio.

Run: python3 -m app.utils.resolver_corpus g3 [--limit N] [--resume] [--plan]
"""
import html
import re
import urllib.parse
from html.parser import HTMLParser

from . import common
from .polite import BotProtection, FetchError, RequestCap

CATALOG_PAGES = ("blue_AF.htm", "blue_GL.htm", "blue_MR.htm", "blue_SZ.htm")
CATALOG_BASES = ("https://gdarchive.net/Bluegrass/", "https://bluegrassarchive.com/")
SAMPLE_RULE = ("the N catalog rows with the smallest SHA-1 of artist URL|date|venue|source "
               "(no seed, no hand picking)")
AUDIO_EXT = (".flac", ".shn", ".mp3", ".wav", ".aif", ".aiff", ".ogg", ".m4a", ".ape", ".wv")
EST_REQUESTS_PER_SHOW = 2.1          # one listing, about 1.1 text files


def _clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "").replace("\xa0", " ")).strip()


# -- catalog -------------------------------------------------------------------

def _frames_of(page_html):
    p = _CatalogParser()
    p.feed(page_html)
    return p.frames


_ARTIST_DIR = re.compile(r"/AppData/[^/]+/?$", re.I)


class _CatalogParser(HTMLParser):
    """Linear walk, tolerant of FrontPage-style HTML (unclosed <tr>/<td>). A <tr> whose first
    cell is a dated link is a show row; the artist is the text of the artist-folder link before
    its table (else the last heading text, or a one-cell row)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.heading = ""
        self.frames = []
        self.stats = {"rows_seen": 0, "rows_no_link": 0, "rows_bad_date": 0}
        self._row_stack = []          # [{"cells": [(text, href)], "cell": None, "depth": n}]
        self._skip = 0
        self._depth = 0               # table nesting
        self._locked = False          # an artist-folder link named the heading; plain text may not replace it
        self._anchor = None           # an <a> outside any row: {"href", "text"}
        self.heading_href = None
        self.all_rows = []            # every dated row, linked or not (most have no folder link)

    def _end_cell(self, row):
        if row["cell"] is not None:
            row["cells"].append((_clean("".join(row["cell"]["text"])), row["cell"]["href"]))
            row["cell"] = None

    def _close_rows(self, depth):
        while self._row_stack and self._row_stack[-1]["depth"] >= depth:
            row = self._row_stack.pop()
            self._end_cell(row)
            self._finish_row(row)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "title"):
            self._skip += 1
        elif tag == "table":
            if self._row_stack and self._row_stack[-1]["cell"] is not None:
                # A table inside a layout cell: text before it in that cell is a heading
                # candidate (the artist name) and must not be lost to the outer cell.
                cell = self._row_stack[-1]["cell"]
                frags = [f for f in (_clean(x) for x in cell["text"]) if f]
                if frags and len(frags[-1]) <= 100:
                    self.heading = frags[-1]
                cell["text"] = []
            self._depth += 1
        elif tag in ("frame", "iframe") and a.get("src"):
            self.frames.append(a["src"])
        elif tag == "tr":
            self._close_rows(self._depth)               # an unclosed <tr> ends at the next one
            self._row_stack.append({"cells": [], "cell": None, "depth": self._depth})
        elif tag in ("td", "th") and self._row_stack:
            row = self._row_stack[-1]
            self._end_cell(row)                          # an unclosed <td> ends at the next cell
            row["cell"] = {"text": [], "href": None}
        elif tag == "a":
            if self._row_stack and self._row_stack[-1]["cell"] is not None:
                cell = self._row_stack[-1]["cell"]
                if a.get("href") and cell["href"] is None:
                    cell["href"] = a["href"]
            elif not self._row_stack and a.get("href"):
                self._anchor = {"href": a["href"], "text": []}
        elif tag == "br" and self._row_stack and self._row_stack[-1]["cell"] is not None:
            self._row_stack[-1]["cell"]["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._row_stack:
            self._end_cell(self._row_stack[-1])
        elif tag == "tr" and self._row_stack:
            self._close_rows(self._depth) if self._row_stack[-1]["depth"] >= self._depth else None
        elif tag == "table":
            self._close_rows(self._depth)
            self._depth = max(0, self._depth - 1)
            self._locked = False                         # the next artist names itself
        elif tag == "a" and self._anchor is not None:
            href, text = self._anchor["href"], _clean("".join(self._anchor["text"]))
            self._anchor = None
            if text and _ARTIST_DIR.search(urllib.parse.urlsplit(href).path):
                self.heading, self._locked, self.heading_href = text, True, href

    def handle_data(self, data):
        if self._skip:
            return
        if self._row_stack and self._row_stack[-1]["cell"] is not None:
            self._row_stack[-1]["cell"]["text"].append(data)
        elif not self._row_stack:
            if self._anchor is not None:
                self._anchor["text"].append(data)
            t = _clean(data)
            if t and len(t) <= 100 and not self._locked and self._anchor is None:
                self.heading = t

    def _finish_row(self, row):
        cells = row["cells"]
        if not cells:
            return
        text0, href0 = cells[0]
        parsed = common.parse_catalog_date(text0)
        if parsed and len(cells) >= 2:
            self.all_rows.append({"artist": self.heading, "artist_href": self.heading_href,
                                  "date_raw": text0, "href": href0,
                                  "venue_raw": cells[1][0], "source_raw": cells[2][0] if len(cells) > 2 else ""})
        if href0 and parsed:
            self.stats["rows_seen"] += 1
            self.rows.append({"artist": self.heading, "date_raw": text0, "href": href0,
                              "venue_raw": cells[1][0] if len(cells) > 1 else "",
                              "source_raw": cells[2][0] if len(cells) > 2 else ""})
            return
        if href0 and re.search(r"\d", text0) and len(cells) >= 2:
            self.stats["rows_bad_date"] += 1
            return
        if not href0 and parsed and len(cells) >= 2:
            self.stats["rows_no_link"] += 1
            return
        filled = [t for t, _ in cells if t]
        if len(filled) == 1 and len(filled[0]) <= 100 and not common.parse_catalog_date(filled[0]):
            self.heading = filled[0]


def _heading_clean(s):
    return re.sub(r"\s*[:\-]+\s*$", "", s or "").strip()


def norm_folder_url(href, base):
    """Absolute, https for the archive's own hosts, percent-encoding normalised, one
    trailing slash, no query or fragment (the ?C= sort links are never a folder)."""
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, href.strip()))
    scheme = "https" if p.hostname and p.hostname.lower().endswith(("gdarchive.net", "bluegrassarchive.com")) else p.scheme
    path = urllib.parse.quote(urllib.parse.unquote(p.path), safe="/:@&=+$,;~()'!*[]")
    if not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit((scheme, p.netloc.lower(), path, "", ""))


def decode_page(raw):
    return common.decode_text(raw)


def parse_catalog(page_html, base_url):
    """-> (shows, stats, frames). shows: dicts with artist, date_raw, venue_raw, source_raw,
    folder_url, catalog_page. Rows pointing at a page rather than a folder are counted, not kept."""
    p = _CatalogParser()
    p.feed(page_html)
    p.close()
    shows, stats = [], dict(p.stats, rows_not_folder=0)
    for r in p.rows:
        url = norm_folder_url(r["href"], base_url)
        last = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]).lower()
        if last.endswith((".htm", ".html", ".php", ".asp") + AUDIO_EXT):
            stats["rows_not_folder"] += 1
            continue
        shows.append({"artist": _heading_clean(r["artist"]), "date_raw": r["date_raw"],
                      "venue_raw": r["venue_raw"], "source_raw": r["source_raw"],
                      "folder_url": url, "catalog_page": base_url.rsplit("/", 1)[-1]})
    return shows, stats, p.frames


def catalog_rows(page_html):
    """Every dated row of a catalog page, linked or not: [{artist, artist_href, date_raw, href|None,
    venue_raw, source_raw}]. In the live pages almost no row links to its show folder; the artist
    heading links to /AppData/<Artist>/."""
    p = _CatalogParser()
    p.feed(page_html)
    p.close()
    return p.all_rows


def catalog_urls():
    return [[b + page for b in CATALOG_BASES] for page in CATALOG_PAGES]


def _rows_of_page(fetcher, variants):
    for url in variants:
        res = fetcher.get(url, max_bytes=8 * 1024 * 1024)
        if not res.ok:
            continue
        text = decode_page(res.body)
        rows = catalog_rows(text)
        if not rows:                                     # a frameset: follow its frames once
            for fr in _frames_of(text):
                fres = fetcher.get(urllib.parse.urljoin(url, fr), max_bytes=8 * 1024 * 1024)
                if fres.ok:
                    rows += catalog_rows(decode_page(fres.body))
        if rows:
            return rows, url
    return None, variants[0]


def row_key(r):
    return f"{r['artist_url'] or r['artist']}|{r['date_raw']}|{r['venue_raw']}|{r['source_raw']}"


def fetch_catalog(fetcher, log=print):
    """The four catalog pages (cached after the first fetch) -> (rows, stats). One row per
    dated line; the live pages link almost none of them to a show folder, only the artist
    heading links (to /AppData/<Artist>/)."""
    rows, seen = [], set()
    stats = {"pages": 0, "rows": 0, "duplicates": 0, "rows_with_show_link": 0}
    for variants in catalog_urls():
        page_rows, url = _rows_of_page(fetcher, variants)
        if page_rows is None:
            log(f"! no rows parsed from {variants[0]}")
            continue
        stats["pages"] += 1
        page = url.rsplit("/", 1)[-1]
        for r in page_rows:
            href = r.pop("href", None)
            r["artist_url"] = norm_folder_url(r.pop("artist_href"), url) if r.get("artist_href") else None
            r.pop("artist_href", None)
            r["folder_url"] = None
            if href:
                u = norm_folder_url(href, url)
                last = urllib.parse.unquote(urllib.parse.urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1]).lower()
                if not last.endswith((".htm", ".html", ".php", ".asp") + AUDIO_EXT):
                    r["folder_url"] = u
            r["artist"] = _heading_clean(r["artist"])
            r["catalog_page"] = page
            k = row_key(r)
            if k in seen:
                stats["duplicates"] += 1
                continue
            seen.add(k)
            stats["rows"] += 1
            stats["rows_with_show_link"] += bool(r["folder_url"])
            rows.append(r)
    return rows, stats


# -- matching a row to a show folder ----------------------------------------------------

_TRIPLE = re.compile(r"(?<!\d)(\d{2,4})[-._](\d{1,2})[-._](\d{2,4})(?!\d)")
_EIGHT = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")


def folder_dates(name):
    """Every (year-or-yy, month, day) a folder name could be stating, both MM-DD-YY and
    YY-MM-DD / YYYY-MM-DD readings. Years keep their written width."""
    out = set()
    for m in _TRIPLE.finditer(name):
        x, y, z = m.group(1), m.group(2), m.group(3)
        if len(x) == 4 and len(z) <= 2:
            out.add((int(x), int(y), int(z)))
        elif len(z) == 4 and len(x) <= 2:
            out.add((int(z), int(x), int(y)))
        elif len(x) == 2 and len(z) == 2:
            out.add((int(x), int(y), int(z)))          # YY-MM-DD
            out.add((int(z), int(x), int(y)))          # MM-DD-YY
    for m in _EIGHT.finditer(name):
        out.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return out


def folder_matches(name, month, day, yy):
    if not month or not day:
        return False
    for y, mo, d in folder_dates(name):
        if mo == month and d == day and (y % 100 == yy % 100) and (y < 100 or yy < 100 or y == yy):
            return True
    return False


def match_folder(row, artist_entries, date_counts):
    """-> (folder_url or None, reason). Only a single folder on that date counts; two folders
    on one date, or two catalog rows for one artist and date, is ambiguous and matches nothing."""
    parsed = common.parse_catalog_date(row["date_raw"])
    mo, d, yy = parsed
    if not mo or not d:
        return None, "partial_date"
    if date_counts.get((row["artist_url"], row["date_raw"]), 0) > 1:
        return None, "ambiguous_rows"
    hits = [e for e in artist_entries if e["is_dir"] and folder_matches(e["name"], mo, d, yy)]
    if len(hits) == 1:
        return norm_folder_url(hits[0]["url"], row["artist_url"]), None
    return None, "ambiguous_folders" if hits else "no_folder"


def date_counts_of(rows):
    c = {}
    for r in rows:
        k = (r["artist_url"], r["date_raw"])
        c[k] = c.get(k, 0) + 1
    return c


# -- Apache autoindex ---------------------------------------------------------------

_LINK = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>([^\n]{0,300})', re.S | re.I)
_SIZE = re.compile(r"^(?:[\d.]+[KMGT]?|-)$")


def _size_bytes(token):
    m = re.fullmatch(r"([\d.]+)([KMGT]?)", token or "")
    if not m:
        return None
    try:
        return int(float(m.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)])
    except ValueError:
        return None


def parse_listing(dir_url, page_html):
    """Entries strictly inside dir_url: [{name, url, is_dir, size}]. Drops the Parent
    Directory link and every ?C=..;O=.. sort link (same page, different query)."""
    cur = urllib.parse.urlsplit(dir_url)
    cur_path = cur.path if cur.path.endswith("/") else cur.path + "/"
    out = []
    for m in _LINK.finditer(page_html):
        href, _text, rest = m.groups()
        if "?" in href or href.startswith("#"):
            continue
        absu = urllib.parse.urljoin(dir_url, href)
        p = urllib.parse.urlsplit(absu)
        if p.netloc.lower() != cur.netloc.lower() or not p.path.startswith(cur_path) or p.path == cur_path:
            continue
        is_dir = href.endswith("/")
        tokens = re.sub(r"<[^>]+>", " ", rest).split()
        size_tok = tokens[-1] if tokens and _SIZE.match(tokens[-1]) else None
        name = urllib.parse.unquote(p.path[len(cur_path):]).rstrip("/")
        if "/" in name:
            continue
        out.append({"name": name, "url": absu, "is_dir": is_dir,
                    "size": None if is_dir else _size_bytes(size_tok)})
    return out


def listing_summary(entries):
    files = [e for e in entries if not e["is_dir"]]
    return {"n_audio": sum(1 for e in files if e["name"].lower().endswith(AUDIO_EXT)),
            "n_subdirs": sum(1 for e in entries if e["is_dir"]),
            "txt": [e for e in files if e["name"].lower().endswith(".txt")]}


# -- one show ---------------------------------------------------------------------------

def folder_name_of(folder_url):
    return urllib.parse.unquote(urllib.parse.urlsplit(folder_url).path.rstrip("/").rsplit("/", 1)[-1])


def artist_dir_of(folder_url):
    parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(folder_url).path.strip("/").split("/")]
    if "AppData" in parts and parts.index("AppData") + 1 < len(parts) - 1:
        return parts[parts.index("AppData") + 1]
    return None


def item_id(folder_url):
    return "g3-" + common.hash_key(folder_url)[:12]


def build_item(show, info_text, info_name, summary):
    """The corpus line for one show. Pure: no network."""
    folder = folder_name_of(show["folder_url"])
    parsed = common.parse_catalog_date(show["date_raw"])
    mo, d, yy = parsed
    year, how = common.resolve_century(yy, folder, info_text)
    date = {"y": year, "m": mo, "d": d}
    adir = artist_dir_of(show["folder_url"])
    return {
        "id": item_id(show["folder_url"]),
        "info_text": info_text,
        "folder_name": folder,
        "n_audio": summary["n_audio"],
        "archive_meta": {
            "tier": "G3", "folder_url": show["folder_url"], "catalog_page": show["catalog_page"],
            "catalog_date": show["date_raw"], "catalog_venue": show["venue_raw"],
            "catalog_source": show["source_raw"], "artist_dir": adir,
            "century": how, "century_inferred": how == "inferred",
            "info_file": info_name, "n_txt": len(summary["txt"]), "n_subdirs": summary["n_subdirs"],
        },
        "gold": {
            "artist": show["artist"] or adir or None,
            "date": date,
            "venue_raw": show["venue_raw"] or None,
            "source": show["source_raw"] or None,
        },
    }


def process_show(fetcher, show):
    """-> (item or None, reason). reason is None on success, else why the show was skipped."""
    res = fetcher.get(show["folder_url"], max_bytes=2 * 1024 * 1024)
    if not res.ok:
        return None, "no_listing"
    entries = parse_listing(show["folder_url"], decode_page(res.body))
    summary = listing_summary(entries)
    if not summary["txt"]:
        return None, "no_txt"

    def fetch_text(e):
        r = fetcher.get(e["url"], max_bytes=common.INFO_MAX_BYTES)
        return common.decode_text(r.body) if r.ok else None

    entry, text = common.pick_info_text(summary["txt"], fetch_text)
    if entry is None:
        return None, "no_info_text"
    return build_item(show, text, entry["name"], summary), None


def artist_entries(fetcher, artist_url, memo):
    """Subfolders of an artist folder, one fetch per artist (memo + the disk cache)."""
    if artist_url not in memo:
        res = fetcher.get(artist_url, max_bytes=4 * 1024 * 1024)
        memo[artist_url] = parse_listing(artist_url, decode_page(res.body)) if res.ok else None
    return memo[artist_url]


def process_row(fetcher, row, date_counts, memo):
    """Row -> (item or None, reason)."""
    used = memo.setdefault("_used", set())
    folder = row["folder_url"]
    if not folder:
        if not row["artist_url"]:
            return None, "no_artist_folder"
        entries = artist_entries(fetcher, row["artist_url"], memo)
        if entries is None:
            return None, "no_artist_folder"
        folder, why = match_folder(row, entries, date_counts)
        if not folder:
            return None, why
    if folder in used:
        return None, "duplicate_folder"                  # two rows, one folder: keep the first
    used.add(folder)
    return process_show(fetcher, dict(row, folder_url=folder))


# -- plan and run ------------------------------------------------------------------------

def select_sample(rows, n, limit=None):
    by_key = {row_key(r): r for r in rows if common.parse_catalog_date(r["date_raw"])}
    keys = common.stable_sample(by_key, n)
    if limit:
        keys = keys[:limit]
    return [by_key[k] for k in keys]


def plan(fetcher, sample_n=common.SAMPLE_SIZE, limit=None):
    pages = [v[0] for v in catalog_urls()]
    cached = [u for u in pages if fetcher.is_cached(u)]
    rows_out = [("catalog pages cached", f"{len(cached)} of {len(pages)}")]
    reqs = len(pages) - len(cached)
    n = min(sample_n, limit) if limit else sample_n
    if len(cached) < len(pages):
        rows_out.append(("rows to sample", f"{n} (catalog not fetched yet)"))
        rows_out.append(("artist folder listings", "about 600 (unknown until the catalog is read)"))
        reqs += 600 + int(round(n * EST_REQUESTS_PER_SHOW))
        return common.plan_text("G3 plan (nothing fetched)", rows_out, reqs,
                                "estimate: ~600 artist listings, then up to 2.1 requests per sampled row")
    rows, st = fetch_catalog(fetcher, log=lambda *_: None)
    sample = select_sample(rows, sample_n, limit)
    counts = date_counts_of(rows)
    memo, left_artists, left_shows = {}, 0, 0.0
    seen_artists = set()
    for r in sample:
        a = r["artist_url"]
        if not r["folder_url"] and a and a not in seen_artists:
            seen_artists.add(a)
            if not fetcher.is_cached(a):
                left_artists += 1
    for r in sample:
        if r["folder_url"]:
            left_shows += 0 if fetcher.is_cached(r["folder_url"]) else EST_REQUESTS_PER_SHOW
            continue
        a = r["artist_url"]
        if not a:
            continue
        if fetcher.is_cached(a):
            entries = artist_entries(fetcher, a, memo)
            if entries is None:
                continue
            f, _ = match_folder(r, entries, counts)
            if f:
                left_shows += 0 if fetcher.is_cached(f) else EST_REQUESTS_PER_SHOW
        else:
            left_shows += EST_REQUESTS_PER_SHOW        # upper bound: assume it matches
    rows_out += [("rows in the catalog", str(st["rows"])),
                 ("rows to sample", str(len(sample))),
                 ("artist listings still needed", str(left_artists)),
                 ("show requests (upper bound)", str(int(round(left_shows))))]
    return common.plan_text("G3 plan (nothing fetched)", rows_out, reqs + left_artists + int(round(left_shows)),
                            "upper bound: rows that match no folder cost only the artist listing")


def collect(fetcher, out_dir, sample_n=common.SAMPLE_SIZE, limit=None, log=print):
    rows, cstats = fetch_catalog(fetcher, log)
    if not rows:
        raise FetchError("no rows parsed from any catalog page")
    sample = select_sample(rows, sample_n, limit)
    counts = date_counts_of(rows)

    def _k(x):
        return re.sub(r"[^a-z0-9]", "", (x or "").lower())
    log(f"catalog: {cstats['rows']} rows, {len({r['artist'] for r in rows})} artists; "
        f"{len({r['artist_url'] for r in sample})} artist folders for the {len(sample)} sampled rows")
    items, skipped, errors, memo = [], {}, 0, {}
    prog = common.Progress("g3 rows", len(sample))
    consecutive, stopped, fatal = 0, None, None
    for r in sample:
        try:
            item, why = process_row(fetcher, r, counts, memo)
            consecutive = 0
        except RequestCap as e:
            stopped = str(e)
            break
        except BotProtection as e:
            stopped, fatal = str(e), e
            break
        except FetchError as e:
            item, why, errors = None, "fetch_error", errors + 1
            consecutive += 1
            log(f"! {e}")
            if consecutive >= 5:
                stopped = "5 fetch errors in a row"
                break
        if item:
            items.append(item)
        else:
            skipped[why] = skipped.get(why, 0) + 1
        prog.tick(fetcher, f"kept {len(items)}")
    inferred = sum(1 for i in items if i["archive_meta"]["century_inferred"])
    meta = {
        "source": "bluegrassarchive.com catalog (gdarchive.net/Bluegrass/)",
        "sample_rule": SAMPLE_RULE, "sample_size": len(sample),
        "catalog_rows": cstats["rows"], "catalog_artists": len({r["artist"] for r in rows}),
        "catalog": cstats, "kept": len(items), "skipped": skipped, "fetch_errors": errors,
        "unmatched_rows": sum(v for k, v in skipped.items()
                              if k in ("no_folder", "ambiguous_folders", "ambiguous_rows", "partial_date", "no_artist_folder")),
        "century_inferred": inferred,
        "requests": fetcher.requests, "cache_hits": fetcher.cache_hits,
        "answer_key": "catalog row: date, venue string, source chain; venue scored as one string",
        "match_rule": "row -> artist folder -> the one subfolder whose name carries the row's date "
                      "(MM-DD-YY, YY-MM-DD or YYYY-MM-DD); two candidates or two rows on one date = unmatched",
        "note": "rows with no matched folder or no usable info text are not written (counted under skipped)",
        "complete": stopped is None, "stopped": stopped,
    }
    path = common.write_corpus(out_dir, "G3", items, meta)
    log(f"wrote {path} ({len(items)} items); skipped {skipped}"
        + (f"; STOPPED EARLY: {stopped}" if stopped else ""))
    if fatal:
        raise fatal
    return meta
