"""
Atlas builder (Resolver v2, chunk 4, 2026-10-03).

Builds assets/atlas/atlas.sqlite, the shipped reference database the reader
consults for venues, artists and events. Runs on Ryan's Mac, with a network
connection, in the project's .venv, before a release:

    python3 -m app.atlas.build                 # the full build
    python3 -m app.atlas.build --sample 2000   # a small build for a quick check
    python3 -m app.atlas.build --plan          # print what would be downloaded, then stop
    python3 -m app.atlas.build --check PATH    # run the sanity checks on a built file
    python3 -m app.atlas.build --wikidata-only # add or refresh Wikidata in the finished Atlas
    python3 -m app.atlas.build --offline --skip-wikidata   # rebuild from the cache folder alone, no network

Standard library only (urllib, lzma, tarfile, json, sqlite3, csv, zipfile). The
core engine and the test suite never run this; it is the only code in Trellis that
reads these sources, and only here, only on a build.

SOURCES AND LICENCES (each filtered to what its licence allows)

  MusicBrainz core data, CC0.  The per-entity JSON dumps at
    https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/<latest>/
    area, place, event, artist, release-group. Only core data is read: names,
    types, dates, areas, aliases (including search hints), member-of
    relationships, event relationships. Tags, genres, ratings and annotations
    are CC BY-NC-SA and are never read, never stored. Legal-name aliases are
    skipped. The directory listing is read at run time and the build stops, with
    the listing printed, if the layout is not what this file expects.

  Wikidata, CC0.  Venues and music festivals in three cached passes (see
    app/atlas/wikidata.py): ids by SPARQL, then labels, aliases, former official names
    with start and end years, located-in and coordinates through the Action API,
    50 items a call. A descriptive User-Agent and delays throughout. If Wikidata
    cannot be fetched the build finishes without it and meta records source_wikidata.
    To add it later to a finished Atlas:  python3 -m app.atlas.build --wikidata-only

  GeoNames, CC BY 4.0.  cities5000 (towns of 5,000 people or more: about 50,000
    places; cities15000 would miss the small-town venues folk festivals use),
    admin1CodesASCII, and the alternate names of those cities when the file is
    allowed (default on). Attribution is required: the app's About section and
    the website footer carry "Place data from GeoNames (geonames.org), CC BY 4.0."

WHICH ACTS (the Act subset)

  An MB artist enters the Atlas when it is a person, group, orchestra, choir or
  of no stated type (never a character, Various Artists or a "[unknown]"
  placeholder) AND at least one of:
      - it performs at any MB event, or
      - --min-release-groups N is above 0 and it appears in the artist credit of at
        least N release groups (default 0: events only; measured on Ryan's library
        the release-group tail of 320,000 acts changed the result for one recording
        and cost 135 MB. Release groups are still counted, for the popularity score).
  --musicians members (default for the API) brings each qualifying group's members as
  musicians, with their dates and instruments; --musicians none (the command line
  default) leaves the musician tables empty. All aliases except legal names are kept,
  search hints included, so "Pat Metheney" finds Pat Metheny.

  --sample implies "every sampled artist": the first N records of each dump
  rarely cross-reference, so the rule above would leave nothing.

WHICH PLACES

  Every MB place except studios and pressing plants (Venue, Concert hall / Theatre,
  Club, Stadium, Arena, Indoor arena, Religious building, Festival stage, Amphitheatre,
  Park, Educational institution, Other, no type, and any type MusicBrainz adds later). Wikidata venues
  join an MB place by its Wikidata link, else by name and city, else are added.
  One row per physical place; every name it ever carried is a place_name row with
  start and end years. Festivals are events (kind "festival") with one row per name.

DETERMINISM

  Same dumps in, same tables out: ids follow dump order and every query pages by
  a stable order. Only meta.build_date and the file's own timestamps differ
  between two builds of the same inputs.

Files
    --cache DIR   downloads live here (default ~/Workshop/dev/atlas-cache/), outside
                  the repo, resumable, and re-used by the next run
    --offline     use only what is already in --cache (the newest dated MusicBrainz folder,
                  geonames/); no request is made, a missing file stops the build and is named.
                  Wikidata is skipped (it only exists as network calls)
    --out PATH    default assets/atlas/atlas.sqlite
"""
import argparse
import collections
import csv
import io
import json
import lzma  # noqa: F401  (tarfile's xz reader needs it; imported so a missing module fails here, early)
import os
import re
import sqlite3
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import schema as S

BUILDER_VERSION = "1"
try:
    from version import __version__ as APP_VERSION
except Exception:  # noqa: BLE001
    APP_VERSION = "dev"
USER_AGENT = f"TrellisMusicLibrary-AtlasBuild/{APP_VERSION} (https://trellismusiclibrary.com)"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "assets" / "atlas" / "atlas.sqlite"
DEFAULT_CACHE = Path("~/Workshop/dev/atlas-cache").expanduser()

MB_BASE = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/"
MB_ENTITIES = ("area", "place", "event", "artist")
MB_OPTIONAL = ("release-group",)
GN_BASE = "https://download.geonames.org/export/dump/"

LICENCES = {
    "musicbrainz": "CC0 1.0 (core data only; tags, genres, ratings and annotations excluded)",
    "wikidata": "CC0 1.0",
    "geonames": "CC BY 4.0 (attribution: Place data from GeoNames (geonames.org), CC BY 4.0.)",
}

PLACE_EXCLUDED = {"Studio", "Pressing plant"}
PLACE_KINDS = {"Venue": "venue", "Stadium": "stadium", "Arena": "arena", "Indoor arena": "arena",
               "Religious building": "religious", "Other": "other", None: "venue", "": "venue",
               "Concert hall / Theatre": "hall", "Club": "club", "Festival stage": "stage",
               "Amphitheatre": "amphitheatre", "Park": "park", "Educational institution": "school"}


def place_kind(typ):
    """Our kind for an MB place type, or None for a type left out (studios, pressing plants)."""
    if typ in PLACE_EXCLUDED:
        return None
    return PLACE_KINDS.get(typ) or re.sub(r"[^a-z0-9]+", "_", str(typ).lower()).strip("_") or "venue"
ACT_KINDS = {"Person": "person", "Group": "group", "Orchestra": "orchestra", "Choir": "choir",
             None: "other", "": "other", "Other": "other"}
GROUP_TYPES = {"Group", "Orchestra", "Choir"}
SPECIAL_ARTISTS = {
    "89ad4ac3-39f7-470e-963a-56509c546377",   # Various Artists
    "125ec42a-7229-4250-afc5-e057484327fe",   # [unknown]
    "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",   # [no artist]
    "f731ccc4-e22a-43af-a747-64213329e088",   # [anonymous]
    "5be4c609-9afa-4ea0-910b-12ffb71e3821",   # [traditional]
}
MEMBER_MODIFIERS = {"original", "founder", "additional", "minor", "task", "current", "past", "temporary"}
AREA_RANK = {"Country": 0, "Subdivision": 1, "County": 2, "Municipality": 3, "City": 3,
             "Island": 3, "District": 4}
MAX_ALIASES = 20



# ── logging ──────────────────────────────────────────────────────────────────

_T0 = time.time()


def say(msg=""):
    el = time.time() - _T0
    print(f"[{int(el // 60):02d}:{int(el % 60):02d}] {msg}", flush=True)


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def h64(mbid):
    """A short, stable stand-in for an MBID: 8 bytes, so a million of them fit in memory."""
    return int(str(mbid).replace("-", "")[:16], 16)


# ── HTTP ─────────────────────────────────────────────────────────────────────

class BuildError(RuntimeError):
    pass


