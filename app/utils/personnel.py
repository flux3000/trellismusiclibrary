"""
utils/personnel.py — resolve who played a given Performance.

Single source of truth for inherit vs. explicit personnel resolution (see
Context Library/Per-Show Personnel — Design Plan §3). Every endpoint/view
that needs a show's lineup must go through resolve_performance_personnel()
rather than querying Membership or PerformancePersonnel directly, so display
and any future authorization/aggregation logic can never disagree.
"""

import calendar

from sqlalchemy import func
from app.extensions import db
from app.models.performance_personnel import PerformancePersonnel
from app.utils.artists import resolve_or_create_musician


def _floor_date(y, m, d):
    """Earliest possible day for a partial date. None if year itself unknown."""
    if y is None:
        return None
    return (y, m or 1, d or 1)


def _ceil_date(y, m, d):
    """Latest possible day for a partial date. None if year itself unknown."""
    if y is None:
        return None
    m = m or 12
    d = d or calendar.monthrange(y, m)[1]
    return (y, m, d)


def _stint_covers(membership, performance):
    """
    True if `membership`'s stint bounds cover `performance`'s date.

    Bounds are inclusive and forgiving of missing month/day (see Membership's
    docstring): a NULL start means "always started"; a NULL end means "never
    ended." A performance with no year at all can't be dated against a stint
    at all, so it's treated as covered — better to over-include a roster
    member than silently drop one because the show's date is unknown.
    """
    perf_floor = _floor_date(performance.start_year, performance.start_month,
                              performance.start_day)
    perf_ceil = _ceil_date(performance.start_year, performance.start_month,
                            performance.start_day)
    if perf_floor is None:
        return True

    start_floor = _floor_date(membership.start_year, membership.start_month,
                               membership.start_day)
    end_ceil = _ceil_date(membership.end_year, membership.end_month,
                           membership.end_day)

    if start_floor is not None and perf_ceil < start_floor:
        return False
    if end_ceil is not None and perf_floor > end_ceil:
        return False
    return True


def _row_dict(row, source):
    return {
        "id":         row.id,          # PerformancePersonnel row id; None for inherited-only entries
        "musician_id":  row.musician_id,
        "name":       row.musician.name,
        "instrument": row.instrument,
        "order":      row.order,
        "is_guest":   row.is_guest,
        "note":       row.note,
        "source":     source,
    }


def resolve_performance_personnel(performance):
    """
    Resolve the ordered lineup for a Performance according to its
    personnel_mode. Returns a list of dicts:
        {musician_id, name, instrument, order, is_guest, note, source}
    `source` is one of:
        'inherited' — from the act roster via a Membership stint covering
                      this date
        'guest'     — a PerformancePersonnel row layered on top of an
                      inherited lineup (mode='inherit'), is_guest=True
        'added'     — a PerformancePersonnel row layered on top of an
                      inherited lineup (mode='inherit'), is_guest=False —
                      someone added as a full member for just this show
                      without being on the act's roster (2026-07-22, Members/
                      Guests two-row UI)
        'explicit'  — a PerformancePersonnel row in mode='explicit'; the act
                      roster is not consulted at all (may be is_guest True or
                      False — the Members/Guests split still applies within
                      explicit mode)

    Regardless of `source`, `is_guest` is always the row's own flag (or False
    for a virtual 'inherited' entry) — that's what the Members/Guests UI
    splits on, not `source`. `source` exists for the case-5 "was this a
    roster member or an addition" distinction inside sync_performance_personnel.

    'explicit' mode ignores the act roster entirely (e.g. Acoustic All-Stars,
    where the roster is just a pick-list of usual suspects, not a truth
    source — see design doc §3A "Refinement").

    The return value is always deduped by musician_id (Ryan, 2026-07-23 bug
    report — see _dedupe_by_musician below): the same person must never appear
    twice, however the duplication arose.
    """
    if performance.personnel_mode == "explicit":
        rows = sorted(performance.personnel, key=lambda r: r.order)
        return _dedupe_by_musician([_row_dict(r, "explicit") for r in rows])

    # inherit: dedupe stints per musician (a person may have multiple stint
    # rows, e.g. Mickey Hart) — include them once if ANY stint covers this
    # date, with an order taken from their earliest stint per the design
    # doc's display-dedupe rule.
    stints_by_musician = {}
    for m in performance.artist.memberships:
        stints_by_musician.setdefault(m.musician_id, []).append(m)

    inherited = []
    for musician_id, stints in stints_by_musician.items():
        covering = [m for m in stints if _stint_covers(m, performance)]
        if not covering:
            continue
        order = min(m.order for m in stints)
        musician = stints[0].musician
        inherited.append({
            "id":         None,
            "musician_id":  musician_id,
            "name":       musician.name,
            "instrument": None,
            "order":      order,
            "is_guest":   False,
            "note":       None,
            "source":     "inherited",
        })
    inherited.sort(key=lambda d: d["order"])

    # Any performance_personnel row on top of the inherited roster — in inherit
    # mode these are always ADDITIONS (case 4/5), but "addition" isn't the same
    # as "guest": the Members/Guests two-row UI (2026-07-22) can add a real
    # is_guest=False row here too (someone subbing in as a full member for
    # just this show, not on the act's roster). Label source by the row's own
    # is_guest flag rather than hardcoding "guest" for everything, so `source`
    # stays trustworthy for anyone reading it — the two-row UI itself splits
    # purely on `is_guest`, not on this label, but it should still be accurate.
    added_rows = sorted(performance.personnel, key=lambda r: r.order)
    added = [_row_dict(r, "guest" if r.is_guest else "added") for r in added_rows]

    return _dedupe_by_musician(inherited + added)


