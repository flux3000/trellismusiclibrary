"""scripts/migrate_add_remote_node.py

Adds the `remote_node` table — outbound peer sharing. Libraries I have joined
as a peer, the mirror of the inbound `peer` table.

Additive and idempotent: one new table, no FKs, no changes to any existing
table. Nothing references it and it references nothing, so this does NOT touch
the stale-FK-DDL problem documented in tools/repair_stale_fk_ddl.py.

Note there is deliberately NO token column — the access token for each remote
lives in the OS keychain (see app/utils/prefs.py: set_remote_token). The
database records that a library was joined, never the credential to reach it.

    python3 scripts/migrate_add_remote_node.py
    FLUX_DB=db/node_b.db python3 scripts/migrate_add_remote_node.py   # node B
"""

import os
import sqlite3

DB = os.environ.get(
    "FLUX_DB",
    os.path.join(os.path.dirname(__file__), "..", "db", "fluxaudio.db"),
)

DDL = """
CREATE TABLE remote_node (
    id                INTEGER PRIMARY KEY,
    display_name      VARCHAR(255) NOT NULL,
    base_url          VARCHAR(512) NOT NULL,
    owner_name        VARCHAR(255),
    peer_name         VARCHAR(255),
    enrolled_at       DATETIME,
    last_connected_at DATETIME,
    left_at           DATETIME
)
"""


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "remote_node" in tables:
        print("remote_node already exists — nothing to do.")
    else:
        cur.execute(DDL)
        con.commit()
        print(f"Created remote_node in {os.path.abspath(DB)}")

    cols = [r[1] for r in cur.execute("PRAGMA table_info(remote_node)")]
    print("columns:", ", ".join(cols))
    count = cur.execute("SELECT COUNT(*) FROM remote_node").fetchone()[0]
    print(f"rows: {count}")
    con.close()


if __name__ == "__main__":
    main()
