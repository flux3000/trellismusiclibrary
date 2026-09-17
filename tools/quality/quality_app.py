#!/usr/bin/env python3
"""
quality_app.py — Listening Quality, standalone.

A self-contained mini-app: point it at a folder, press Analyse, read the
result. Runs entirely independently of Flux — its own server, its own UI, no
database, no config. The scoring engine is imported unchanged from
quality_features.py / quality_scoring.py, so results are identical to the CLI
and to whatever eventually ships inside Flux.

    python3 quality_app.py                 # serve on http://127.0.0.1:5055
    python3 quality_app.py --port 8080
    python3 quality_app.py --open ~/Music  # prefill the path box

Requires: flask, numpy, scipy, soundfile, pyloudnorm

Design note: the whole point of a standalone build is that the analysis engine
has to stand on its own before it is welded into the app. Anything this needs
that Flux would supply (track flags, DB rows, library root) is a dependency we
do not want the engine to have.
"""

import os
import re
import sys
import json
import glob
import threading
import unicodedata
from flask import Flask, request, jsonify, Response, send_file

# The engine now lives in the app (app/utils/quality/) so there is exactly one
# copy — moved 2026-07-30 when Listening Quality was integrated into ingestion.
# This harness is a thin client of it. Repo root goes on sys.path so
# `app.utils.quality` resolves when running this file directly.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, _REPO_ROOT)
from app.utils.quality import (                           # noqa: E402
    extract_recording_features,
    score_recording,
    guess_source_from_name,
    interpret_full,
)

app = Flask(__name__)

# Job state. In-memory and single-user by design — this is a local tool.
JOBS = {}
_lock = threading.Lock()

AUDIO_EXT = (".flac", ".wav", ".aiff", ".aif")

# Where the folder picker opens. Matches config.IMPORT_DIR in the Flux app —
# new material lands here before ingest, so it is what you almost always want
# to analyse. Falls back to the library, then home, if it doesn't exist.
DEFAULT_DIRS = [
    os.environ.get("IMPORT_DIR", "/Volumes/music/Flux Audio/Download"),
    "/Volumes/music/Flux Audio/Library",
    os.path.expanduser("~"),
]


def default_dir():
    for d in DEFAULT_DIRS:
        if d and os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def _norm(s):
    """macOS gives decomposed filenames; normalise so comparisons behave."""
    return unicodedata.normalize("NFC", s)


def find_recordings(root):
    """
    A "recording" is any directory containing audio, at any depth.

    Handles both shapes found in real libraries: a artist folder holding
    many show folders, and a single show folder pointed at directly. Disc
    subdirectories (CD1/, CD2/) are folded into their parent rather than
    treated as separate recordings.
    """
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return []

    with_audio = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        if any(f.lower().endswith(AUDIO_EXT) for f in filenames):
            with_audio.add(dirpath)

    # Fold disc subdirs into the show folder above them
    folded = set()
    for d in with_audio:
        parent = os.path.dirname(d)
        if re.match(r"^(cd|disc|disk|set|vol|volume|part|tape|show)\s*\d+$",
                    os.path.basename(d), re.I) and parent.startswith(root):
            folded.add(parent)
        else:
            folded.add(d)

    # Drop any folder that is an ancestor of another candidate, unless it holds
    # audio itself — prevents a artist folder being scored as one recording.
    out = []
    for d in sorted(folded):
        has_own = any(f.lower().endswith(AUDIO_EXT) for f in os.listdir(d)) \
            if os.path.isdir(d) else False
        deeper = any(o != d and o.startswith(d + os.sep) for o in folded)
        if has_own or not deeper:
            out.append(d)
    return sorted(out)


# Source-from-folder-name lives in the ENGINE (quality_scoring) so this harness
# and app/api/quality.py cannot drift apart — both need it at triage time, when
# no Recording row exists yet. Imported above with the rest of the engine.
def _guess_source(name):
    return guess_source_from_name(name)


def run_job(job_id, root):
    """Analyse every recording under `root`, updating job state as it goes."""
    folders = find_recordings(root)
    with _lock:
        JOBS[job_id].update(total=len(folders), done=0, results=[], state="running")

    if not folders:
        with _lock:
            JOBS[job_id].update(state="error", error="No audio files found in that folder.")
        return

    for i, folder in enumerate(folders):
        try:
            feats = extract_recording_features(folder)
            # Display the path RELATIVE to the scan root, not just the basename.
            # Some folders in the library contain a nested second copy of the
            # same show (the 1976 Paris FM recording has an "(FM A)" folder
            # inside it holding the same 11 tracks). Showing the relative path
            # makes that visible instead of rendering two identical-looking
            # cards.
            rel = os.path.relpath(folder, JOBS[job_id]["root"])
            name = _norm(os.path.basename(folder) if rel == "." else rel)
            if "error" in feats:
                entry = {"folder": folder, "name": name, "error": feats["error"]}
            else:
                # Source is read off the folder name — "(SBD)", "(AUD)", "(FM)",
                # "(MTX)" is near-universal in collector naming, and it is what
                # `recording.source` gets populated from at ingest anyway.
                # Source is the strongest single predictor of grade we have
                # (CV r = +0.314 alone), so the harness must exercise it or it
                # is not measuring what the app measures.
                src = _guess_source(name)
                scored = score_recording(feats, source=src)
                entry = {
                    "folder": folder,
                    "name": name,
                    "source": src,
                    "scores": scored,
                    "interpretation": interpret_full(scored, feats),
                    "sampled": feats.get("sampled", []),
                    "flags": scored.get("flags", []),
                    # RAW FEATURES are exposed here and deliberately NOT in the
                    # Flux app. Per Ryan 2026-07-31 this harness is the
                    # development surface for the quantitative score, so it
                    # needs everything the engine saw, not just the verdict.
                    "features": {k: v for k, v in feats.items()
                                 if isinstance(v, (int, float, bool)) or v is None},
                }
        except Exception as e:                                  # noqa: BLE001
            entry = {"folder": folder,
                     "name": os.path.relpath(folder, JOBS[job_id]["root"]),
                     "error": f"{type(e).__name__}: {e}"}
        with _lock:
            JOBS[job_id]["results"].append(entry)
            JOBS[job_id]["done"] = i + 1

    with _lock:
        JOBS[job_id]["state"] = "done"