def _dedupe_by_musician(rows):
    """
    Collapse to one entry per musician_id, first occurrence wins.

    Bug this fixes (Ryan, 2026-07-23 — JD Crowe & the New South, recording
    #239): a Performance's own performance_personnel row (added via the
    recording page's Members/Guests widget, source 'added'/'guest') persists
    independently of the act's Membership roster. If that SAME person is
    later added to the act's roster too — a completely reasonable, separate
    action on the Artist page — resolve_performance_personnel would
    previously return them TWICE: once as the new 'inherited' roster entry,
    once as the now-redundant leftover 'added' row. The UI showed two
    identical pills, and removing either one silently failed — the
    name-based diffing in sync_performance_personnel couldn't tell them
    apart, since as long as the OTHER duplicate still carried that name, the
    set-membership check never saw the name as "dropped."
    Since `inherited` is always built before `added`/explicit rows are
    appended, first-occurrence-wins means a roster (inherited) entry always
    takes priority over a redundant added/guest row for the same musician —
    correct, because it makes removing that pill go through the normal
    "drop an inherited member from this one show" flow (case 5 in
    sync_performance_personnel), which also sweeps up the stale leftover
    row as a side effect of that flip's full-rewrite semantics.
    """
    seen = set()
    deduped = []
    for r in rows:
        if r["musician_id"] in seen:
            continue
        seen.add(r["musician_id"])
        deduped.append(r)
    return deduped


