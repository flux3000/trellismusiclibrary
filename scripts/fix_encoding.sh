#!/usr/bin/env bash
#
# fix_encoding.sh — repair taper info files that got mangled by wget.
#
# A browser's "Save Page As" auto-detects the source encoding and transcodes
# to UTF-8 on save. wget doesn't — it writes exactly the bytes the server
# sent, encoding untouched. Old taper .txt/.nfo files are very often typed
# in Windows Notepad and saved as Windows-1252 (curly quotes, apostrophes,
# em dashes all live above the ASCII range there), so anything that reads
# them assuming UTF-8 turns those characters into gobbledygook.
#
# Usage:
#   ./fix_encoding.sh [directory]      # defaults to the current directory
#
# Recurses through the directory for .txt and .nfo files. A file that's
# already valid UTF-8 is left completely alone and not even mentioned in the
# output — this only touches files that actually need it. Anything it does
# convert gets its original saved alongside it as <file>.orig first, so a
# bad guess never actually loses data.

set -euo pipefail

ROOT="${1:-.}"

if ! command -v iconv >/dev/null 2>&1; then
  echo "iconv not found -- this ships with macOS and Linux by default, check your PATH." >&2
  exit 1
fi

converted=0
already_ok=0
failed=0

while IFS= read -r -d '' f; do
  # Already valid UTF-8 (including plain ASCII, which is a UTF-8 subset) --
  # leave it untouched. This check is what keeps a correct file from ever
  # being "corrected" into mush.
  if iconv -f UTF-8 -t UTF-8 "$f" >/dev/null 2>&1; then
    already_ok=$((already_ok + 1))
    continue
  fi

  tmp="$(mktemp)"
  if iconv -f WINDOWS-1252 -t UTF-8 "$f" > "$tmp" 2>/dev/null \
     || iconv -f ISO-8859-1 -t UTF-8 "$f" > "$tmp" 2>/dev/null; then
    cp "$f" "$f.orig"
    mv "$tmp" "$f"
    converted=$((converted + 1))
    echo "fixed: $f"
  else
    rm -f "$tmp"
    failed=$((failed + 1))
    echo "could not fix: $f (encoding not recognized -- left as-is)" >&2
  fi
done < <(find "$ROOT" -type f \( -iname '*.txt' -o -iname '*.nfo' \) -print0)

echo
echo "done -- $converted fixed, $already_ok already fine, $failed left alone"
