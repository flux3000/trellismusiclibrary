#!/usr/bin/env python3
"""
copy_review.py — the review surface for Trellis copy.

    source .venv/bin/activate
    python3 tools/copy_review.py              # http://127.0.0.1:5056
    python3 tools/copy_review.py --port 8080

A local harness, same shape as tools/quality/quality_app.py: its own server,
its own page, no database. Everything it knows comes from
`app/utils/copyreview`, which is where the capability lives — this file is a
thin client of it, so the gate in tests/ and the `--report` CLI and this page
can never disagree about what is unreviewed.

It serves the repo's own `app/static/` so the page wears the real stylesheet
and the real self-hosted faces. Copy has to be judged in the skin it ships in,
and a second set of design tokens here would drift from the first.

Decisions are saved to `app/utils/copyreview/ledger/copy_ledger.json` the
moment they are made, because losing an afternoon of judgement to a crashed
tab would be worse than the extra write. The SOURCE FILES are not touched
until "Write to source" is pressed, and that path runs every guard in
writeback.py.
"""

import argparse
import os
import sys
import threading
import webbrowser

from flask import Flask, jsonify, request, send_file, send_from_directory

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _REPO_ROOT)

from pathlib import Path                                          # noqa: E402

from app.utils.copyreview import writeback                        # noqa: E402
from app.utils.copyreview.extract import extract                  # noqa: E402
from app.utils.copyreview.group import SCENARIOS                  # noqa: E402
from app.utils.copyreview.ledger import Baseline, Ledger, VERDICTS   # noqa: E402

app = Flask(__name__, static_folder=None)
ROOT = Path(_REPO_ROOT)

_lock = threading.Lock()
_state: dict = {}


def load(include_parked: bool = False) -> dict:
    """Extract, match against the ledger, and cache the result."""
    extraction = extract(ROOT)
    ledger = Ledger.load(ROOT)
    baseline = Baseline.load(ROOT)
    matches = ledger.match(extraction)
    return {
        "extraction": extraction,
        "ledger": ledger,
        "baseline": baseline,
        "matches": {m.unit.key: m for m in matches},
        "include_parked": include_parked,
    }


def units_in_scope() -> list:
    extraction = _state["extraction"]
    return extraction.units if _state["include_parked"] else extraction.reviewable()


def site_json(site) -> dict:
    return {
        "file": site.file,
        "line": site.line,
        "kind": site.kind,
        "section": site.section,
        "scenario": site.scenario,
        "function": site.function,
        "page": site.page,
        "attr": site.attr,
        "quote": site.quote,
        "reason": site.reason,
        "holes": site.hole_sources,
        "form": site.form,
        "writable": bool(site.spans),
    }


def unit_json(unit) -> dict:
    match = _state["matches"].get(unit.key)
    entry = match.entry if match else None
    return {
        "key": unit.key,
        "text": unit.text,
        "raw": unit.raw,
        "variants": unit.variants,
        "section": unit.section,
        "scenario": unit.scenario,
        "page": unit.page,
        "userFacing": unit.user_facing,
        "neverSeen": unit.never_seen,
        "emDash": unit.em_dash,
        "occurrences": unit.occurrences,
        "sites": [site_json(s) for s in unit.sites],
        "status": match.status if match else "new",
        "was": match.was if match else None,
        "verdict": entry.verdict if entry else None,
        "replacement": entry.replacement if entry else None,
        "note": entry.note if entry else "",
        "inBaseline": unit.key in _state["baseline"].keys,
    }


@app.route("/api/state")
def api_state():
    with _lock:
        units = units_in_scope()
        extraction = _state["extraction"]
        ledger = _state["ledger"]
        pending = [e.id for e in ledger.entries.values() if e.needs_write]
        return jsonify(
            {
                "units": [unit_json(u) for u in units],
                "scenarios": list(SCENARIOS),
                "verdicts": list(VERDICTS),
                "guardTokens": sorted(extraction.guard_tokens),
                "includeParked": _state["include_parked"],
                "pending": pending,
                "counts": {
                    "userFacing": len(extraction.reviewable()),
                    "all": len(extraction.units),
                    "neverSeen": len(extraction.never_seen()),
                    "emDash": len(extraction.em_dashed()),
                    "decided": len(ledger.entries),
                    "baseline": len(_state["baseline"].keys),
                },
            }
        )