def _clean_names(names):
    out, seen = [], set()
    for n in (names or []):
        n = (n or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def sync_performance_personnel(performance, member_names=None, guest_names=None):
    """
    Reconcile a Performance's resolved lineup against two SEPARATE lists —
    the write side of the Members/Guests two-row UI (2026-07-22, replacing
    the old single ambiguous list + guest-vs-member inference by diffing).
    `is_guest` on each row is now simply which list the frontend put the
    name in, not an inference — that ambiguity is gone because the UI no
    longer has one row to be ambiguous about.

    Either list may be omitted (None, not just empty) to mean "leave that
    bucket exactly as currently resolved" — distinct from an empty list,
    which means "clear this bucket." Callers that only care about one row
    (e.g. removing a single guest pill) can pass just that list.

    Members are still the tricky bucket, because a 'member' pill may be
    virtual (an 'inherited' row from the act's Membership stints — no
    performance_personnel row of its own) or a real row (an 'explicit' row
    with is_guest=False). Dropping an EXPLICIT member is a plain row delete.
    Dropping an INHERITED member can't edit the act's global stint dates away
    from one show's form — per the design doc's case 5, it instead switches
    THIS performance to 'explicit' mode and snapshots the rest of the
    surviving lineup (members and guests both) so nobody visually vanishes
    or reappears from the flip itself.

    Guests are always real rows regardless of mode (the inherit-mode
    resolver never marks a Membership-derived row as a guest), so they're
    plain add/remove-by-id — no mode-flip case exists for them.
    """
    resolved = resolve_performance_personnel(performance)
    cur_members = [r for r in resolved if not r["is_guest"]]
    cur_guests  = [r for r in resolved if r["is_guest"]]

    if member_names is None:
        member_names = [r["name"] for r in cur_members]
    if guest_names is None:
        guest_names = [r["name"] for r in cur_guests]
    member_names = _clean_names(member_names)
    guest_names  = _clean_names(guest_names)
    target_member_lower = {n.lower() for n in member_names}
    target_guest_lower  = {n.lower() for n in guest_names}

    dropped_inherited = [r for r in cur_members
                         if r["source"] == "inherited" and r["name"].lower() not in target_member_lower]
    dropped_members   = [r for r in cur_members
                         if r["source"] != "inherited" and r["name"].lower() not in target_member_lower]
    dropped_guests    = [r for r in cur_guests if r["name"].lower() not in target_guest_lower]

    cur_member_lower = {r["name"].lower() for r in cur_members}
    cur_guest_lower  = {r["name"].lower() for r in cur_guests}
    added_members = [n for n in member_names if n.lower() not in cur_member_lower]
    added_guests  = [n for n in guest_names  if n.lower() not in cur_guest_lower]

    if dropped_inherited:
        # Case 5: an act-roster member is being marked absent from this one
        # show. Flip to explicit and rewrite EVERYTHING (members + guests)
        # from the pre-edit resolved state, applying this call's other
        # adds/drops on top — a full snapshot, not a partial patch, so the
        # flip itself never makes anyone else appear or disappear.
        drop_lower = {r["name"].lower() for r in dropped_inherited} | \
                     {r["name"].lower() for r in dropped_members}
        keep_members = [r for r in cur_members if r["name"].lower() not in drop_lower]
        keep_guests  = [r for r in cur_guests
                        if r["name"].lower() not in {d["name"].lower() for d in dropped_guests}]

        performance.personnel_mode = "explicit"
        db.session.query(PerformancePersonnel).filter_by(
            performance_id=performance.id).delete(synchronize_session=False)
        db.session.flush()

        order = 0
        for r in keep_members:
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=r["musician_id"], order=order,
                instrument=r["instrument"], note=r["note"], is_guest=False))
            order += 1
        for n in added_members:
            musician = resolve_or_create_musician(n)
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=musician.id, order=order, is_guest=False))
            order += 1
        for r in keep_guests:
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=r["musician_id"], order=order,
                instrument=r["instrument"], note=r["note"], is_guest=True))
            order += 1
        for n in added_guests:
            musician = resolve_or_create_musician(n)
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=musician.id, order=order, is_guest=True))
            order += 1
        db.session.flush()
        return

    # No inherited member dropped: stay in the current mode, reconcile rows
    # directly — plain deletes for anything dropped, plain inserts for
    # anything added, tagged is_guest by which list it came from.
    for r in dropped_members + dropped_guests:
        if r["id"] is not None:
            db.session.query(PerformancePersonnel).filter_by(id=r["id"]).delete(
                synchronize_session=False)

    if added_members or added_guests:
        base_order = db.session.query(func.max(PerformancePersonnel.order)).filter_by(
            performance_id=performance.id).scalar()
        base_order = (base_order + 1) if base_order is not None else 0
        i = 0
        for n in added_members:
            musician = resolve_or_create_musician(n)
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=musician.id, order=base_order + i, is_guest=False))
            i += 1
        for n in added_guests:
            musician = resolve_or_create_musician(n)
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=musician.id, order=base_order + i, is_guest=True))
            i += 1

    db.session.flush()


def set_performance_personnel_mode(performance, mode):
    """
    Manually toggle a Performance's personnel_mode — distinct from the
    automatic flip inside sync_performance_personnel_from_names' case 5
    (dropping an inherited member). This is the explicit "Inherit / Explicit"
    control on the recording page.

      inherit -> explicit: snapshot the CURRENTLY resolved lineup into
        performance_personnel rows first, so flipping the toggle never makes
        anyone visually disappear — you start explicit mode looking at
        exactly what you had a second ago, then edit from there.

      explicit -> inherit: clear performance_personnel entirely and revert
        to the act's plain resolved roster. Deliberately a clean revert, not
        a merge — keeping the old explicit rows around as bonus "guests"
        after switching back could silently double-list someone who's
        genuinely on the act roster (they'd show once as inherited, once as
        a leftover guest row).

    No-op if already in the requested mode.
    """
    if mode not in ("inherit", "explicit"):
        raise ValueError(f"invalid personnel_mode: {mode!r}")
    if mode == performance.personnel_mode:
        return

    if mode == "explicit":
        resolved = resolve_performance_personnel(performance)
        db.session.query(PerformancePersonnel).filter_by(
            performance_id=performance.id).delete(synchronize_session=False)
        db.session.flush()
        for i, r in enumerate(resolved):
            db.session.add(PerformancePersonnel(
                performance_id=performance.id, musician_id=r["musician_id"], order=i,
                instrument=r["instrument"], is_guest=r["is_guest"], note=r["note"]))
    else:
        db.session.query(PerformancePersonnel).filter_by(
            performance_id=performance.id).delete(synchronize_session=False)

    performance.personnel_mode = mode
    db.session.flush()
