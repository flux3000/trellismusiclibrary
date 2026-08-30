#!/usr/bin/env python3
"""
trellis_uninstall.py — find and remove every trace of Trellis Music Library
from a test machine (app, repo+venv, database, library folders, logs).

Stdlib only, so it runs with whatever python3 ships on the Mac. Keychain
entries are reported but NOT auto-deleted — fuzzy-matching Keychain items
by name is a good way to nuke something unrelated, so that step stays manual.

SAFETY: the Library/Download/Backlog/Workshop tree the first-run wizard
creates is NOT deleted by default, even with --execute. An empty scaffold
folder (nothing ever landed in it) is safe and gets removed like everything
else. A folder that actually contains files — audio from testing, most
likely — is reported but held back. Add --include-library-content to also
delete that. There is no way to recover a deleted FLAC; the safe path costs
you one extra flag, not one lost recording.

Usage:
    python3 trellis_uninstall.py                          # dry run — report only
    python3 trellis_uninstall.py --execute                 # delete app/repo/db/logs; KEEPS any library content
    python3 trellis_uninstall.py --execute --yes            # same, no per-group confirmation
    python3 trellis_uninstall.py --execute --include-library-content   # also delete library folders that contain files
"""

import argparse
import shutil
import subprocess
from collections import Counter
from pathlib import Path

HOME = Path.home()

# Every name Trellis has gone by, oldest to newest — the rename left some
# paths on the old name. Check all three, not just the current one.
NAMES = ["Flux Audio", "Trellis", "Trellis Music Library"]


def find_running_processes():
    """pgrep, not ps|grep — doesn't match its own command line."""
    try:
        out = subprocess.run(
            ["pgrep", "-fli", "trellis"], capture_output=True, text=True
        ).stdout.strip()
        return [line for line in out.splitlines() if line]
    except FileNotFoundError:
        return ["(pgrep not found — check manually with Activity Monitor)"]


def mdfind(query, onlyin=None):
    """Spotlight search — fast, and covers the whole disk without a manual walk."""
    cmd = ["mdfind"]
    if onlyin:
        cmd += ["-onlyin", str(onlyin)]
    cmd += ["-name", query]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        return [Path(p) for p in out.splitlines() if p]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def du(path):
    """Cheap human-readable size, best-effort."""
    try:
        n = path.stat().st_size if path.is_file() else sum(
            f.stat().st_size for f in path.rglob("*") if f.is_file()
        )
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"
    except OSError:
        return "size unknown"


def classify_library_tree(paths):
    """
    Split library-tree candidates into 'empty' (safe — pure wizard scaffold,
    trivially recreated) and 'protected' (contains real files — likely audio
    from testing, held back from deletion unless explicitly included).
    Returns (empty_paths, protected_info) where protected_info is
    [(path, file_count, total_bytes, {ext: count})].
    """
    empty, protected = [], []
    for p in paths:
        files = [f for f in p.rglob("*") if f.is_file()]
        if not files:
            empty.append(p)
        else:
            exts = Counter(f.suffix.lower() or "(no ext)" for f in files)
            total = sum(f.stat().st_size for f in files)
            protected.append((p, len(files), total, exts))
    return empty, protected


def collect_targets():
    """Returns {group_label: [Path, ...]} plus a separate protected-library list."""
    targets = {}
    seen = set()

    def add(label, paths):
        fresh = [p for p in paths if p.exists() and p not in seen]
        seen.update(fresh)
        if fresh:
            targets.setdefault(label, []).extend(fresh)

    # 1. The packaged .app, wherever it landed (Applications, Desktop, Downloads, ...)
    add("Packaged app (.app bundle)", mdfind("Trellis Music Library.app"))

    # 2. Source checkout — look for a repo dir literally named trellis/fluxaudio
    #    under common dev locations rather than crawling the whole home dir.
    dev_roots = [HOME / "Workshop" / "dev", HOME / "dev", HOME / "Projects", HOME]
    repo_hits = []
    for root in dev_roots:
        if not root.exists():
            continue
        for name in ("trellis", "fluxaudio"):
            p = root / name
            if p.exists() and (p / "run.py").exists():
                repo_hits.append(p)
    add("Source repo (includes its .venv)", repo_hits)

    # 3. App data — the database, avatars, transcode cache, root marker.
    #    Check every historical folder name under Application Support.
    add(
        "App data (database + cache, in Application Support)",
        [HOME / "Library" / "Application Support" / n for n in NAMES],
    )

    # 4. The Library/Download/Backlog/Workshop tree the first-run wizard
    #    creates wherever the user pointed it. Handled separately below —
    #    NOT folded into `targets` — because it needs the empty/protected split.
    lib_tree_candidates = []
    for parent in (HOME, HOME / "Desktop", HOME / "Documents"):
        for n in NAMES:
            p = parent / n
            if p.exists() and "Application Support" not in str(p) and p not in seen:
                lib_tree_candidates.append(p)
    seen.update(lib_tree_candidates)
    empty_lib, protected_lib = classify_library_tree(lib_tree_candidates)
    if empty_lib:
        targets["Library folder tree — empty scaffold (safe)"] = empty_lib

    # 5. Logs
    add("Logs", [HOME / "Library" / "Logs" / "FluxAudio"])

    # 6. Mount LaunchAgent — only relevant if this box was ever set up as the
    #    NAS-mounting host, not a plain streaming peer, but check anyway.
    add(
        "Mount LaunchAgent (unlikely on a peer-only box)",
        [HOME / "Library" / "LaunchAgents" / "com.fluxaudio.mountlibrary.plist"],
    )

    return targets, protected_lib


