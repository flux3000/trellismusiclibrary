"""audit_flatten.py — which recordings still have audio in subdirectories?

Resumable: writes after each batch so it can run under a short timeout.

Usage: python3 audit_flatten.py <db_path> <library_root> <out.json> [batch]
"""
import os
import sys
import json
import glob
import sqlite3

POLICY_DATE = "2026-07-14"

db_path, lib, out = sys.argv[1], sys.argv[2], sys.argv[3]
batch = int(sys.argv[4]) if len(sys.argv) > 4 else 60

res = json.load(open(out)) if os.path.exists(out) else {}
con = sqlite3.connect(db_path)
con.row_factory = sqlite3.Row
rows = list(con.execute(
    "SELECT id, folder_path, created_at FROM recording ORDER BY created_at"))

done = 0
for r in rows:
    key = str(r["id"])
    if key in res or done >= batch:
        continue
    d = os.path.join(lib, r["folder_path"] or "")
    if not os.path.isdir(d):
        res[key] = {"state": "missing", "folder": r["folder_path"],
                    "created": r["created_at"]}
    else:
        root = len([f for f in os.listdir(d) if f.lower().endswith(".flac")])
        deep = len(glob.glob(os.path.join(d, "*", "*.flac")))
        res[key] = {
            "state": "nested" if (deep and not root) else
                     "mixed" if (deep and root) else "flat",
            "root_flac": root, "sub_flac": deep,
            "folder": r["folder_path"], "created": r["created_at"],
            "pre_policy": (r["created_at"] or "") < POLICY_DATE,
        }
    done += 1
    if done % 20 == 0:
        json.dump(res, open(out, "w"))

json.dump(res, open(out, "w"))
print(f"checked {len(res)}/{len(rows)}  COMPLETE={len(res) >= len(rows)}")
