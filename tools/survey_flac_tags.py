#!/usr/bin/env python3
"""
Inventory the Vorbis comments actually present on disk across the library.

Read-only. Splits the library in two:

  TRELLIS-WRITTEN  folders carrying CONCERTDATE, i.e. ones our own tag export
                   has already clobbered. Their tags tell us nothing about the
                   wider community, so they are counted and then set aside.
  UNTOUCHED        everything else: whatever the taper, the seeder, or some
                   other tagger left behind. This is the interesting half.

Reports key frequency, multi-value usage and distinct example values, and
separates de facto standard keys from genuinely custom ones.

Run natively, inside .venv -- a Cowork session cannot open the NAS FLACs.

    source .venv/bin/activate
    python3 tools/survey_flac_tags.py
    python3 tools/survey_flac_tags.py --limit 200        # random subset
    python3 tools/survey_flac_tags.py --json /tmp/tagsurvey.json
"""
import argparse
import collections
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from config import LIBRARY_ROOT
except Exception:                                       # noqa: BLE001
    LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", "")

from mutagen.flac import FLAC                           # noqa: E402

SKIP_PREFIXES = (".", "_")          # .DS_Store, _originals/
MARKER = "concertdate"              # presence means Trellis has written here

# Named in the Vorbis comment spec, plus the de facto keys every tagger uses.
# Anything outside this set is reported separately as custom.
STANDARD = {
    "title", "version", "album", "tracknumber", "artist", "performer",
    "copyright", "license", "organization", "description", "genre", "date",
    "location", "contact", "isrc",
    "albumartist", "album artist", "composer", "comment", "discnumber",
    "disctotal", "tracktotal", "totaltracks", "totaldiscs", "encoder",
    "encoded-by", "encodedby", "encoder_options", "compatible_brands",
}


def find_folders(root):
    """Yield (folder, first_flac_filename) for each folder containing FLACs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(SKIP_PREFIXES)]
        flacs = sorted(f for f in filenames if f.lower().endswith(".flac"))
        if flacs:
            yield dirpath, flacs[0]


def read_tags(path):
    """Return {key_lower: [values]} for one FLAC, or None on read failure."""
    try:
        tags = FLAC(path).tags or {}
    except Exception:                                    # noqa: BLE001
        return None
    out = {}
    for key, value in tags:                              # mutagen yields pairs
        out.setdefault(key.lower(), []).append(value)
    return out


def report(title, folders, per_folder):
    """Print a frequency table with distinct example values."""
    n = len(folders)
    print(f"\n{'=' * 96}\n{title}  ({n} folders)\n{'=' * 96}")
    if not n:
        return

    present = collections.Counter()
    multi = collections.Counter()
    examples = collections.defaultdict(list)

    for f in folders:
        for k, values in per_folder[f].items():
            present[k] += 1
            if len(values) > 1:
                multi[k] += 1
            for v in values:
                v = str(v).strip()
                if v and v not in examples[k] and len(examples[k]) < 4:
                    examples[k].append(v)

    for label, keys in (("STANDARD KEYS", lambda k: k in STANDARD),
                        ("CUSTOM KEYS",   lambda k: k not in STANDARD)):
        rows = [(k, c) for k, c in present.most_common() if keys(k)]
        if not rows:
            continue
        print(f"\n-- {label} " + "-" * (92 - len(label)))
        for k, c in rows:
            flag = f"  multi x{multi[k]}" if multi[k] else ""
            print(f"\n  {k}   {c} folders ({100.0 * c / n:.1f}%){flag}")
            for v in examples[k]:
                print(f"      | {v[:110]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=LIBRARY_ROOT)
    ap.add_argument("--limit", type=int, help="sample this many folders at random")
    ap.add_argument("--json", help="write the full per-folder result here")
    args = ap.parse_args()

    if not args.root or not os.path.isdir(args.root):
        sys.exit("Library root not found: %r" % (args.root,))

    candidates = list(find_folders(args.root))
    total = len(candidates)
    if args.limit and args.limit < total:
        random.seed(0)                                   # reproducible sample
        candidates = random.sample(candidates, args.limit)

    per_folder = {}
    errors = []
    for folder, name in candidates:
        tags = read_tags(os.path.join(folder, name))
        if tags is None:
            errors.append(folder)
            continue
        per_folder[os.path.relpath(folder, args.root)] = tags

    keys = list(per_folder)
    written = [k for k in keys if MARKER in per_folder[k]]
    untouched = [k for k in keys if MARKER not in per_folder[k]]
    bare = [k for k in untouched if not per_folder[k]]

    print("root       : %s" % args.root)
    print("folders    : %d read, %d found, %d read errors" % (len(keys), total, len(errors)))
    print("trellis    : %d already carry %s (set aside)" % (len(written), MARKER.upper()))
    print("untouched  : %d, of which %d carry no tags at all" % (len(untouched), len(bare)))

    report("UNTOUCHED BY TRELLIS", [k for k in untouched if per_folder[k]], per_folder)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"root": args.root, "written": written,
                       "untouched": untouched, "folders": per_folder}, fh, indent=1)
        print("\nwrote %s" % args.json)


if __name__ == "__main__":
    main()
