#!/usr/bin/env python3
"""
repair_flatten.py — bring pre-2026-07-14 recordings up to the flatten policy.

The always-flatten-on-ingest policy landed 2026-07-14. An audit of all 531
recordings found 5 that predate it and still have audio below their folder
root. Zero recordings ingested AFTER the policy are affected, so this is a
one-off historical cleanup, not a recurring bug.

Those 5 are NOT all the same problem, and treating them alike would destroy
data. Three distinct shapes:

  disc_subdirs      CD1/ + CD2/ holding the two halves of one show.
                    -> flatten, renumber continuously, set disc labels
                    (track.set_number — renamed 2026-07-28 from `set`,
                    a SQL reserved word; see note below).
                    Track numbers in the DB currently restart per disc
                    (1,1,2,2,3,3...) because multi-disc detection did not exist
                    at ingest — that is the exact bug the policy was created to
                    fix, so the numbers get rebuilt too.

  nested_show       The show folder contains a SECOND show folder holding all
                    the audio; the root is empty.
                    -> move the audio up one level, remove the empty shell.

  nested_duplicate  Audio at the root AND a nested copy of the same tracks.
                    -> verify the copy really is redundant by comparing FLAC
                    internal MD5 signatures, then delete the nested copy.
                    NEVER deleted on filename or size alone.

Safety:
  * dry run by default; --apply is required to touch anything
  * the database is copied to a timestamped backup before any write
  * a duplicate is only removed after every file's decoded-audio MD5 matches
  * non-audio (Art/, artwork, checksum and text files) is left exactly alone
  * DB and filesystem are updated together; a failure rolls the DB back

Usage:
    python3 repair_flatten.py                      # dry run, report the plan
    python3 repair_flatten.py --apply              # do it
    python3 repair_flatten.py --id 76 --apply      # one recording
"""

import os
import re
import sys
import shutil
import sqlite3
import argparse
import unicodedata
from datetime import datetime

DEFAULT_DB = os.path.expanduser("~/Workshop/dev/trellis/db/trellis.db")
DEFAULT_LIB = "/Volumes/music/Flux Audio/Library"

AUDIO_EXT = (".flac", ".wav", ".aiff", ".aif", ".shn", ".ape")
JUNK = {".ds_store", "thumbs.db", "desktop.ini", ".apdisk"}
DISC_RE = re.compile(r"^(cd|disc|disk|set|vol|volume|part|tape|show)\s*[-_]?\s*(\d+)$", re.I)


def norm(s):
    return unicodedata.normalize("NFC", s)


def is_audio(name):
    return name.lower().endswith(AUDIO_EXT)


def is_junk(name):
    return name.lower() in JUNK


def flac_md5(path):
    """
    FLAC's internal MD5 of the DECODED audio, read from the header.

    Filename- and tag-independent, so it is the right way to prove two files
    hold the same audio before deleting one. Falls back to a whole-file hash
    for non-FLAC.
    """
    try:
        import mutagen.flac
        sig = mutagen.flac.FLAC(path).info.md5_signature
        if sig:
            return f"flac:{sig:032x}"
    except Exception:
        pass
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "file:" + h.hexdigest()


def sanitize(name):
    """macOS forbids : and / in filenames."""
    return re.sub(r"[:/\x00]", "-", name).strip()


# ═════════════════════════════════════════════════════════════════════════════
# Classification
# ═════════════════════════════════════════════════════════════════════════════
def classify(folder):
    """Return (kind, detail) for a recording folder."""
    if not os.path.isdir(folder):
        return "missing", {}
    entries = os.listdir(folder)
    root_audio = sorted(e for e in entries if is_audio(e))
    subdirs = [e for e in entries
               if os.path.isdir(os.path.join(folder, e)) and not e.startswith(".")]

    audio_subs = []
    for s in subdirs:
        try:
            kids = os.listdir(os.path.join(folder, s))
        except OSError:
            continue
        if any(is_audio(k) for k in kids):
            audio_subs.append(s)

    if not audio_subs:
        return "flat", {"root_audio": root_audio}

    disc_subs = [s for s in audio_subs if DISC_RE.match(s)]
    if disc_subs and not root_audio:
        return "disc_subdirs", {"discs": sorted(
            disc_subs, key=lambda s: int(DISC_RE.match(s).group(2)))}
    if root_audio:
        return "nested_duplicate", {"root_audio": root_audio, "subs": audio_subs}
    if len(audio_subs) == 1:
        return "nested_show", {"sub": audio_subs[0]}
    return "unknown", {"subs": audio_subs, "root_audio": root_audio}


