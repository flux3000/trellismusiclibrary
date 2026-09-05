#!/usr/bin/env python3
"""
tools/parse_debug.py — Info file parser debug/workshop tool.

Runs parse_info_file() against one or more text files (or a folder) and
prints a formatted report. No DB writes, no side effects.

Usage:
    python3 tools/parse_debug.py path/to/file.txt
    python3 tools/parse_debug.py path/to/folder/
    python3 tools/parse_debug.py ~/Workshop/_audio_library/_text/ --db
    python3 tools/parse_debug.py *.txt --tracks

Options:
    --db        Load known artist/venue names from the Flux Audio SQLite DB
                for fuzzy match scoring. DB path read from .env or default.
    --tracks    Print the full track list (default: show count only).
    --no-color  Disable ANSI color output.
"""

import sys
import os
import argparse
from pathlib import Path

# ── Allow running from any directory ──────────────────────────────────────────
REPO_ROOT  = Path(__file__).resolve().parent.parent
UTILS_PATH = REPO_ROOT / "app" / "utils"
sys.path.insert(0, str(UTILS_PATH))

# Import the module directly to avoid triggering Flask app init
import importlib.util as _ilu
_spec   = _ilu.spec_from_file_location("ingest", UTILS_PATH / "ingest.py")
_ingest = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ingest)
parse_info_file = _ingest.parse_info_file

# ── Optional color support ─────────────────────────────────────────────────────
USE_COLOR = sys.stdout.isatty()

def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def green(t):  return _c(t, "32")
def yellow(t): return _c(t, "33")
def cyan(t):   return _c(t, "36")
def bold(t):   return _c(t, "1")
def dim(t):    return _c(t, "2")
def red(t):    return _c(t, "31")


def load_known_names_from_db():
    """Pull artist and venue names from the Flux Audio SQLite DB."""
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    db_path = os.environ.get("DATABASE_URL", str(REPO_ROOT / "db" / "trellis.db"))
    db_path = db_path.replace("sqlite:///", "")

    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        # Try both old schema (performer) and new schema (artist) gracefully
        artists = []
        for table in ("performer", "artist"):
            try:
                cur.execute(f"SELECT name FROM {table}")
                artists = [r[0] for r in cur.fetchall()]
                break
            except sqlite3.OperationalError:
                continue

        venues = []
        try:
            cur.execute("SELECT name FROM venue")
            venues = [r[0] for r in cur.fetchall()]
        except sqlite3.OperationalError:
            pass

        conn.close()
        return artists, venues
    except Exception as e:
        print(yellow(f"  [warn] Could not load DB ({e}) — running without known names"))
        return [], []


def fmt_field(label, value, match=None):
    """Format a single field row."""
    label_str = f"  {bold(label + ':'):<22}"
    if value is None:
        return f"{label_str}{dim('—')}"
    val_str = cyan(value)
    if match and match.lower() != value.lower():
        val_str += f"  {dim('→ fuzzy match:')} {green(match)}"
    elif match:
        val_str += f"  {dim('(exact match)')}"
    return f"{label_str}{val_str}"


def report_file(file_path, known_artists, known_venues, show_tracks):
    """Parse one file and print its report."""
    path = Path(file_path)
    print(bold(f"\n{'─' * 60}"))
    print(bold(f"  {path.name}"))
    print(bold(f"{'─' * 60}"))

    result = parse_info_file(str(path), known_artists=known_artists, known_venues=known_venues)

    if not result.get("raw_content"):
        print(red("  [error] Could not read file"))
        return

    print(fmt_field("Artist",  result["artist"],  result.get("artist_match")))
    print(fmt_field("Venue",   result["venue"],   result.get("venue_match")))

    # Date
    if result["year"]:
        parts = [str(result["year"])]
        if result["month"]: parts.append(f"{result['month']:02d}")
        if result["day"]:   parts.append(f"{result['day']:02d}")
        date_str = "-".join(parts)
    else:
        date_str = None
    print(fmt_field("Date", date_str))

    # Location
    loc_parts = [p for p in [result["city"], result["state"], result["country"]] if p]
    print(fmt_field("City",    result["city"]))
    print(fmt_field("State",   result["state"]))
    print(fmt_field("Country", result["country"]))

    print(fmt_field("Source",  result["source"]))

    # Tracks
    n_tracks = len(result["tracks"])
    track_label = f"{n_tracks} track{'s' if n_tracks != 1 else ''}"
    print(fmt_field("Tracks", track_label))

    if show_tracks and result["tracks"]:
        for t in result["tracks"]:
            print(f"    {dim(str(t['number']).rjust(3) + '.')}  {t['title']}")


def main():
    parser = argparse.ArgumentParser(
        description="Debug the Flux Audio info-file parser against text files."
    )
    parser.add_argument("paths", nargs="+", help="File(s) or folder(s) to parse")
    parser.add_argument("--db",       action="store_true", help="Load known artists/venues from DB")
    parser.add_argument("--tracks",   action="store_true", help="Print full track list")
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    args = parser.parse_args()

    global USE_COLOR
    if args.no_color:
        USE_COLOR = False

    # Collect all .txt files from the given paths
    txt_files = []
    for p in args.paths:
        path = Path(p).expanduser()
        if path.is_dir():
            txt_files.extend(sorted(path.glob("*.txt")))
        elif path.is_file() and path.suffix.lower() == ".txt":
            txt_files.append(path)
        else:
            print(yellow(f"[skip] {p} — not a .txt file or directory"))

    if not txt_files:
        print(red("No .txt files found."))
        sys.exit(1)

    # Optionally load known names from DB
    known_artists, known_venues = [], []
    if args.db:
        print(dim("Loading known artist/venue names from DB..."))
        known_artists, known_venues = load_known_names_from_db()
        print(dim(f"  {len(known_artists)} artists, {len(known_venues)} venues loaded\n"))

    for f in txt_files:
        report_file(f, known_artists, known_venues, args.tracks)

    print(f"\n{dim(f'Parsed {len(txt_files)} file(s).')}\n")


if __name__ == "__main__":
    main()