@app.route("/api/source")
def api_source():
    """The lines around a site, plus whatever comments sit above it.

    A string means something different depending on what is around it, and a
    comment above it is often the only record of why it says what it says.
    """
    rel = request.args.get("file", "")
    try:
        line = int(request.args.get("line", "1"))
    except ValueError:
        return jsonify({"error": "line must be a number"}), 400
    span = max(1, min(40, int(request.args.get("span", "8"))))

    path = (ROOT / rel).resolve()
    if not str(path).startswith(str(ROOT)) or not path.exists():
        return jsonify({"error": "not a file in this repo"}), 400

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, line - 1 - span)
    end = min(len(lines), line + span)
    return jsonify(
        {
            "file": rel,
            "first": start + 1,
            "focus": line,
            "lines": lines[start:end],
        }
    )


@app.route("/api/decide", methods=["POST"])
def api_decide():
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    verdict = body.get("verdict")
    with _lock:
        unit = next((u for u in _state["extraction"].units if u.key == key), None)
        if unit is None:
            return jsonify({"error": "no such string"}), 404
        try:
            _state["ledger"].decide(
                unit,
                verdict,
                replacement=body.get("replacement"),
                note=body.get("note", ""),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # A decision also retires this string from the gate's backlog, so the
        # ratchet tightens as the queue drains rather than by hand.
        removed = _state["baseline"].prune(_state["ledger"])
        _state["ledger"].save()
        if removed:
            _state["baseline"].save()
        _state["matches"] = {m.unit.key: m for m in _state["ledger"].match(_state["extraction"])}
        return jsonify(unit_json(unit))


@app.route("/api/undo", methods=["POST"])
def api_undo():
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    with _lock:
        _state["ledger"].undo(key)
        _state["ledger"].save()
        _state["matches"] = {m.unit.key: m for m in _state["ledger"].match(_state["extraction"])}
        unit = next((u for u in _state["extraction"].units if u.key == key), None)
        return jsonify(unit_json(unit) if unit else {"key": key})


@app.route("/api/write", methods=["POST"])
def api_write():
    """Dry run by default. `apply` writes, and only if every guard passes."""
    body = request.get_json(silent=True) or {}
    apply_now = bool(body.get("apply"))
    only = set(body["only"]) if body.get("only") else None
    with _lock:
        plan = writeback.build(
            _state["extraction"],
            _state["ledger"],
            only=only,
            confirm_bare_removal=bool(body.get("confirmEmptyLiterals")),
        )
        payload = {
            "splices": len(plan.splices),
            "files": sorted(plan.by_file()),
            "warnings": plan.warnings,
            "refusals": plan.refusals,
            "diff": writeback.diff(ROOT, plan) if plan.splices else "",
        }
        result = writeback.apply(
            ROOT,
            _state["extraction"],
            plan,
            dry_run=not apply_now,
            ledger=_state["ledger"] if apply_now else None,
        )
        payload["refused"] = result.refused
        payload["written"] = result.files if apply_now and not result.refused else []
        if payload["written"]:
            # The plan's byte ranges now point at the old bytes, so the only
            # correct next move is a fresh extraction.
            _state.update(load(_state["include_parked"]))
        return jsonify(payload)


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    body = request.get_json(silent=True) or {}
    with _lock:
        _state.update(load(bool(body.get("includeParked", _state["include_parked"]))))
        return api_state()


@app.route("/static/<path:name>")
def static_files(name: str):
    """The app's own stylesheet and faces, served from the repo."""
    return send_from_directory(os.path.join(_REPO_ROOT, "app", "static"), name)


@app.route("/")
def index():
    return send_file(os.path.join(_HERE, "copy_review.html"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Review every string Trellis shows a user.")
    parser.add_argument("--port", type=int, default=5056)
    parser.add_argument(
        "--include-parked",
        action="store_true",
        help="also show strings the oracles judged not user-facing",
    )
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    print("Extracting…")
    _state.update(load(args.include_parked))
    counts = _state["extraction"]
    print(
        f"  {len(counts.reviewable())} user-facing strings, "
        f"{len(counts.never_seen())} unreachable by clicking, "
        f"{len(_state['ledger'].entries)} already decided"
    )
    url = f"http://127.0.0.1:{args.port}/"
    print(f"  {url}")
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