@app.route("/api/browse")
def browse():
    """
    List sub-folders of a path so the UI can offer a real directory picker.

    A browser can't hand back a usable absolute path (`webkitdirectory` gives
    names, not paths), so navigation has to happen server-side. Bound to
    localhost by default, on the user's own machine, listing their own files.

    Reports which folders contain audio so the picker can mark what is
    analysable before you click into it.
    """
    path = request.args.get("path", "").strip() or default_dir()
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return jsonify({"error": f"Not a folder: {path}"}), 400
    try:
        entries = sorted(os.listdir(path), key=lambda s: s.lower())
    except PermissionError:
        return jsonify({"error": f"No permission to read {path}"}), 403

    dirs = []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        if not os.path.isdir(full):
            continue
        try:
            kids = os.listdir(full)
        except (PermissionError, OSError):
            kids = []
        has_audio = any(k.lower().endswith(AUDIO_EXT) for k in kids)
        subdirs = [k for k in kids
                   if not k.startswith(".") and os.path.isdir(os.path.join(full, k))]
        # Look one level deeper before declaring a folder audio-free. Recordings
        # that still carry CD1/ CD2/ disc subdirs hold no audio at their own
        # root, and marking them empty would tell the user a perfectly
        # analysable show has nothing in it.
        if not has_audio:
            for sub in subdirs:
                try:
                    if any(k.lower().endswith(AUDIO_EXT)
                           for k in os.listdir(os.path.join(full, sub))):
                        has_audio = True
                        break
                except (PermissionError, OSError):
                    continue
        has_subdirs = bool(subdirs)
        dirs.append({"name": name, "path": full,
                     "audio": has_audio, "subdirs": has_subdirs})

    parent = os.path.dirname(path.rstrip(os.sep)) or "/"
    here_audio = any(e.lower().endswith(AUDIO_EXT) for e in entries)
    return jsonify({
        "path": path,
        "parent": None if parent == path else parent,
        "dirs": dirs,
        "here_has_audio": here_audio,
        "shortcuts": _shortcuts(),
    })


def _shortcuts():
    """Import folder first, then library, home, all volumes. Deduped."""
    out = []
    for s in (default_dir(),
              os.environ.get("LIBRARY_ROOT", "/Volumes/music/Flux Audio/Library"),
              os.path.expanduser("~"), "/Volumes"):
        if s and os.path.isdir(s) and s not in out:
            out.append(s)
    return out


@app.route("/api/analyse", methods=["POST"])
def analyse():
    root = (request.json or {}).get("path", "").strip()
    if not root:
        return jsonify({"error": "No path given"}), 400
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return jsonify({"error": f"Not a folder: {root}"}), 400
    job_id = os.urandom(8).hex()
    with _lock:
        JOBS[job_id] = {"state": "starting", "total": 0, "done": 0,
                        "results": [], "root": root}
    threading.Thread(target=run_job, args=(job_id, root), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def job(job_id):
    with _lock:
        j = JOBS.get(job_id)
        return jsonify(j) if j else (jsonify({"error": "unknown job"}), 404)


@app.route("/api/stream")
def stream():
    """
    Serve a sampled track with HTTP Range support so the browser can seek.

    Only paths inside a folder this session actually analysed are served —
    without that check this endpoint would read any file on the machine.
    """
    path = request.args.get("path", "")
    path = os.path.abspath(os.path.expanduser(path))
    with _lock:
        roots = [j["root"] for j in JOBS.values()]
    if not any(path.startswith(r + os.sep) or path == r for r in roots):
        return jsonify({"error": "path not in an analysed folder"}), 403
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404

    size = os.path.getsize(path)
    rng = request.headers.get("Range")
    mime = {"flac": "audio/flac", "wav": "audio/wav",
            "aiff": "audio/aiff", "aif": "audio/aiff"}.get(
        path.rsplit(".", 1)[-1].lower(), "application/octet-stream")

    if not rng:
        return send_file(path, mimetype=mime, conditional=True)

    m = re.match(r"bytes=(\d+)-(\d*)", rng)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    length = end - start + 1

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(gen(), 206, mimetype=mime, direct_passthrough=True)
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    return resp


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return send_file(os.path.join(here, "quality_app.html"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Listening Quality — standalone analyser")
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", dest="prefill", default="",
                    help="prefill the folder path box")
    a = ap.parse_args()
    app.config["PREFILL"] = a.prefill
    print(f"\n  Listening Quality  →  http://{a.host}:{a.port}\n")
    app.run(host=a.host, port=a.port, debug=False, threaded=True)
