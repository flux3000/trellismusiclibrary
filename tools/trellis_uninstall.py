#!/usr/bin/env python3
"""
trellis_uninstall.py — find and remove every trace of Trellis Music Library
from a test machine (app, repo+venv, database, library folders, logs).

Stdlib only, so it runs with whatever python3 ships on the Mac. Keychain
entries are reported but NOT auto-deleted — fuzzy-matching Keychain items
by name is a good way to nuke something unrelated, so that step stays manual.

Usage:
    python3 trellis_uninstall.py              # dry run — just reports what it found
    python3 trellis_uninstall.py --execute    # actually deletes (asks to confirm each group)
    python3 trellis_uninstall.py --execute --yes   # no per-group confirmation (still prints what it did)
"""

import argparse
import shutil
import subprocess
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


def collect_targets():
    """Returns {group_label: [Path, ...]} of everything found. Dedup'd."""
    targets = {}
    seen = set()

    def add(label, paths):
        fresh = [p for p in paths if p.exists() and p not in seen]
        for p in fresh:
            seen.add(p)
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
    app_support = [HOME / "Library" / "Application Support" / n for n in NAMES]
    add("App data (database + cache, in Application Support)", app_support)

    # 4. The Library/Download/Backlog/Workshop tree the first-run wizard
    #    creates wherever the user pointed it — search common top-level spots.
    lib_tree_hits = []
    for parent in (HOME, HOME / "Desktop", HOME / "Documents"):
        for n in NAMES:
            p = parent / n
            # Skip anything already caught as Application Support above.
            if p.exists() and "Application Support" not in str(p) and p not in seen:
                lib_tree_hits.append(p)
    add("Library folder tree (Library/Download/Backlog/Workshop)", lib_tree_hits)

    # 5. Logs
    add("Logs", [HOME / "Library" / "Logs" / "FluxAudio"])

    # 6. Mount LaunchAgent — only relevant if this box was ever set up as the
    #    NAS-mounting host, not a plain streaming peer, but check anyway.
    add(
        "Mount LaunchAgent (unlikely on a peer-only box)",
        [HOME / "Library" / "LaunchAgents" / "com.fluxaudio.mountlibrary.plist"],
    )

    return targets


def report(targets, procs):
    print("=" * 70)
    print("TRELLIS UNINSTALL — scan results")
    print("=" * 70)

    if procs:
        print("\n⚠ Running process(es) matching 'trellis' — stop these first:")
        for line in procs:
            print(f"   {line}")
    else:
        print("\n✓ No running Trellis processes found.")

    if not targets:
        print("\n✓ Nothing else found on disk. You're already clean.")
        return

    for label, paths in targets.items():
        print(f"\n{label}:")
        for p in paths:
            size = du(p)
            print(f"   {p}  ({size})")

    print(
        "\n⚠ Not covered by this script — do these by hand:\n"
        "   Keychain Access.app → search 'remote_token' and 'trellis' → delete matches.\n"
        "   (peer nodes store a remote_token:<node_id> entry when they join a library;\n"
        "   a BYOK AI key, if one was ever entered in Settings, lives there too.)"
    )


def du(path):
    """Cheap human-readable size, best-effort."""
    try:
        if path.is_file():
            n = path.stat().st_size
        else:
            n = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"
    except OSError:
        return "size unknown"


def execute(targets, assume_yes):
    for label, paths in targets.items():
        if not assume_yes:
            ans = input(f"\nDelete all under '{label}'? [y/N] ").strip().lower()
            if ans != "y":
                print("   skipped.")
                continue
        for p in paths:
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                print(f"   ✓ removed {p}")
            except OSError as e:
                print(f"   ✗ failed on {p}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="actually delete (default is dry-run/report only)")
    ap.add_argument("--yes", action="store_true", help="skip the per-group confirmation prompt")
    args = ap.parse_args()

    procs = find_running_processes()
    targets = collect_targets()
    report(targets, procs)

    if not args.execute:
        print("\n(dry run — nothing was deleted. Re-run with --execute to remove the above.)")
        return

    if procs:
        print("\n✗ Refusing to delete while a Trellis process is running — stop it first (see above).")
        return

    execute(targets, args.yes)
    print("\nDone. Re-run without --execute to confirm the scan comes back empty.")


if __name__ == "__main__":
    main()
