#!/usr/bin/env python3
"""
tools/find_signables.py — enumerate everything inside a .app that needs its own
signature, in the order codesign has to be handed it.

    python3 tools/find_signables.py <path/to/App.app> binaries
    python3 tools/find_signables.py <path/to/App.app> frameworks

Both modes print NUL-separated paths, deepest path first, for a bash caller to
read with `while IFS= read -r -d ''`.

── Why this is Python and not a find | sort pipeline ────────────────────────

Three reasons, all of which cost a build:

1.  Depth-ordered NUL-separated sorting needs `sort -z`, which is a GNU
    extension. BSD sort's support for it varies by macOS release, and when it
    is missing the pipeline does not error — it produces nothing, which reads
    exactly like "this bundle contains no libraries."

2.  Testing for Mach-O by forking `file` once per candidate is ~700 processes
    and depends on the wording of its output. Reading four bytes and comparing
    them to the documented magic numbers is faster, and cannot be broken by a
    change to a message string.

3.  This file can be run and checked on any machine, against any bundle,
    without a certificate. The signing script cannot.

── The two modes, and why they are separate ─────────────────────────────────

FRAMEWORKS are signed as BUNDLES, never as the loose Mach-O inside them. A
framework carries its own _CodeSignature/CodeResources describing its
contents; signing the binary inside it directly leaves that manifest
describing something that no longer exists, and `codesign --verify --deep`
rejects the enclosing app. For a VERSIONED framework the unit to sign is the
version directory (Versions/3.13), not the .framework root.

BINARIES is therefore everything else — and it deliberately EXCLUDES anything
living under a .framework, because mode one already covers it.

Deepest first, in both modes, because a bundle's signature seals its contents:
whatever is inside has to be signed before the thing that contains it.
"""

import os
import sys
from pathlib import Path

# Mach-O and universal-binary magic numbers, as the first four bytes on disk,
# both byte orders. Source: <mach-o/loader.h> and <mach-o/fat.h>.
MACHO_MAGIC = {
    b"\xfe\xed\xfa\xce",  # MH_MAGIC       32-bit
    b"\xce\xfa\xed\xfe",  # MH_CIGAM       32-bit, swapped
    b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64
    b"\xcf\xfa\xed\xfe",  # MH_CIGAM_64
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC      universal
    b"\xbe\xba\xfe\xca",  # FAT_CIGAM
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
    b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64
}


def is_macho(path: Path) -> bool:
    """True if the file begins with a Mach-O or universal-binary magic number."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) in MACHO_MAGIC
    except OSError:
        return False


def looks_like_code(path: Path) -> bool:
    """Cheap pre-filter, so we do not open every text file in the bundle.

    The owner-execute bit OR a library extension. Name alone is not enough —
    PyInstaller ships extensionless helper binaries — and the execute bit alone
    is not enough either, because some .so files arrive without it.
    """
    if path.suffix in (".so", ".dylib"):
        return True
    try:
        return bool(path.stat().st_mode & 0o100)
    except OSError:
        return False


def deepest_first(paths):
    """Sort by path depth descending, then by name, so runs are reproducible."""
    return sorted(paths, key=lambda p: (-len(p.parts), str(p)))


def find_frameworks(contents: Path):
    """Framework bundles, as the directories codesign should actually be given.

    os.walk does not follow symlinks by default, which is what we want:
    PyInstaller mirrors Contents/Frameworks into Contents/Resources as symlinks,
    and signing the same framework twice through two names is at best wasted
    time and at worst a conflict.
    """
    out = []
    for root, dirs, _files in os.walk(contents, followlinks=False):
        for d in list(dirs):
            if not d.endswith(".framework"):
                continue
            fw = Path(root) / d
            if fw.is_symlink():
                continue
            versions = fw / "Versions"
            if versions.is_dir():
                # Concrete version directories only. "Current" is a symlink to
                # one of them; following it signs the same code twice.
                for v in sorted(versions.iterdir()):
                    if v.is_dir() and not v.is_symlink():
                        out.append(v)
            else:
                out.append(fw)
            # Do not descend into a framework: its contents are its own problem,
            # sealed by the signature we are about to put on it.
            dirs.remove(d)
    return deepest_first(out)


def find_binaries(contents: Path, exclude: Path):
    """Loose Mach-O files, minus the main executable, minus framework contents."""
    out = []
    for root, dirs, files in os.walk(contents, followlinks=False):
        # Same pruning as above, for the same reason.
        dirs[:] = [d for d in dirs if not d.endswith(".framework")]
        for name in files:
            p = Path(root) / name
            if p.is_symlink():
                continue
            if exclude is not None and p == exclude:
                continue
            if looks_like_code(p) and is_macho(p):
                out.append(p)
    return deepest_first(out)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[2] not in ("binaries", "frameworks"):
        print(f"usage: {sys.argv[0]} <App.app> binaries|frameworks", file=sys.stderr)
        return 2

    app = Path(sys.argv[1])
    contents = app / "Contents"
    if not contents.is_dir():
        print(f"{app} has no Contents/ — that is not an app bundle.", file=sys.stderr)
        return 1

    if sys.argv[2] == "frameworks":
        paths = find_frameworks(contents)
    else:
        # The main executable is signed separately, WITH entitlements, so it
        # must not appear in the nested list.
        exe_dir = contents / "MacOS"
        exes = [p for p in exe_dir.iterdir() if p.is_file()] if exe_dir.is_dir() else []
        main_exe = exes[0] if len(exes) == 1 else None
        paths = find_binaries(contents, main_exe)

    sys.stdout.write("".join(f"{p}\0" for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