def _ssl_context():
    try:
        from app.utils.net import SSL_CONTEXT
        return SSL_CONTEXT
    except Exception:  # noqa: BLE001
        import ssl
        return ssl.create_default_context()


_CTX = None


def http_open(url, headers=None, method="GET", timeout=60):
    global _CTX
    if _CTX is None:
        _CTX = _ssl_context()
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX)


def http_text(url, headers=None, timeout=60, tries=4):
    last = None
    for i in range(tries):
        try:
            with http_open(url, headers, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(2 ** i)
    raise BuildError(f"cannot read {url}: {last}")


def remote_size(url):
    try:
        with http_open(url, method="HEAD", timeout=30) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def download(url, dest, label=None, tries=8):
    """Resumable download to `dest` (kept for the next run). Returns the path."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    label = label or dest.name
    total = remote_size(url)
    if dest.exists() and (total is None or dest.stat().st_size == total):
        say(f"{label}: already downloaded ({human(dest.stat().st_size)})")
        return dest
    part = dest.with_name(dest.name + ".part")
    for attempt in range(tries):
        have = part.stat().st_size if part.exists() else 0
        if total is not None and have > total:
            part.unlink()
            have = 0
        if total is not None and have == total:
            break
        try:
            headers = {"Range": f"bytes={have}-"} if have else {}
            with http_open(url, headers, timeout=60) as r:
                if have and r.status != 206:
                    have = 0                                    # server ignored Range: start over
                mode = "ab" if have else "wb"
                if total is None:
                    cl = r.headers.get("Content-Length")
                    total = (int(cl) + have) if cl else None
                last = time.time()
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - last > 5:
                            last = time.time()
                            pct = f" ({100 * have / total:.0f}%)" if total else ""
                            say(f"{label}: {human(have)}{' of ' + human(total) if total else ''}{pct}")
            if total is None or part.stat().st_size == total:
                break
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            say(f"{label}: interrupted ({e}); retry {attempt + 1}/{tries}")
            time.sleep(min(60, 2 ** attempt))
    else:
        raise BuildError(f"{label}: could not finish after {tries} tries; run again to resume")
    if total is not None and part.stat().st_size != total:
        raise BuildError(f"{label}: size {part.stat().st_size} != expected {total}; run again to resume")
    part.replace(dest)
    say(f"{label}: done ({human(dest.stat().st_size)})")
    return dest


# ── MusicBrainz dumps ────────────────────────────────────────────────────────

def parse_listing(html):
    """Names linked from an Apache-style directory listing."""
    out = []
    for href in re.findall(r'href="([^"]+)"', html or ""):
        if href.startswith(("?", "#", "..", "/")) and not re.search(r"\d{8}-\d{6}", href):
            continue
        out.append(urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]) + ("/" if href.endswith("/") else ""))
    return out


def discover_mb(base=MB_BASE, release=None, fetch=http_text):
    """-> (release dir name, {entity: url}, SHA256SUMS url or None). Fails with the listing printed."""
    top = fetch(base)
    dirs = sorted(n.rstrip("/") for n in parse_listing(top) if re.fullmatch(r"\d{8}-\d{6}/", n))
    pick = release
    if pick is None:
        try:
            cand = fetch(base + "LATEST").strip()
            if re.fullmatch(r"\d{8}-\d{6}", cand):
                pick = cand
        except BuildError:
            pass
        if pick is None and dirs:
            pick = dirs[-1]
    if not pick:
        raise BuildError("MusicBrainz layout changed: no dated dump folders in " + base
                         + "\nListing:\n" + "\n".join("  " + n for n in parse_listing(top)[:60]))
    url = f"{base}{pick}/"
    names = set(parse_listing(fetch(url)))
    need = [f"{e}.tar.xz" for e in MB_ENTITIES]
    missing = [n for n in need if n not in names]
    if missing:
        raise BuildError(f"MusicBrainz layout changed: {url} has no {', '.join(missing)}"
                         "\nListing:\n" + "\n".join("  " + n for n in sorted(names)[:80]))
    files = {e: f"{url}{e}.tar.xz" for e in MB_ENTITIES + MB_OPTIONAL if f"{e}.tar.xz" in names}
    return pick, files, (url + "SHA256SUMS" if "SHA256SUMS" in names else None)


def discover_cached(cache, release=None):
    """-> (release dir name, {entity: path}, None) from the download cache alone (--offline).
    The newest dated folder that holds every required dump, or `release` when given."""
    cache = Path(cache)
    dirs = sorted((d.name for d in cache.glob("*") if d.is_dir() and re.fullmatch(r"\d{8}-\d{6}", d.name)),
                  reverse=True) if cache.is_dir() else []
    if release:
        dirs = [release]
    need = [f"{e}.tar.xz" for e in MB_ENTITIES]
    for name in dirs:
        have = {f.name for f in (cache / name).glob("*.tar.xz")}
        if all(n in have for n in need):
            files = {e: str(cache / name / f"{e}.tar.xz") for e in MB_ENTITIES + MB_OPTIONAL
                     if f"{e}.tar.xz" in have}
            return name, files, None
    raise BuildError(f"--offline: no MusicBrainz folder in {cache} holds {', '.join(need)}"
                     + (f" (looked at {', '.join(dirs)})" if dirs else " (the cache has no dated folder)"))


def iter_tar_json(source, entity, limit=None):
    """
    Stream the records of one MB dump: `source` is a path or an open binary file,
    the archive an .tar.xz holding one JSON object per line in a file named for the
    entity (mbdump/artist). Nothing is loaded whole. Yields dicts; a line that is
    not JSON is counted in iter_tar_json.bad and skipped.
    """
    opener = (lambda: tarfile.open(str(source), mode="r|xz")) if isinstance(source, (str, os.PathLike)) \
        else (lambda: tarfile.open(fileobj=source, mode="r|xz"))
    n = 0
    with opener() as tf:
        for member in tf:
            if not member.isfile() or os.path.basename(member.name) != entity:
                continue
            f = tf.extractfile(member)
            for line in f:
                line = line.strip()
                if not line.startswith(b"{"):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    iter_tar_json.bad += 1
                    continue
                yield rec
                n += 1
                if limit and n >= limit:
                    return
                if n % 200000 == 0:
                    say(f"  {entity}: {n:,} records read")


iter_tar_json.bad = 0


# ── Wikidata ─────────────────────────────────────────────────────────────────
# The fetch lives in app/atlas/wikidata.py (two SPARQL-and-API passes, cached, resumable).

def _qid(uri):
    m = re.search(r"/(Q\d+)$", uri or "")
    return m.group(1) if m else None



# ── GeoNames ─────────────────────────────────────────────────────────────────

def parse_geonames_city(cols):
    """A cities5000/cities15000 row -> dict, or None."""
    if len(cols) < 15:
        return None
    try:
        return {"geonameid": int(cols[0]), "name": cols[1], "ascii": cols[2], "alts": cols[3],
                "lat": float(cols[4]), "lon": float(cols[5]), "cc": cols[8], "admin1": cols[10],
                "population": int(cols[14] or 0)}
    except ValueError:
        return None


def parse_admin1(lines):
    """admin1CodesASCII.txt -> {"US.NC": "North Carolina"}."""
    out = {}
    for ln in lines:
        c = ln.rstrip("\n").split("\t")
        if len(c) >= 2 and c[0]:
            out[c[0]] = c[1]
    return out


def iter_geonames_alt(fileobj, wanted):
    """alternateNamesV2 rows for `wanted` geonameids: (geonameid, name, kind)."""
    for ln in io.TextIOWrapper(fileobj, encoding="utf-8", newline=""):
        c = ln.rstrip("\n").split("\t")
        if len(c) < 4:
            continue
        try:
            gid = int(c[1])
        except ValueError:
            continue
        if gid not in wanted or c[2] not in ("", "en", "abbr"):
            continue
        if c[2] == "abbr" and False:
            continue
        hist = len(c) > 7 and c[7] == "1"
        yield gid, c[3], "former" if hist else "alias"


# ── sources ──────────────────────────────────────────────────────────────────

class Sources:
    """What the builder reads. RemoteSources downloads; tests use StaticSources."""
    sample = None
    has_release_groups = False
    wd_status = None             # "ok" | "partial" | "skipped" | "failed", set by wikidata()

    def mb(self, entity):
        raise NotImplementedError

    def wikidata(self):
        return None

    def geonames(self):
        return None

    def describe(self):
        return {}


class StaticSources(Sources):
    """Records handed in directly (tests and the fixture Atlas): no download, no parsing."""

    def __init__(self, areas=(), places=(), events=(), artists=(), release_groups=None,
                 wikidata=None, geonames=None, sample=None):
        self._mb = {"area": areas, "place": places, "event": events, "artist": artists,
                    "release-group": release_groups or ()}
        self.has_release_groups = release_groups is not None
        self._wd, self._gn, self.sample = wikidata, geonames, sample
        self.wd_status = "ok" if wikidata else "skipped"

    def mb(self, entity):
        return iter(self._mb[entity])

    def wikidata(self):
        return self._wd

    def geonames(self):
        return self._gn

    def describe(self):
        return {"source": "static"}


class RemoteSources(Sources):
    def __init__(self, cache, *, sample=None, skip_wikidata=False, release=None, release_groups=True,
                 geonames_set="cities5000", geonames_alt=True, delay=2.0, api_delay=1.0, refresh_wikidata=False,
                 offline=False):
        self.cache = Path(cache)
        self.offline = offline
        if offline:
            skip_wikidata = True
        self.sample = sample
        self.skip_wikidata = skip_wikidata
        self.delay, self.api_delay, self.refresh_wikidata = delay, api_delay, refresh_wikidata
        self.wd_status = "skipped" if skip_wikidata else None
        self.gn_set = geonames_set
        self.gn_alt = geonames_alt and not sample
        if offline:
            self.release, self.files, self.sums = discover_cached(self.cache, release=release)
        else:
            self.release, self.files, self.sums = discover_mb(release=release)
        self.has_release_groups = release_groups and "release-group" in self.files
        if release_groups and not self.has_release_groups:
            say("release-group dump not offered in this release: acts qualify on events alone")
        self._info = {"mb_release": self.release, "mb_base": MB_BASE,
                      "mb_files": {e: Path(str(u)).name for e, u in self.files.items()}}
        self._sums = None

    # -- planning --
    def plan(self):
        if self.offline:
            say(f"offline: MusicBrainz json-dumps release {self.release} from {self.cache}")
            for e, path in self.files.items():
                say(f"  {e:<26} {human(Path(path).stat().st_size):>10}   {path}")
            return 0
        say(f"MusicBrainz json-dumps release {self.release}")
        total = 0
        rows = []
        for e, url in self.files.items():
            if e == "release-group" and not self.has_release_groups:
                continue
            n = remote_size(url)
            rows.append((e, n, url))
            total += n or 0
        gn = [f"{self.gn_set}.zip", "admin1CodesASCII.txt"] + (["alternateNamesV2.zip"] if self.gn_alt else [])
        for g in gn:
            n = remote_size(GN_BASE + g)
            rows.append((g, n, GN_BASE + g))
            total += n or 0
        for name, n, url in rows:
            say(f"  {name:<26} {human(n) if n else '?':>10}   {url}")
        say(f"  {'total download':<26} {human(total):>10}   (Wikidata is separate, below)")
        if not self.skip_wikidata:
            from . import wikidata as WD
            say("  Wikidata: " + (WD.cached_estimate(self.cache, self.api_delay)
                                  or "ids first (about 20 SPARQL queries), then entity details 50 a call; "
                                     "the exact estimate prints after pass 1. Cached under the cache folder."))
        return total

    def _sha_for(self, name):
        if self.sums and self._sums is None:
            try:
                self._sums = {ln.split()[-1].lstrip("*"): ln.split()[0] for ln in http_text(self.sums).splitlines() if ln.strip()}
            except BuildError:
                self._sums = {}
        return (self._sums or {}).get(name)

    def _verify(self, path, name):
        want = self._sha_for(name)
        if not want:
            return
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise BuildError(f"{name}: checksum mismatch. Remove {path} and run again.")
        say(f"{name}: checksum ok")

    def mb(self, entity):
        url = self.files[entity]
        if self.sample and not self.offline:
            say(f"{entity}: streaming the first {self.sample:,} records (no download kept)")
            resp = http_open(url, timeout=120)
            try:
                yield from iter_tar_json(resp, entity, limit=self.sample)
            finally:
                resp.close()
            return
        path = self.cache / f"{self.release}" / f"{entity}.tar.xz"
        if self.offline:
            if not path.is_file():
                raise BuildError(f"--offline: {path} is not in the cache. Run once without --offline to download it")
            say(f"{entity}: reading from the cache")
            yield from iter_tar_json(path, entity, limit=self.sample)
            return
        fresh = not path.exists()
        download(url, path, label=entity)
        if fresh:
            self._verify(path, f"{entity}.tar.xz")
        say(f"{entity}: reading")
        yield from iter_tar_json(path, entity)

    def wikidata(self):
        if self.skip_wikidata:
            return None
        from . import wikidata as WD
        items, names, info = WD.fetch(self.cache, delay=self.delay, api_delay=self.api_delay,
                                      sample=self.sample, refresh=self.refresh_wikidata)
        self._info["wikidata"] = info
        self.wd_status = "partial" if (info["failed"] or info["failed_batches"]) else "ok"
        return items, names

    def geonames(self):
        d = self.cache / "geonames"
        if self.offline:
            need = [f"{self.gn_set}.zip", "admin1CodesASCII.txt"] + (["alternateNamesV2.zip"] if self.gn_alt else [])
            gone = [n for n in need if not (d / n).is_file()]
            if gone:
                raise BuildError(f"--offline: {', '.join(str(d / n) for n in gone)} not in the cache"
                                 + (" (--no-geonames-alt skips the alternate names)" if "alternateNamesV2.zip" in gone else ""))
            cities_zip, admin1 = d / f"{self.gn_set}.zip", d / "admin1CodesASCII.txt"
        else:
            cities_zip = download(GN_BASE + f"{self.gn_set}.zip", d / f"{self.gn_set}.zip", label=self.gn_set)
            admin1 = download(GN_BASE + "admin1CodesASCII.txt", d / "admin1CodesASCII.txt", label="admin1CodesASCII")
        with zipfile.ZipFile(cities_zip) as z:
            with z.open(f"{self.gn_set}.txt") as f:
                cities = [c for c in (parse_geonames_city(ln.rstrip("\n").split("\t"))
                                      for ln in io.TextIOWrapper(f, encoding="utf-8", newline="")) if c]
        if self.sample:
            cities = cities[:self.sample]
        a1 = parse_admin1(open(admin1, encoding="utf-8"))
        alt = None
        if self.gn_alt:
            alt_zip = (d / "alternateNamesV2.zip") if self.offline else \
                download(GN_BASE + "alternateNamesV2.zip", d / "alternateNamesV2.zip", label="alternateNamesV2")
            wanted = {c["geonameid"] for c in cities}

            def alt_iter():
                with zipfile.ZipFile(alt_zip) as z:
                    with z.open("alternateNamesV2.txt") as f:
                        yield from iter_geonames_alt(f, wanted)
            alt = alt_iter
        self._info["geonames"] = {"set": self.gn_set, "alternate_names": bool(alt), "base": GN_BASE}
        return {"cities": cities, "admin1": a1, "alt": alt}

    def describe(self):
        return dict(self._info)


# ── the builder ──────────────────────────────────────────────────────────────

class _Rows:
    """Buffered executemany for one insert statement."""

    def __init__(self, db, sql, size=20000):
        self.db, self.sql, self.size, self.buf = db, sql, size, []

    def add(self, row):
        self.buf.append(row)
        if len(self.buf) >= self.size:
            self.flush()

    def flush(self):
        if self.buf:
            self.db.executemany(self.sql, self.buf)
            self.buf = []


class AtlasBuilder:
    def __init__(self, path, *, sample=None, min_release_groups=0, force_bigram=False, musicians="members", log=say):
        self.path = Path(path)
        self.sample = sample
        self.min_rg = max(0, int(min_release_groups))
        if musicians not in ("members", "none"):
            raise BuildError("musicians must be 'members' or 'none'")
        self.musicians = musicians
        self.log = log
        self.bigram = force_bigram or not S.trigram_available()
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:                     # a folder that allows writes but not deletes: empty it in place
                open(self.path, "wb").close()
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("pragma journal_mode = off")
        self.db.execute("pragma synchronous = off")
        self.db.execute("pragma cache_size = -400000")
        self.db.executescript(S.DDL)
        self._setup()

    def _setup(self):
        self._next = collections.Counter()
        r = lambda sql: _Rows(self.db, sql)
        self.r_area = r("insert into area values (?,?,?,?,?,?,?,?,?,?,?,?)")
        self.r_area_name = r("insert into area_name(area_id,name,key,kind) values (?,?,?,?)")
        self.r_place = r("insert into place values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        self.r_place_name = r("insert into place_name(place_id,name,key,start_year,end_year,kind) values (?,?,?,?,?,?)")
        self.r_place_area = r("insert or ignore into place_area values (?,?)")
        self.r_act = r("insert into act values (?,?,?,?,?,?,?,?,?,?,?)")
        self.r_act_name = r("insert into act_name(act_id,name,key,ck,kind) values (?,?,?,?,?)")
        self.r_musician = r("insert into musician values (?,?,?)")
        self.r_musician_name = r("insert into musician_name(musician_id,name,key,kind) values (?,?,?,?)")
        self.r_member = r("insert into act_member values (?,?,?,?,?)")
        self.r_event = r("insert into event values (?,?,?,?,?,?,?,?,?,?)")
        self.r_event_act = r("insert or ignore into event_act values (?,?)")
        self.used_areas = set()      # MB areas a kept place or event sits in, with their ancestors
        self.areas = {}              # mbid -> dict
        self.area_chain_cache = {}
        self.place_by_mbid = {}      # mbid -> place id
        self.place_by_qid = {}
        self.place_meta = {}         # place id -> (city key, region key, country)
        self.event_perf = {}         # event id -> [(h64, role)]
        self.perf_count = collections.Counter()
        self.rg_count = collections.Counter()
        self.act_by_hash = {}
        self.musician_by_hash = {}
        self.person_stash = {}
        self.stats = collections.Counter()

    @classmethod
    def reopen(cls, path, log=say):
        """An existing Atlas, open for --wikidata-only: ids continue after what is there."""
        self = cls.__new__(cls)
        self.path, self.sample, self.min_rg, self.log = Path(path), None, 0, log
        self.musicians = "members"
        self.db = sqlite3.connect(str(path))
        self.db.execute("pragma journal_mode = off")
        self.db.execute("pragma synchronous = off")
        self.meta = dict(self.db.execute("select key, value from meta"))
        self.bigram = self.meta.get("fuzzy_mode") == "bigram"
        self._setup()
        return self

    def drop_wikidata(self):
        """Remove what a Wikidata run added: its places and their names, the names it merged
        into MusicBrainz places (every place_name above meta.wikidata_name_floor), its festivals."""
        db = self.db
        mine = "select id from place where source = 'wikidata'"
        n_places = db.execute(f"select count(*) from ({mine})").fetchone()[0]
        db.execute(f"delete from place_name where place_id in ({mine})")
        db.execute(f"delete from place_area where place_id in ({mine})")
        db.execute("delete from place where source = 'wikidata'")
        floor = self.meta.get("wikidata_name_floor")
        n_names = 0
        if floor is not None:
            n_names = db.execute("delete from place_name where id > ?", (int(floor),)).rowcount
        n_fest = db.execute("delete from event where mbid is null and kind like 'festival%'").rowcount
        self.log(f"wikidata rows removed: {n_places:,} places, {n_names:,} merged names, {n_fest:,} festival rows")
        for t in ("place", "event", "place_name"):
            self._next[t] = db.execute(f"select coalesce(max(id), 0) from {t}").fetchone()[0]
        self.place_by_qid = {q: i for i, q in db.execute("select id, wikidata from place where wikidata is not null")}
        self.place_meta = {i: (S.norm_key(c or ""), cc) for i, c, cc in db.execute("select id, city, country from place")}

    def reindex(self, kinds):
        for kind in kinds:
            for t in (f"vocab_{kind}", f"fts_{kind}", f"gram_df_{kind}", f"gram_{kind}"):
                self.db.execute(f"drop table if exists {t}")
        self._fuzzy_index(kinds)

    def nid(self, table):
        self._next[table] += 1
        return self._next[table]

    # ── areas ────────────────────────────────────────────────────────────────
    def add_areas(self, records):
        raw = {}
        for r in records:
            m, name = r.get("id"), (r.get("name") or "").strip()
            if not m or not name:
                continue
            iso = r.get("iso-3166-1-codes") or []
            parents = [((rel.get("area") or {}).get("id"), rel.get("direction"))
                       for rel in (r.get("relations") or [])
                       if rel.get("target-type") == "area" and rel.get("type") == "part of"]
            aliases = [(a.get("name"), "alias") for a in (r.get("aliases") or [])
                       if a.get("name") and a.get("type") != "Legal name"]
            raw[m] = {"name": name, "type": r.get("type"), "iso": iso[0] if iso else None,
                      "parents": parents, "aliases": aliases}
        self.areas = raw
        for m, a in raw.items():
            a["parent"] = self._pick_parent(m)
        for m, a in raw.items():
            aid = self.nid("area")
            a["id"] = aid
        for m, a in raw.items():
            chain = self._chain(m)
            a["country"] = next((raw[c]["iso"] for c in chain if raw[c]["iso"]), None)
            a["region"] = next((raw[c]["name"] for c in chain if raw[c]["type"] == "Subdivision"), None)
        self.log(f"areas read: {len(raw):,} (written at the end: only those a kept place or event sits in)")

    def finish_areas(self):
        """Write the MB areas a kept place or event is in, and their ancestors. The other ~100,000
        (towns with no venue and no show) are never looked up and cost 25 MB; GeoNames covers towns."""
        for m in list(self.used_areas):
            for c in self._chain(m):
                self.used_areas.add(c)
        kept = 0
        for m, a in self.areas.items():
            if m not in self.used_areas:
                continue
            parent = self.areas.get(a["parent"])
            self.r_area.add((a["id"], m, None, a["name"], (a["type"] or "area").lower(), a["country"], a["region"],
                             parent["id"] if parent else None, None, None, None, "musicbrainz"))
            self._add_area_names(a["id"], a["name"], a["aliases"])
            kept += 1
        self.r_area.flush()
        self.r_area_name.flush()
        self.log(f"areas: {kept:,} of {len(self.areas):,} kept")

    def _add_area_names(self, area_id, name, aliases):
        seen = {S.norm_key(name)}
        self.r_area_name.add((area_id, name, S.norm_key(name), "official"))
        for n, kind in aliases[:MAX_ALIASES]:
            k = S.norm_key(n)
            if k and k not in seen:
                seen.add(k)
                self.r_area_name.add((area_id, n, k, kind))

    def _pick_parent(self, m):
        a = self.areas[m]
        mine = AREA_RANK.get(a["type"], 5)
        cands = [(p, d) for p, d in a["parents"] if p in self.areas and p != m]
        back = [p for p, d in cands if d == "backward"]
        pool = back or [p for p, d in cands if AREA_RANK.get(self.areas[p]["type"], 5) < mine]
        pool = [p for p in pool if AREA_RANK.get(self.areas[p]["type"], 5) <= mine] or pool
        if not pool:
            return None
        return max(pool, key=lambda p: (AREA_RANK.get(self.areas[p]["type"], 5), p))

    def _chain(self, m):
        hit = self.area_chain_cache.get(m)
        if hit is not None:
            return hit
        out, seen, cur = [], set(), m
        while cur and cur in self.areas and cur not in seen and len(out) < 10:
            out.append(cur)
            seen.add(cur)
            cur = self.areas[cur].get("parent")
        self.area_chain_cache[m] = out
        return out

    # ── places ───────────────────────────────────────────────────────────────
    @staticmethod
    def _wikidata_of(rec):
        for rel in rec.get("relations") or []:
            if rel.get("type") == "wikidata" and rel.get("target-type") == "url":
                q = _qid(((rel.get("url") or {}).get("resource")) or "")
                if q:
                    return q
        return None

    def add_places(self, records):
        kept = 0
        for r in records:
            typ = r.get("type")
            m, name = r.get("id"), (r.get("name") or "").strip()
            kind = place_kind(typ)
            if not m or not name or kind is None:
                self.stats["place skipped"] += 1
                continue
            area_m = (r.get("area") or {}).get("id")
            chain = self._chain(area_m) if area_m in self.areas else []
            city, region, country = self._place_geo(chain)
            coords = r.get("coordinates") or {}
            try:
                lat, lon = float(coords.get("latitude")), float(coords.get("longitude"))
            except (TypeError, ValueError):
                lat = lon = None
            ls = r.get("life-span") or {}
            pid = self.nid("place")
            qid = self._wikidata_of(r)
            self.r_place.add((pid, m, qid, name, kind, self.areas[area_m]["id"] if chain else None,
                              city, region, country, lat, lon, S.year_of(ls.get("begin")), S.year_of(ls.get("end")),
                              "musicbrainz"))
            self.place_by_mbid[m] = pid
            if qid:
                self.place_by_qid[qid] = pid
            self.place_meta[pid] = (S.norm_key(city or ""), country)
            for c in chain:
                self.r_place_area.add((pid, self.areas[c]["id"]))
                self.used_areas.add(c)
            self._place_names(pid, name, r.get("aliases") or [])
            kept += 1
        for rows in (self.r_place, self.r_place_name, self.r_place_area):
            rows.flush()
        self.log(f"places: {kept:,} kept, {self.stats['place skipped']:,} skipped (studios and the like)")

    def _place_geo(self, chain):
        kinds = [(self.areas[c]["type"], self.areas[c]["name"]) for c in chain]
        city = next((n for t, n in kinds if t in ("City", "Municipality")), None) \
            or next((n for t, n in kinds if t in ("District", "Island")), None)
        region = next((n for t, n in kinds if t == "Subdivision"), None)
        country = next((self.areas[c]["iso"] for c in chain if self.areas[c]["iso"]), None)
        return city, region, country

    def _place_names(self, pid, name, aliases):
        have = {}
        self._put_place_name(have, pid, name, "official", None, None)
        for a in aliases[:MAX_ALIASES * 2]:
            n = (a.get("name") or "").strip()
            if not n or a.get("type") == "Legal name":
                continue
            s, e = S.year_of(a.get("begin")), S.year_of(a.get("end"))
            kind = "search_hint" if a.get("type") == "Search hint" else ("former" if (e or a.get("ended")) else "alias")
            self._put_place_name(have, pid, n, kind, s, e)

    def _put_place_name(self, have, pid, name, kind, s, e):
        k = S.norm_key(name)
        if not k:
            return
        key = (k, s, e)
        if k in have and (s, e) == (None, None):
            return
        if key in have:
            return
        have[key] = 1
        have.setdefault(k, 1)
        self.r_place_name.add((pid, name, k, s, e, kind))

    # ── events ───────────────────────────────────────────────────────────────
    def add_events(self, records):
        kept = 0
        for r in records:
            m, name = r.get("id"), (r.get("name") or "").strip()
            if not m or not name or r.get("cancelled"):
                continue
            ls = r.get("life-span") or {}
            begin, end = ls.get("begin"), ls.get("end")
            place_m = area_m = series = None
            perf = []
            for rel in r.get("relations") or []:
                tt, typ = rel.get("target-type"), rel.get("type")
                if tt == "place" and typ == "held at":
                    place_m = (rel.get("place") or {}).get("id")
                elif tt == "area" and typ == "held in":
                    area_m = (rel.get("area") or {}).get("id")
                elif tt == "artist" and (rel.get("artist") or {}).get("id"):
                    perf.append((h64(rel["artist"]["id"]), typ or "performer"))
                elif tt == "series":
                    series = (rel.get("series") or {}).get("name") or series
            if not begin and not perf:
                continue
            pid = self.place_by_mbid.get(place_m)
            aid = self.areas[area_m]["id"] if area_m in self.areas else None
            if aid is not None:
                self.used_areas.add(area_m)
            eid = self.nid("event")
            self.r_event.add((eid, m, name, S.norm_key(name), (r.get("type") or "").lower() or None,
                              begin, end, pid, aid, series))
            if perf:
                self.event_perf[eid] = perf
                for h in {h for h, _ in perf}:
                    self.perf_count[h] += 1
            kept += 1
        self.r_event.flush()
        self.log(f"events: {kept:,}")

    # ── release groups ───────────────────────────────────────────────────────
    def count_release_groups(self, records):
        n = 0
        for r in records:
            n += 1
            seen = set()
            for ac in r.get("artist-credit") or []:
                a = ac.get("artist") or {}
                if a.get("id") and a["id"] not in seen:
                    seen.add(a["id"])
                    self.rg_count[h64(a["id"])] += 1
        self.log(f"release groups read: {n:,} ({len(self.rg_count):,} credited artists)")

    # ── artists ──────────────────────────────────────────────────────────────
    def _qualifies(self, h):
        if self.sample:
            return True
        return self.perf_count.get(h, 0) > 0 or (self.min_rg > 0 and self.rg_count.get(h, 0) >= self.min_rg)

    @staticmethod
    def _is_special(r):
        n = (r.get("name") or "").strip()
        return (r.get("id") in SPECIAL_ARTISTS or not n or n.startswith("[") or n.lower() == "various artists"
                or r.get("type") == "Character")

    @staticmethod
    def _aliases(r):
        out = []
        for a in r.get("aliases") or []:
            n = (a.get("name") or "").strip()
            if not n or a.get("type") == "Legal name":
                continue
            out.append((n, "search_hint" if a.get("type") == "Search hint" else "alias"))
        return out

    def add_artists(self, records):
        total = kept = 0
        for r in records:
            total += 1
            if self._is_special(r):
                continue
            m = r.get("id")
            h = h64(m)
            typ = r.get("type")
            is_group = typ in GROUP_TYPES
            rels = r.get("relations") or []
            if self.musicians == "members" and typ == "Person" and any(x.get("type") == "member of band" for x in rels):
                self.person_stash[h] = ((r.get("name") or "").strip(), self._aliases(r))
            if not self._qualifies(h):
                continue
            kept += 1
            aid = self.nid("act")
            name = r["name"].strip()
            ls = r.get("life-span") or {}
            country = r.get("country") or (self.areas.get(((r.get("area") or {}).get("id")), {}) or {}).get("country")
            ne, nrg = self.perf_count.get(h, 0), self.rg_count.get(h, 0)
            self.r_act.add((aid, m, name, r.get("sort-name"), ACT_KINDS.get(typ, "other"),
                            S.year_of(ls.get("begin")), S.year_of(ls.get("end")), country, ne, nrg, 3 * ne + nrg))
            seen = set()

            def put(n, kind):
                k = S.norm_key(n)
                if k and k not in seen:
                    seen.add(k)
                    self.r_act_name.add((aid, n, k, S.act_core(n), kind))
            put(name, "official")
            for n, kind in self._aliases(r)[:MAX_ALIASES]:
                put(n, kind)
            if h in self.perf_count:
                self.act_by_hash[h] = aid
            for rel in (rels if self.musicians == "members" else ()):
                if rel.get("type") != "member of band" or rel.get("target-type") != "artist":
                    continue
                d = rel.get("direction")
                if not (d == "backward" or (d is None and is_group)):
                    continue
                person = rel.get("artist") or {}
                if not person.get("id") or not (person.get("name") or "").strip():
                    continue
                mid = self._musician(person)
                attrs = [a for a in (rel.get("attributes") or []) if a.lower() not in MEMBER_MODIFIERS]
                self.r_member.add((aid, mid, S.year_of(rel.get("begin")), S.year_of(rel.get("end")),
                                   ", ".join(attrs[:3]) or None))
            if total % 500000 == 0:
                self.log(f"  artists: {total:,} read, {kept:,} kept")
        for h, aid in self.act_by_hash.items():
            pass
        self.log(f"artists: {total:,} read, {kept:,} kept as acts")

    def _musician(self, person):
        h = h64(person["id"])
        mid = self.musician_by_hash.get(h)
        if mid is None:
            mid = self.nid("musician")
            self.musician_by_hash[h] = mid
            name = person["name"].strip()
            self.r_musician.add((mid, person["id"], name))
            self.r_musician_name.add((mid, name, S.norm_key(name), "official"))
            self._musician_seen = getattr(self, "_musician_seen", {})
            self._musician_seen[mid] = {S.norm_key(name)}
        return mid

    def finish_artists(self):
        """Musician aliases (from the person records seen), and the event-to-act links."""
        seen_all = getattr(self, "_musician_seen", {})
        for h, mid in self.musician_by_hash.items():
            stash = self.person_stash.get(h)
            if not stash:
                continue
            seen = seen_all[mid]
            for n, kind in stash[1][:MAX_ALIASES]:
                k = S.norm_key(n)
                if k and k not in seen:
                    seen.add(k)
                    self.r_musician_name.add((mid, n, k, kind))
        linked = 0
        for eid, perf in self.event_perf.items():
            for h, role in perf:
                aid = self.act_by_hash.get(h)
                if aid:
                    self.r_event_act.add((aid, eid))
                    linked += 1
        for rows in (self.r_act, self.r_act_name, self.r_musician, self.r_musician_name, self.r_member,
                     self.r_event_act):
            rows.flush()
        self.person_stash.clear()
        self.log(f"musicians: {len(self.musician_by_hash):,}; event-act links: {linked:,}")

    # ── Wikidata ─────────────────────────────────────────────────────────────
    def add_wikidata(self, items, names):
        self.r_place_name.flush()
        self.wd_floor = self.db.execute("select coalesce(max(id), 0) from place_name").fetchone()[0]
        by_qid_names = collections.defaultdict(list)
        for q, n, kind, s, e in names:
            by_qid_names[q].append((n, kind, s, e))
        by_key = collections.defaultdict(list)
        for pid, k in self.db.execute("select place_id, key from place_name where kind = 'official'"):
            by_key[k].append(pid)
        merged = added = fest = 0
        for q, it in items.items():
            if it["kind"] == "festival":
                self._add_festival(it, by_qid_names.get(q, []))
                fest += 1
                continue
            pid = self.place_by_qid.get(q)
            if pid is None:
                ck = S.norm_key(it["loc"] or "")
                for cand in by_key.get(S.norm_key(it["label"]), []):
                    city_key, country = self.place_meta.get(cand, ("", None))
                    if ck and city_key and ck == city_key and (not it["cc"] or not country or it["cc"] == country):
                        pid = cand
                        break
            have = {}
            if pid is None:
                pid = self.nid("place")
                self.r_place.add((pid, None, q, it["label"], "venue", None, it["loc"], None, it["cc"],
                                  it["lat"], it["lon"], it["inception"], it["dissolved"], "wikidata"))
                self.place_meta[pid] = (S.norm_key(it["loc"] or ""), it["cc"])
                self.place_by_qid[q] = pid
                self._put_place_name(have, pid, it["label"], "official", None, None)
                added += 1
            else:
                for _, nm, _k, s, e in self.db.execute(
                        "select place_id, name, kind, start_year, end_year from place_name where place_id = ?", (pid,)):
                    have[(S.norm_key(nm), s, e)] = 1
                    have.setdefault(S.norm_key(nm), 1)
                merged += 1
            for n, kind, s, e in by_qid_names.get(q, [])[:MAX_ALIASES * 2]:
                self._put_place_name(have, pid, n, kind, s, e)
        for rows in (self.r_place, self.r_place_name):
            rows.flush()
        self.log(f"wikidata: {added:,} venues added, {merged:,} merged into MusicBrainz places, {fest:,} festivals")

    def _add_festival(self, it, names):
        seen = set()
        for n, kind in [(it["label"], "official")] + [(n, "alias") for n, k, s, e in names if k != "former"] \
                + [(n, "former") for n, k, s, e in names if k == "former"]:
            k = S.norm_key(n)
            if not k or k in seen:
                continue
            seen.add(k)
            self.r_event.add((self.nid("event"), None, n, k, "festival" if kind == "official" else f"festival_{kind}",
                              None, None, None, None, it["label"]))
        self.r_event.flush()

    # ── GeoNames ─────────────────────────────────────────────────────────────
    def add_geonames(self, cities, admin1, alt=None):
        region_ids = {}
        for code, name in sorted(admin1.items()):
            cc = code.split(".")[0]
            aid = self.nid("area")
            region_ids[code] = aid
            self.r_area.add((aid, None, None, name, "region", cc, name, None, None, None, None, "geonames"))
            self.r_area_name.add((aid, name, S.norm_key(name), "official"))
        by_gid = {}
        for c in cities:
            aid = self.nid("area")
            by_gid[c["geonameid"]] = aid
            region = admin1.get(f"{c['cc']}.{c['admin1']}")
            self.r_area.add((aid, None, c["geonameid"], c["name"], "city", c["cc"], region,
                             region_ids.get(f"{c['cc']}.{c['admin1']}"), c["lat"], c["lon"], c["population"], "geonames"))
            seen = {S.norm_key(c["name"])}
            self.r_area_name.add((aid, c["name"], S.norm_key(c["name"]), "official"))
            extra = [c["ascii"]]
            if alt is None:
                extra += [a for a in c["alts"].split(",") if a.isascii()][:6]
            for n in extra:
                k = S.norm_key(n)
                if k and k not in seen:
                    seen.add(k)
                    self.r_area_name.add((aid, n, k, "alias"))
            c["_seen"] = seen
        if alt is not None:
            per = collections.Counter()
            seen_by = {c["geonameid"]: c["_seen"] for c in cities}
            for gid, n, kind in alt():
                aid = by_gid.get(gid)
                k = S.norm_key(n)
                if aid is None or not k or k in seen_by[gid] or per[gid] >= 10:
                    continue
                seen_by[gid].add(k)
                per[gid] += 1
                self.r_area_name.add((aid, n, k, kind))
        self.r_area.flush()
        self.r_area_name.flush()
        self.log(f"geonames: {len(cities):,} cities, {len(admin1):,} regions")

    # ── finishing ────────────────────────────────────────────────────────────
    def finish(self, meta):
        for rows in (self.r_area, self.r_area_name, self.r_place, self.r_place_name, self.r_place_area,
                     self.r_act, self.r_act_name, self.r_musician, self.r_musician_name, self.r_member,
                     self.r_event, self.r_event_act):
            rows.flush()
        self.log("indexes")
        self.db.executescript(S.INDEXES)
        self.log(f"fuzzy index ({'bigram table' if self.bigram else 'FTS5 trigram'})")
        self._fuzzy_index()
        counts = {t: self.db.execute(f"select count(*) from {t}").fetchone()[0] for t in S.TABLES if t != "meta"}
        meta = dict(meta)
        meta.update({"schema_version": str(S.SCHEMA_VERSION), "fuzzy_mode": "bigram" if self.bigram else "trigram",
                     "builder_version": BUILDER_VERSION, "app_version": APP_VERSION,
                     "sqlite_version": sqlite3.sqlite_version,
                     "licences": json.dumps(LICENCES, sort_keys=True),
                     "counts": json.dumps(counts, sort_keys=True),
                     "min_release_groups": str(self.min_rg),
                     "musicians": self.musicians,
                     "act_rule": ("every sampled artist" if self.sample else
                                  "performs at an MB event" + (f", or credited on >= {self.min_rg} release groups"
                                                               if self.min_rg else "")),
                     "geonames_credit": "Place data from GeoNames (geonames.org), CC BY 4.0."})
        if self.sample:
            meta["sample"] = str(self.sample)
        self.db.executemany("insert into meta values (?,?)", sorted(meta.items()))
        self.db.commit()
        self.log("analyze")
        self.db.execute("analyze")
        self.db.commit()
        self.db.close()
        return counts

    def _fuzzy_index(self, kinds=None):
        db = self.db
        src = {"place": "select id, replace(key,' ','') from place_name",
               "act": "select id, replace(key,' ','') from act_name",
               "event": "select id, replace(key,' ','') from event"}
        for kind, sql in src.items():
            if kinds is not None and kind not in kinds:
                continue
            if not self.bigram:
                db.execute(f"create virtual table fts_{kind} using fts5(k, content='', {S.FTS_OPTIONS})")
                db.execute(f"insert into fts_{kind}(rowid, k) {sql}")
                db.execute(f"create virtual table vocab_{kind} using fts5vocab(fts_{kind}, 'row')")
            else:
                db.execute(f"create table gram_{kind}(g text, rid integer)")
                rows = _Rows(db, f"insert into gram_{kind} values (?,?)", 50000)
                for rid, k in db.execute(sql).fetchall():
                    for g in S.bigrams(k):
                        rows.add((g, rid))
                rows.flush()
                db.execute(f"create index gram_{kind}_g on gram_{kind}(g)")
                db.execute(f"create table gram_df_{kind} as select g, count(*) n from gram_{kind} group by g")
                db.execute(f"create index gram_df_{kind}_g on gram_df_{kind}(g)")
            db.commit()


# ── build ────────────────────────────────────────────────────────────────────

def build_atlas(out, sources, *, min_release_groups=0, force_bigram=False, musicians="members", log=say):
    """Build `out` from `sources` and return {"counts", "seconds", "bytes", "path"}. Writes to a temp
    file beside `out` and renames it into place when complete, so a failed build never leaves a half file."""
    t0 = time.time()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".building")
    b = AtlasBuilder(tmp, sample=sources.sample, min_release_groups=min_release_groups,
                     force_bigram=force_bigram, musicians=musicians, log=log)
    log(f"SQLite {sqlite3.sqlite_version}: fuzzy matching by {'bigram table (no FTS5 trigram here)' if b.bigram else 'FTS5 trigram'}")
    b.add_areas(sources.mb("area"))
    b.add_places(sources.mb("place"))
    b.add_events(sources.mb("event"))
    b.finish_areas()
    if sources.has_release_groups and not sources.sample:
        b.count_release_groups(sources.mb("release-group"))
    b.add_artists(sources.mb("artist"))
    b.finish_artists()
    wd, wd_status, wd_reason = None, sources.wd_status, None
    try:
        wd = sources.wikidata()
    except Exception as e:                          # noqa: BLE001 -- a source failing must not end the build
        wd_status, wd_reason = "failed", f"{type(e).__name__}: {e}"
        log(f"WARNING: Wikidata could not be fetched ({wd_reason}). The build goes on with MusicBrainz and "
            f"GeoNames; add Wikidata afterwards with: python3 -m app.atlas.build --wikidata-only")
    if wd:
        b.add_wikidata(*wd)
        wd_status = sources.wd_status or "ok"
    elif wd_status is None:
        wd_status = "skipped"
    gn = sources.geonames()
    if gn:
        b.add_geonames(gn["cities"], gn["admin1"], gn.get("alt"))
    meta = {"build_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source_wikidata": wd_status}
    if wd_reason:
        meta["source_wikidata_reason"] = wd_reason
    if wd:
        meta["wikidata_name_floor"] = str(b.wd_floor)
    meta.update({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v)
                 for k, v in sources.describe().items()})
    counts = b.finish(meta)
    tmp.replace(out)
    secs = time.time() - t0
    return {"counts": counts, "seconds": secs, "bytes": out.stat().st_size, "path": str(out)}


# ── Wikidata only ────────────────────────────────────────────────────────────

def update_wikidata(out, fetched, *, status="ok", reason=None, info=None, log=say):
    """Replace the Wikidata-sourced rows of an existing Atlas with `fetched` (items, names), rebuild the
    place and event fuzzy indexes and update meta. Works on a copy and renames it into place, so
    a failure leaves the Atlas as it was. Returns the new row counts."""
    import shutil
    out = Path(out)
    if not out.exists():
        raise BuildError(f"no Atlas at {out}: run a build first")
    if out.with_name(out.name + ".building").exists():
        raise BuildError(f"{out.name}.building exists: a build is still writing. Run this after it finishes")
    t0 = time.time()
    tmp = out.with_name(out.name + ".updating")
    shutil.copyfile(out, tmp)
    try:
        b = AtlasBuilder.reopen(tmp, log=log)
        if int(b.meta.get("schema_version", "0")) != S.SCHEMA_VERSION:
            raise BuildError(f"{out.name} has schema {b.meta.get('schema_version')}, this builder writes {S.SCHEMA_VERSION}")
        b.drop_wikidata()
        b.add_wikidata(*fetched)
        for rows in (b.r_place, b.r_place_name, b.r_event):
            rows.flush()
        log(f"fuzzy index ({'bigram table' if b.bigram else 'FTS5 trigram'}) for places and events")
        b.reindex(("place", "event"))
        counts = {t: b.db.execute(f"select count(*) from {t}").fetchone()[0] for t in S.TABLES if t != "meta"}
        upd = {"counts": json.dumps(counts, sort_keys=True), "source_wikidata": status,
               "wikidata_name_floor": str(b.wd_floor),
               "wikidata_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if info:
            upd["wikidata"] = json.dumps(info, sort_keys=True)
        b.db.executemany("insert or replace into meta values (?,?)", sorted(upd.items()))
        if reason:
            b.db.execute("insert or replace into meta values ('source_wikidata_reason', ?)", (reason,))
        else:
            b.db.execute("delete from meta where key = 'source_wikidata_reason'")
        b.db.commit()
        b.db.execute("analyze")
        b.db.commit()
        b.db.close()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    return {"counts": counts, "seconds": time.time() - t0, "bytes": out.stat().st_size, "path": str(out)}


# ── sanity checks ────────────────────────────────────────────────────────────

def run_checks(path, printer=say, *, sample=False):
    """Row counts and the lookups the Atlas exists for. Returns the number of failures."""
    from .lookup import Atlas
    a = Atlas.open(path)
    fails = 0
    printer("sanity checks")
    for t, n in a.counts().items():
        printer(f"  {t:<14} {n:>12,}")
    printer(f"  fuzzy mode: {a.fuzzy_mode}; built {a.meta.get('build_date')}; sqlite {a.meta.get('sqlite_version')}")

    def check(label, ok, detail, essential=True):
        nonlocal fails
        word = "ok  " if ok else ("n/a " if (sample or not essential) else "FAIL")
        if not ok and not sample and essential:
            fails += 1
        printer(f"  [{word}] {label}: {detail}")

    c = a.artist("DiMeola")
    check('"DiMeola" -> Al Di Meola', any(x.name == "Al Di Meola" for x in c),
          ", ".join(f"{x.name} ({x.score:.2f} {x.how})" for x in c[:3]) or "no candidates")
    c = a.artist("Pat Matheny")
    check('"Pat Matheny" -> Pat Metheny', any(x.name.startswith("Pat Metheny") for x in c),
          ", ".join(f"{x.name} ({x.score:.2f} {x.how})" for x in c[:3]) or "no candidates")
    c = a.venue("Hammersmith Odeon")
    london = []
    for x in c:
        areas = [r[0] for r in a._q("select a.name from place_area pa join area a on a.id = pa.area_id "
                                    "where pa.place_id = ?", (x.id,))]
        london.append((x, any("london" in n.lower() for n in areas + [x.extra.get("city") or "", x.extra.get("region") or ""])))
    check('"Hammersmith Odeon" -> a place in London', any(ok for _, ok in london),
          ", ".join(f"{x.name} ({x.extra.get('city')}, {x.extra.get('country')}; matched {x.matched!r} "
                    f"{x.start_year}-{x.end_year})" for x, _ in london[:3]) or "no candidates")
    return fails


# ── command line ─────────────────────────────────────────────────────────────

def _wikidata_only(args):
    from . import wikidata as WD
    out = Path(args.out)
    if not out.exists():
        say(f"STOPPED: no Atlas at {out}. Build one first (python3 -m app.atlas.build --skip-wikidata)")
        return 1
    if args.sample:
        say("STOPPED: --sample would replace the real Wikidata rows with a sample; it is not for --wikidata-only")
        return 1
    if args.plan:
        say("Wikidata: " + (WD.cached_estimate(args.cache, args.api_delay)
                            or "ids first (about 20 SPARQL queries), then entity details 50 a call; "
                               "the exact estimate prints after pass 1"))
        return 0
    try:
        items, names, info = WD.fetch(args.cache, delay=args.delay, api_delay=args.api_delay,
                                      sample=args.sample, refresh=args.refresh_wikidata)
    except Exception as e:                          # noqa: BLE001
        say(f"STOPPED: Wikidata could not be fetched ({type(e).__name__}: {e}). The Atlas is unchanged. "
            f"What was fetched is cached; run the same command again to resume.")
        return 1
    status = "partial" if (info["failed"] or info["failed_batches"]) else "ok"
    try:
        res = update_wikidata(out, (items, names), status=status, info=info)
    except BuildError as e:
        say(f"STOPPED: {e}")
        return 1
    say(f"updated {res['path']}: {human(res['bytes'])} in {res['seconds'] / 60:.1f} min (source_wikidata={status})")
    fails = run_checks(res["path"], sample=bool(args.sample))
    say("done" if not fails else f"done, with {fails} failed sanity check(s)")
    return 2 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m app.atlas.build",
                                 description="Build the Trellis Atlas (assets/atlas/atlas.sqlite).")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output file (default assets/atlas/atlas.sqlite)")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="download cache (default ~/Workshop/dev/atlas-cache/)")
    ap.add_argument("--sample", type=int, metavar="N", help="small build: first N records of each source")
    ap.add_argument("--skip-wikidata", action="store_true")
    ap.add_argument("--no-release-groups", action="store_true", help="acts qualify on MB events alone")
    ap.add_argument("--min-release-groups", type=int, default=0, metavar="N",
                    help="release groups that qualify an act with no MB event (default 0: events only)")
    ap.add_argument("--musicians", choices=("members", "none"), default="none",
                    help="members: store each kept group's members; none (default): leave the musician tables empty")
    ap.add_argument("--offline", action="store_true",
                    help="use only the files already in --cache; make no request; Wikidata is skipped")
    ap.add_argument("--geonames", choices=("cities5000", "cities15000"), default="cities5000")
    ap.add_argument("--no-geonames-alt", action="store_true", help="skip alternateNamesV2 (190 MB)")
    ap.add_argument("--mb-release", help="a specific dump folder, e.g. 20261001-001001 (default: LATEST)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between Wikidata SPARQL requests")
    ap.add_argument("--api-delay", type=float, default=1.0, help="seconds between Wikidata API calls (never below 1)")
    ap.add_argument("--wikidata-only", action="store_true",
                    help="fetch Wikidata and put it into the existing --out Atlas, replacing only its Wikidata rows")
    ap.add_argument("--refresh-wikidata", action="store_true", help="ignore the Wikidata cache and fetch everything again")
    ap.add_argument("--force-bigram", action="store_true", help="build the bigram fallback instead of FTS5 trigram")
    ap.add_argument("--plan", action="store_true", help="print the download plan and stop")
    ap.add_argument("--check", metavar="PATH", help="run the sanity checks on a built file and stop")
    args = ap.parse_args(argv)

    if args.check:
        return 2 if run_checks(args.check) else 0
    say(f"Atlas builder {BUILDER_VERSION}; SQLite {sqlite3.sqlite_version}; Python {sys.version.split()[0]}")
    if not S.trigram_available():
        say("this SQLite has no FTS5 trigram (needs 3.34 or later): the bigram fallback will be built. "
            "Use the project's .venv Python for the shipped file.")
    if args.wikidata_only:
        return _wikidata_only(args)
    try:
        src = RemoteSources(args.cache, sample=args.sample, skip_wikidata=args.skip_wikidata,
                            release=args.mb_release, release_groups=not args.no_release_groups,
                            geonames_set=args.geonames, geonames_alt=not args.no_geonames_alt,
                            delay=args.delay, api_delay=args.api_delay, refresh_wikidata=args.refresh_wikidata,
                            offline=args.offline)
        if args.plan:
            src.plan()
            return 0
        if not args.sample:
            src.plan()
        res = build_atlas(args.out, src, min_release_groups=args.min_release_groups,
                          force_bigram=args.force_bigram, musicians=args.musicians)
    except BuildError as e:
        say(f"BUILD STOPPED: {e}")
        return 1
    if iter_tar_json.bad:
        say(f"{iter_tar_json.bad} dump lines were not valid JSON and were skipped")
    mins = res["seconds"] / 60
    say(f"built {res['path']}: {human(res['bytes'])} in {mins:.1f} min")
    try:
        con = sqlite3.connect(f"file:{res['path']}?mode=ro", uri=True)
        rows = con.execute("select name, sum(pgsize) from dbstat group by name order by 2 desc limit 8").fetchall()
        con.close()
        say("largest tables and indexes: " + ", ".join(f"{n} {human(b)}" for n, b in rows))
    except sqlite3.Error:
        pass
    fails = run_checks(res["path"], sample=bool(args.sample))
    say("done" if not fails else f"done, with {fails} failed sanity check(s): the file is written, do not ship it until they pass")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
