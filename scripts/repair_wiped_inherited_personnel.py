"""scripts/repair_wiped_inherited_personnel.py

One-time repair for a bug in _do_confirm: Batch Import's "Auto-Ingest" path
never sends "members"/ "guests" in its confirm payload at all (no review
wizard, no pre-fill). That None got collapsed to [] before reaching
sync_performance_personnel, which reads [] as "the user just cleared every
member" — tripping its case-5 safeguard (an act-roster member disappearing from
one show flips that show to personnel_mode='explicit' and snapshots the
surviving lineup). With nothing in the list to keep, the snapshot was empty:
the performance flipped to 'explicit' with zero performance_personnel rows, and
its Members row on View Recording came out blank even though the performer's
own roster was intact.

This finds every performance matching that exact signature — mode='explicit',
zero performance_personnel rows, AND the performer has at least one
Membership (so reverting to inherit actually restores something) — and
reverts personnel_mode back to 'inherit'. This is a clean, lossless revert:
there are zero performance_personnel rows to lose in either direction, same
as set_performance_personnel_mode()'s explicit→inherit path.

A performance with mode='explicit' and zero rows AND a performer with NO
roster at all is left alone — that's a legitimately personnel-less show
(nothing to revert to), not this bug's signature.

Idempotent: rerunning finds nothing (already-reverted rows are back in
'inherit' mode, which no longer matches the query).
Run once from the repo root:  python3 scripts/repair_wiped_inherited_personnel.py
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT p.id, per.name AS performer_name, p.start_year, p.start_month, p.start_day
        FROM performance p
        JOIN performer per ON per.id = p.performer_id
        WHERE p.personnel_mode = 'explicit'
          AND (SELECT COUNT(*) FROM performance_personnel pp WHERE pp.performance_id = p.id) = 0
          AND (SELECT COUNT(*) FROM membership m WHERE m.performer_id = p.performer_id) > 0
        ORDER BY p.id
    """)
    rows = cur.fetchall()

    if not rows:
        print("No affected performances found — nothing to repair.")
        con.close()
        return

    print(f"Found {len(rows)} performance(s) wiped by the ingest bug:")
    for r in rows:
        date = "-".join(str(x) for x in (r["start_year"], r["start_month"], r["start_day"]) if x) or "unknown date"
        print(f"  #{r['id']}  {r['performer_name']}  ({date})")

    cur.execute("""
        UPDATE performance SET personnel_mode = 'inherit'
        WHERE id IN ({})
    """.format(",".join(str(r["id"]) for r in rows)))
    con.commit()
    con.close()
    print(f"Reverted {len(rows)} performance(s) to personnel_mode='inherit'.")


if __name__ == "__main__":
    main()