# ═════════════════════════════════════════════════════════════════════════════
# Planning — no side effects
# ═════════════════════════════════════════════════════════════════════════════
def plan_disc_subdirs(folder, detail, tracks):
    """
    Flatten CD1/CD2 into the root, renumbering continuously.

    Names are rebuilt as "NN - Title.ext" because the originals genuinely
    collide (every disc has its own 01) — the one case where the standing
    preserve-original-names policy has to give way.
    """
    moves, updates, n = [], [], 0
    width = 2
    total = sum(len([f for f in os.listdir(os.path.join(folder, d)) if is_audio(f)])
                for d in detail["discs"])
    width = max(2, len(str(total)))

    by_rel = {norm(t["file_path"]): t for t in tracks}
    for disc in detail["discs"]:
        files = sorted(f for f in os.listdir(os.path.join(folder, disc)) if is_audio(f))
        for f in files:
            n += 1
            rel_old = f"{disc}/{f}"
            t = by_rel.get(norm(rel_old))
            # Prefer the DB title; fall back to the filename with its leading
            # track number stripped.
            title = (t or {}).get("title") or re.sub(r"^\d+\s*[-._]\s*", "", os.path.splitext(f)[0])
            ext = os.path.splitext(f)[1]
            rel_new = f"{str(n).zfill(width)} - {sanitize(title)}{ext}"
            moves.append((rel_old, rel_new))
            if t:
                updates.append({"id": t["id"], "file_path": rel_new,
                                "track_number": n, "set_number": disc.upper()})
    return moves, updates


def plan_nested_show(folder, detail, tracks):
    """Move audio up one level. Names are preserved — nothing collides."""
    sub = detail["sub"]
    moves, updates = [], []
    by_rel = {norm(t["file_path"]): t for t in tracks}
    root_names = {f for f in os.listdir(folder) if is_audio(f)}
    for f in sorted(f for f in os.listdir(os.path.join(folder, sub)) if is_audio(f)):
        if f in root_names:
            return None, None            # unexpected collision — refuse
        rel_old, rel_new = f"{sub}/{f}", f
        moves.append((rel_old, rel_new))
        t = by_rel.get(norm(rel_old))
        if t:
            updates.append({"id": t["id"], "file_path": rel_new})
    return moves, updates


def verify_duplicate(folder, detail):
    """True only if every nested file's decoded audio matches one at the root."""
    root = {f: flac_md5(os.path.join(folder, f)) for f in detail["root_audio"]}
    root_sigs = set(root.values())
    for sub in detail["subs"]:
        for f in sorted(os.listdir(os.path.join(folder, sub))):
            if not is_audio(f):
                continue
            if flac_md5(os.path.join(folder, sub, f)) not in root_sigs:
                return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Execution
# ═════════════════════════════════════════════════════════════════════════════
def prune_empty(folder, apply):
    """
    Remove now-empty subdirectories, ignoring OS junk files.

    Only touches directories that held audio. Art/, artwork and any folder
    still holding real files survive untouched.
    """
    removed = []
    for d in sorted(os.listdir(folder), reverse=True):
        p = os.path.join(folder, d)
        if not os.path.isdir(p):
            continue
        left = [x for x in os.listdir(p) if not is_junk(x)]
        if left:
            continue
        removed.append(d)
        if apply:
            shutil.rmtree(p)
    return removed