def report(targets, protected_lib, procs):
    print("=" * 70)
    print("TRELLIS UNINSTALL — scan results")
    print("=" * 70)

    if procs:
        print("\n⚠ Running process(es) matching 'trellis' — stop these first:")
        for line in procs:
            print(f"   {line}")
    else:
        print("\n✓ No running Trellis processes found.")

    if not targets and not protected_lib:
        print("\n✓ Nothing else found on disk. You're already clean.")
        return

    for label, paths in targets.items():
        print(f"\n{label}:")
        for p in paths:
            print(f"   {p}  ({du(p)})")

    if protected_lib:
        print(
            "\n🔒 Library folder(s) with actual files in them — KEPT by default:"
        )
        for p, count, total, exts in protected_lib:
            ext_str = ", ".join(f"{n}x{e}" for e, n in exts.most_common())
            print(f"   {p}")
            print(f"      {count} files, {du_bytes(total)} — {ext_str}")
        print(
            "   These are not touched even with --execute. Pass\n"
            "   --include-library-content as well if you really want them gone —\n"
            "   there's no undo on a deleted FLAC."
        )

    print(
        "\n⚠ Not covered by this script — do these by hand:\n"
        "   Keychain Access.app → search 'remote_token' and 'trellis' → delete matches.\n"
        "   (peer nodes store a remote_token:<node_id> entry when they join a library;\n"
        "   a BYOK AI key, if one was ever entered in Settings, lives there too.)"
    )


def du_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def execute(targets, protected_lib, include_library_content, assume_yes):
    for label, paths in targets.items():
        if not assume_yes:
            ans = input(f"\nDelete all under '{label}'? [y/N] ").strip().lower()
            if ans != "y":
                print("   skipped.")
                continue
        for p in paths:
            _rm(p)

    if not protected_lib:
        return

    if not include_library_content:
        print(
            f"\n🔒 Kept {len(protected_lib)} library folder(s) with real files — "
            "re-run with --include-library-content to also delete those."
        )
        return

    print(
        "\n⚠ --include-library-content was passed — this deletes real files, "
        "audio included."
    )
    for p, count, total, exts in protected_lib:
        if not assume_yes:
            ans = input(
                f"   Really delete {p} ({count} files, {du_bytes(total)})? "
                "Type the word 'delete' to confirm: "
            ).strip()
            if ans != "delete":
                print("   skipped.")
                continue
        _rm(p)


def _rm(p):
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        print(f"   ✓ removed {p}")
    except OSError as e:
        print(f"   ✗ failed on {p}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually delete (default is dry-run/report only)")
    ap.add_argument("--yes", action="store_true", help="skip the per-group confirmation prompt (library content still needs its own 'delete' typed confirmation unless combined with --include-library-content and --yes)")
    ap.add_argument(
        "--include-library-content",
        action="store_true",
        help="ALSO delete library folders that contain real files (likely audio) — off by default, no undo",
    )
    args = ap.parse_args()

    procs = find_running_processes()
    targets, protected_lib = collect_targets()
    report(targets, protected_lib, procs)

    if not args.execute:
        print("\n(dry run — nothing was deleted. Re-run with --execute to remove the above.)")
        return

    if procs:
        print("\n✗ Refusing to delete while a Trellis process is running — stop it first (see above).")
        return

    execute(targets, protected_lib, args.include_library_content, args.yes)
    print("\nDone. Re-run without --execute to confirm the scan comes back empty (or shows only kept library content).")


if __name__ == "__main__":
    main()