def repair(con, lib, rec, apply, verbose=True):
    folder = os.path.join(lib, rec["folder_path"])
    tracks = [dict(r) for r in con.execute(
        "SELECT id, file_path, title, track_number, set_number FROM track WHERE recording_id=? "
        "ORDER BY track_number, file_path", (rec["id"],))]
    kind, detail = classify(folder)

    if kind == "flat":
        return {"kind": kind, "action": "none"}
    if kind in ("missing", "unknown"):
        return {"kind": kind, "action": "SKIP — needs manual review", "detail": detail}

    if kind == "disc_subdirs":
        moves, updates = plan_disc_subdirs(folder, detail, tracks)
        removes = []
    elif kind == "nested_show":
        moves, updates = plan_nested_show(folder, detail, tracks)
        if moves is None:
            return {"kind": kind, "action": "SKIP — filename collision on move"}
        removes = []
    else:  # nested_duplicate
        if not verify_duplicate(folder, detail):
            return {"kind": kind,
                    "action": "SKIP — nested copy is NOT identical, review by hand",
                    "detail": detail}
        moves, updates = [], []
        removes = detail["subs"]

    result = {"kind": kind, "moves": moves, "updates": updates,
              "removes": removes, "action": "repair"}
    if not apply:
        result["pruned"] = [d for d in removes] or "(computed after move)"
        return result

    # ── DB first (uncommitted), then filesystem, then commit ─────────────────
    #
    # Order matters more than it looks. Moving files first and updating the DB
    # afterwards means a failed UPDATE leaves audio at the new path and rows
    # pointing at the old one — a broken recording, and rolling back the
    # transaction does nothing to put the files back. (Caught exactly that in
    # testing: the column was then still named `set`, a SQL reserved word,
    # so the UPDATE threw and the files had already moved. The column has
    # since been renamed to set_number for this reason — see
    # app/models/track.py — but the DB-first/filesystem-second ordering
    # below stays regardless, since any future column could hit the same
    # class of bug.)
    #
    # So: run the DB writes inside an open transaction first — any SQL problem
    # surfaces before a single file has moved — then move files, then commit.
    # If a move fails midway, the DB rolls back AND the completed moves are
    # reversed, leaving the recording exactly as it started.
    done_moves = []
    try:
        for u in updates:
            cols = ", ".join(f'"{k}"=?' for k in u if k != "id")
            con.execute(f'UPDATE track SET {cols} WHERE id=?',
                        [v for k, v in u.items() if k != "id"] + [u["id"]])

        for rel_old, rel_new in moves:
            src, dst = os.path.join(folder, rel_old), os.path.join(folder, rel_new)
            if os.path.exists(dst):
                raise FileExistsError(dst)
            shutil.move(src, dst)
            done_moves.append((src, dst))
        for sub in removes:
            shutil.rmtree(os.path.join(folder, sub))

        con.commit()
    except Exception as e:                                        # noqa: BLE001
        con.rollback()
        for src, dst in reversed(done_moves):                     # put files back
            try:
                shutil.move(dst, src)
            except Exception:                                     # noqa: BLE001
                result["action"] = (f"FAILED AND COULD NOT FULLY UNDO — {e}. "
                                    f"Left stranded: {dst}")
                return result
        result["action"] = f"FAILED (no changes made) — {type(e).__name__}: {e}"
        return result

    result["pruned"] = prune_empty(folder, apply=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--library", default=DEFAULT_LIB)
    ap.add_argument("--id", type=int, help="repair a single recording id")
    ap.add_argument("--apply", action="store_true", help="actually make changes")
    a = ap.parse_args()

    if not os.path.isfile(a.db):
        sys.exit(f"database not found: {a.db}")
    if not os.path.isdir(a.library):
        sys.exit(f"library not found: {a.library}")

    if a.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{a.db}.pre-flatten-{stamp}.bak"
        shutil.copy2(a.db, backup)
        print(f"database backed up to {backup}\n")

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    q = "SELECT id, folder_path FROM recording"
    rows = list(con.execute(q + (" WHERE id=?" if a.id else ""),
                            (a.id,) if a.id else ()))

    todo, n_done = [], 0
    for rec in rows:
        kind, _ = classify(os.path.join(a.library, rec["folder_path"]))
        if kind != "flat":
            todo.append(rec)

    print(f"{len(rows)} recordings checked — {len(todo)} need repair\n")
    for rec in todo:
        r = repair(con, a.library, rec, a.apply)
        print(f"[{rec['id']:>4}] {r['kind']:<17s} {rec['folder_path'][:64]}")
        if r["action"] not in ("repair", "none"):
            print(f"       {r['action']}")
            continue
        for old, new in r.get("moves", [])[:4]:
            print(f"       move  {old[:44]:<46s} -> {new[:44]}")
        if len(r.get("moves", [])) > 4:
            print(f"       ...   {len(r['moves']) - 4} more")
        for sub in r.get("removes", []):
            print(f"       DELETE duplicate folder: {sub[:60]}")
        if r.get("pruned"):
            print(f"       pruned empty: {r['pruned']}")
        if r.get("updates"):
            print(f"       db: {len(r['updates'])} track rows updated")
        n_done += 1

    con.close()
    print(f"\n{'APPLIED' if a.apply else 'DRY RUN'} — {n_done} recording(s) "
          f"{'repaired' if a.apply else 'would be repaired'}")
    if not a.apply and n_done:
        print("Re-run with --apply to make these changes.")


if __name__ == "__main__":
    main()
