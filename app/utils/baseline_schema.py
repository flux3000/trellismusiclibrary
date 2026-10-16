"""app/utils/baseline_schema.py -- the schema of Trellis as of the Alembic baseline (2026-10-14).

FROZEN. Do not edit, regenerate or "keep in sync" with the models. This is the data behind
revision 0001_baseline (app/migrations/versions/0001_baseline.py): for every table the app
had on 2026-10-14, the CREATE TABLE statement, the ADD COLUMN fragment of each column, and
its indexes. The baseline uses it to bring any v0.2.0-or-later database up to that schema
by inspection (create what is missing, add what is missing, never touch what is there).
A later model change is a NEW Alembic revision; the baseline must keep meaning what it
meant on the day it shipped, which is why it does not read the live models.

Each entry:  table -> (create_sql, [(column, add_column_sql, needs_default)], [(index, sql)])
needs_default is True for a NOT NULL column with no server default: SQLite cannot add such
a column to a table that has rows, so BASELINE_DEFAULTS names the ones the old migration
scripts added with a default; any other missing column of that kind means the database
predates what Trellis supports.
"""

# (table, column) -> SQL literal the old scripts used when they added the column.
BASELINE_DEFAULTS = {
    ("recording", "is_official"): "0",
    ("track", "is_official"): "0",
    ("recording", "is_complete"): "1",
    ("artist", "default_personnel_mode"): "'inherit'",
}

# GENERATED once from the models on 2026-10-14. Do not edit by hand.
SCHEMA = {
    'bulk_ingest_run': (
        "CREATE TABLE bulk_ingest_run ( id INTEGER NOT NULL, root VARCHAR(1024) NOT NULL, status VARCHAR(16) DEFAULT 'running' NOT NULL, started_at DATETIME NOT NULL, finished_at DATETIME, last_error TEXT, mode VARCHAR(8) DEFAULT 'auto' NOT NULL, applied_json TEXT, cleared_at DATETIME, PRIMARY KEY (id) )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('root', 'root VARCHAR(1024) NOT NULL', True),
            ('status', "status VARCHAR(16) DEFAULT 'running' NOT NULL", False),
            ('started_at', 'started_at DATETIME NOT NULL', True),
            ('finished_at', 'finished_at DATETIME', False),
            ('last_error', 'last_error TEXT', False),
            ('mode', "mode VARCHAR(8) DEFAULT 'auto' NOT NULL", False),
            ('applied_json', 'applied_json TEXT', False),
            ('cleared_at', 'cleared_at DATETIME', False),
        ],
        [
            ('ix_bulk_ingest_run_status', 'CREATE INDEX IF NOT EXISTS ix_bulk_ingest_run_status ON bulk_ingest_run (status)'),
        ]),
    'collection': (
        'CREATE TABLE collection ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, description TEXT, created_at DATETIME, updated_at DATETIME, system_key VARCHAR(50), PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('description', 'description TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
            ('system_key', 'system_key VARCHAR(50)', False),
        ],
        [
            ('ix_collection_system_key', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_collection_system_key ON collection (system_key)'),
        ]),
    'download_job': (
        "CREATE TABLE download_job ( id INTEGER NOT NULL, source VARCHAR(16) NOT NULL, source_id VARCHAR(255) NOT NULL, artist VARCHAR(255), date VARCHAR(32), venue VARCHAR(255), status VARCHAR(16) DEFAULT 'queued' NOT NULL, position INTEGER DEFAULT '0' NOT NULL, total_bytes BIGINT DEFAULT '0' NOT NULL, done_bytes BIGINT DEFAULT '0' NOT NULL, dest_path VARCHAR(1024) NOT NULL, error TEXT, checksums TEXT, created_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME, PRIMARY KEY (id) )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('source', 'source VARCHAR(16) NOT NULL', True),
            ('source_id', 'source_id VARCHAR(255) NOT NULL', True),
            ('artist', 'artist VARCHAR(255)', False),
            ('date', 'date VARCHAR(32)', False),
            ('venue', 'venue VARCHAR(255)', False),
            ('status', "status VARCHAR(16) DEFAULT 'queued' NOT NULL", False),
            ('position', "position INTEGER DEFAULT '0' NOT NULL", False),
            ('total_bytes', "total_bytes BIGINT DEFAULT '0' NOT NULL", False),
            ('done_bytes', "done_bytes BIGINT DEFAULT '0' NOT NULL", False),
            ('dest_path', 'dest_path VARCHAR(1024) NOT NULL', True),
            ('error', 'error TEXT', False),
            ('checksums', 'checksums TEXT', False),
            ('created_at', 'created_at DATETIME NOT NULL', True),
            ('started_at', 'started_at DATETIME', False),
            ('finished_at', 'finished_at DATETIME', False),
        ],
        [
            ('ix_download_job_status', 'CREATE INDEX IF NOT EXISTS ix_download_job_status ON download_job (status)'),
        ]),
    'engineer': (
        'CREATE TABLE engineer ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, sort_name VARCHAR(255), bio TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('sort_name', 'sort_name VARCHAR(255)', False),
            ('bio', 'bio TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'genre': (
        'CREATE TABLE genre ( id INTEGER NOT NULL, name VARCHAR(80) NOT NULL, description TEXT, color VARCHAR(7), created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), UNIQUE (name) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(80) NOT NULL', True),
            ('description', 'description TEXT', False),
            ('color', 'color VARCHAR(7)', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'lomax_run': (
        'CREATE TABLE lomax_run ( id INTEGER NOT NULL, skill VARCHAR(24) NOT NULL, subject_type VARCHAR(16) NOT NULL, subject_id INTEGER, subject_key VARCHAR(1024), level VARCHAR(12) NOT NULL, question TEXT, input_json TEXT, status VARCHAR(12) NOT NULL, result_json TEXT, usage_json TEXT, model VARCHAR(64), error TEXT, created_by INTEGER, created_at DATETIME, finished_at DATETIME, migrated_from VARCHAR(24), PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('skill', 'skill VARCHAR(24) NOT NULL', True),
            ('subject_type', 'subject_type VARCHAR(16) NOT NULL', True),
            ('subject_id', 'subject_id INTEGER', False),
            ('subject_key', 'subject_key VARCHAR(1024)', False),
            ('level', 'level VARCHAR(12) NOT NULL', True),
            ('question', 'question TEXT', False),
            ('input_json', 'input_json TEXT', False),
            ('status', 'status VARCHAR(12) NOT NULL', True),
            ('result_json', 'result_json TEXT', False),
            ('usage_json', 'usage_json TEXT', False),
            ('model', 'model VARCHAR(64)', False),
            ('error', 'error TEXT', False),
            ('created_by', 'created_by INTEGER', False),
            ('created_at', 'created_at DATETIME', False),
            ('finished_at', 'finished_at DATETIME', False),
            ('migrated_from', 'migrated_from VARCHAR(24)', False),
        ],
        [
            ('ix_lomax_run_key', 'CREATE INDEX IF NOT EXISTS ix_lomax_run_key ON lomax_run (subject_key)'),
            ('ix_lomax_run_skill', 'CREATE INDEX IF NOT EXISTS ix_lomax_run_skill ON lomax_run (skill)'),
            ('ix_lomax_run_subject', 'CREATE INDEX IF NOT EXISTS ix_lomax_run_subject ON lomax_run (subject_type, subject_id)'),
        ]),
    'musician': (
        'CREATE TABLE musician ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, sort_name VARCHAR(255), bio TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('sort_name', 'sort_name VARCHAR(255)', False),
            ('bio', 'bio TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'node_setting': (
        'CREATE TABLE node_setting ( "key" VARCHAR(64) NOT NULL, value VARCHAR(500) NOT NULL, PRIMARY KEY ("key") )',
        [
            ('key', '"key" VARCHAR(64) NOT NULL', False),
            ('value', 'value VARCHAR(500) NOT NULL', True),
        ],
        [
        ]),
    'peer': (
        'CREATE TABLE peer ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, contact_note TEXT, created_at DATETIME, last_seen_at DATETIME, revoked_at DATETIME, PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('contact_note', 'contact_note TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('last_seen_at', 'last_seen_at DATETIME', False),
            ('revoked_at', 'revoked_at DATETIME', False),
        ],
        [
        ]),
    'remote_node': (
        'CREATE TABLE remote_node ( id INTEGER NOT NULL, display_name VARCHAR(255) NOT NULL, base_url VARCHAR(512) NOT NULL, owner_name VARCHAR(255), peer_name VARCHAR(255), enrolled_at DATETIME, last_connected_at DATETIME, left_at DATETIME, PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('display_name', 'display_name VARCHAR(255) NOT NULL', True),
            ('base_url', 'base_url VARCHAR(512) NOT NULL', True),
            ('owner_name', 'owner_name VARCHAR(255)', False),
            ('peer_name', 'peer_name VARCHAR(255)', False),
            ('enrolled_at', 'enrolled_at DATETIME', False),
            ('last_connected_at', 'last_connected_at DATETIME', False),
            ('left_at', 'left_at DATETIME', False),
        ],
        [
        ]),
    'user': (
        'CREATE TABLE user ( id INTEGER NOT NULL, username VARCHAR(64) NOT NULL, email VARCHAR(255), display_name VARCHAR(120), avatar_ext VARCHAR(8), password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL, all_artists BOOLEAN NOT NULL, is_active BOOLEAN NOT NULL, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), UNIQUE (username), UNIQUE (email) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('username', 'username VARCHAR(64) NOT NULL', True),
            ('email', 'email VARCHAR(255)', False),
            ('display_name', 'display_name VARCHAR(120)', False),
            ('avatar_ext', 'avatar_ext VARCHAR(8)', False),
            ('password_hash', 'password_hash VARCHAR(255) NOT NULL', True),
            ('role', 'role VARCHAR(16) NOT NULL', True),
            ('all_artists', 'all_artists BOOLEAN NOT NULL', True),
            ('is_active', 'is_active BOOLEAN NOT NULL', True),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'venue': (
        'CREATE TABLE venue ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, city VARCHAR(128), state VARCHAR(64), country VARCHAR(64), bio TEXT, history TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('city', 'city VARCHAR(128)', False),
            ('state', 'state VARCHAR(64)', False),
            ('country', 'country VARCHAR(64)', False),
            ('bio', 'bio TEXT', False),
            ('history', 'history TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'artist': (
        'CREATE TABLE artist ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, sort_name VARCHAR(255), bio TEXT, abbreviation VARCHAR(16), default_personnel_mode VARCHAR(16) NOT NULL, image_ext VARCHAR(8), dossier_json TEXT, lineup_json TEXT, genre_id INTEGER, mbid VARCHAR(36), mb_status VARCHAR(16), mb_type VARCHAR(32), mb_area VARCHAR(120), mb_begin VARCHAR(10), mb_end VARCHAR(10), mb_disambiguation VARCHAR(255), mb_links_json TEXT, mb_extra_json TEXT, mb_checked_at DATETIME, confirmed_at DATETIME, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(genre_id) REFERENCES genre (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('sort_name', 'sort_name VARCHAR(255)', False),
            ('bio', 'bio TEXT', False),
            ('abbreviation', 'abbreviation VARCHAR(16)', False),
            ('default_personnel_mode', 'default_personnel_mode VARCHAR(16) NOT NULL', True),
            ('image_ext', 'image_ext VARCHAR(8)', False),
            ('dossier_json', 'dossier_json TEXT', False),
            ('lineup_json', 'lineup_json TEXT', False),
            ('genre_id', 'genre_id INTEGER REFERENCES genre(id)', False),
            ('mbid', 'mbid VARCHAR(36)', False),
            ('mb_status', 'mb_status VARCHAR(16)', False),
            ('mb_type', 'mb_type VARCHAR(32)', False),
            ('mb_area', 'mb_area VARCHAR(120)', False),
            ('mb_begin', 'mb_begin VARCHAR(10)', False),
            ('mb_end', 'mb_end VARCHAR(10)', False),
            ('mb_disambiguation', 'mb_disambiguation VARCHAR(255)', False),
            ('mb_links_json', 'mb_links_json TEXT', False),
            ('mb_extra_json', 'mb_extra_json TEXT', False),
            ('mb_checked_at', 'mb_checked_at DATETIME', False),
            ('confirmed_at', 'confirmed_at DATETIME', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
            ('ix_artist_mbid', 'CREATE INDEX IF NOT EXISTS ix_artist_mbid ON artist (mbid)'),
        ]),
    'collection_grant': (
        'CREATE TABLE collection_grant ( id INTEGER NOT NULL, peer_id INTEGER NOT NULL, collection_id INTEGER NOT NULL, created_at DATETIME, revoked_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(peer_id) REFERENCES peer (id), FOREIGN KEY(collection_id) REFERENCES collection (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('peer_id', 'peer_id INTEGER NOT NULL REFERENCES peer(id)', True),
            ('collection_id', 'collection_id INTEGER NOT NULL REFERENCES collection(id)', True),
            ('created_at', 'created_at DATETIME', False),
            ('revoked_at', 'revoked_at DATETIME', False),
        ],
        [
            ('ix_collection_grant_collection_id', 'CREATE INDEX IF NOT EXISTS ix_collection_grant_collection_id ON collection_grant (collection_id)'),
            ('ix_collection_grant_peer_id', 'CREATE INDEX IF NOT EXISTS ix_collection_grant_peer_id ON collection_grant (peer_id)'),
        ]),
    'engineer_alias': (
        'CREATE TABLE engineer_alias ( id INTEGER NOT NULL, engineer_id INTEGER NOT NULL, alias VARCHAR(255) NOT NULL, alias_key VARCHAR(255) NOT NULL, created_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_engineer_alias_key UNIQUE (engineer_id, alias_key), FOREIGN KEY(engineer_id) REFERENCES engineer (id) ON DELETE CASCADE )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('engineer_id', 'engineer_id INTEGER NOT NULL REFERENCES engineer(id) ON DELETE CASCADE', True),
            ('alias', 'alias VARCHAR(255) NOT NULL', True),
            ('alias_key', 'alias_key VARCHAR(255) NOT NULL', True),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_engineer_alias_alias_key', 'CREATE INDEX IF NOT EXISTS ix_engineer_alias_alias_key ON engineer_alias (alias_key)'),
            ('ix_engineer_alias_engineer_id', 'CREATE INDEX IF NOT EXISTS ix_engineer_alias_engineer_id ON engineer_alias (engineer_id)'),
        ]),
    'engineer_image': (
        "CREATE TABLE engineer_image ( id INTEGER NOT NULL, engineer_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, ext VARCHAR(8) NOT NULL, is_primary BOOLEAN DEFAULT '0' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, origin VARCHAR(24) DEFAULT 'upload' NOT NULL, caption VARCHAR(255), credit VARCHAR(255), source_ref VARCHAR(255), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(engineer_id) REFERENCES engineer (id) ON DELETE CASCADE )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('engineer_id', 'engineer_id INTEGER NOT NULL REFERENCES engineer(id) ON DELETE CASCADE', True),
            ('filename', 'filename VARCHAR(255) NOT NULL', True),
            ('ext', 'ext VARCHAR(8) NOT NULL', True),
            ('is_primary', "is_primary BOOLEAN DEFAULT '0' NOT NULL", False),
            ('sort_order', "sort_order INTEGER DEFAULT '0' NOT NULL", False),
            ('origin', "origin VARCHAR(24) DEFAULT 'upload' NOT NULL", False),
            ('caption', 'caption VARCHAR(255)', False),
            ('credit', 'credit VARCHAR(255)', False),
            ('source_ref', 'source_ref VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_engineer_image_engineer_id', 'CREATE INDEX IF NOT EXISTS ix_engineer_image_engineer_id ON engineer_image (engineer_id)'),
        ]),
    'event': (
        'CREATE TABLE event ( id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, venue_id INTEGER, city VARCHAR(128), state VARCHAR(64), country VARCHAR(64), start_year INTEGER, start_month INTEGER, start_day INTEGER, end_year INTEGER, end_month INTEGER, end_day INTEGER, notes TEXT, history TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(venue_id) REFERENCES venue (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('name', 'name VARCHAR(255) NOT NULL', True),
            ('venue_id', 'venue_id INTEGER REFERENCES venue(id)', False),
            ('city', 'city VARCHAR(128)', False),
            ('state', 'state VARCHAR(64)', False),
            ('country', 'country VARCHAR(64)', False),
            ('start_year', 'start_year INTEGER', False),
            ('start_month', 'start_month INTEGER', False),
            ('start_day', 'start_day INTEGER', False),
            ('end_year', 'end_year INTEGER', False),
            ('end_month', 'end_month INTEGER', False),
            ('end_day', 'end_day INTEGER', False),
            ('notes', 'notes TEXT', False),
            ('history', 'history TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'lomax_easter_egg_seen': (
        'CREATE TABLE lomax_easter_egg_seen ( id INTEGER NOT NULL, user_id INTEGER NOT NULL, egg_key VARCHAR(32) NOT NULL, seen_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_lomax_egg_user_key UNIQUE (user_id, egg_key), FOREIGN KEY(user_id) REFERENCES user (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('user_id', 'user_id INTEGER NOT NULL REFERENCES user(id)', True),
            ('egg_key', 'egg_key VARCHAR(32) NOT NULL', True),
            ('seen_at', 'seen_at DATETIME', False),
        ],
        [
            ('ix_lomax_easter_egg_seen_user_id', 'CREATE INDEX IF NOT EXISTS ix_lomax_easter_egg_seen_user_id ON lomax_easter_egg_seen (user_id)'),
        ]),
    'lomax_proposal': (
        "CREATE TABLE lomax_proposal ( id INTEGER NOT NULL, run_id INTEGER NOT NULL, field VARCHAR(32) NOT NULL, current TEXT, proposed TEXT NOT NULL, confidence VARCHAR(8), source VARCHAR(24), url VARCHAR(1024), decision VARCHAR(10), challenge BOOLEAN DEFAULT '0' NOT NULL, agrees BOOLEAN DEFAULT '0' NOT NULL, decided_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(run_id) REFERENCES lomax_run (id) )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('run_id', 'run_id INTEGER NOT NULL REFERENCES lomax_run(id)', True),
            ('field', 'field VARCHAR(32) NOT NULL', True),
            ('current', 'current TEXT', False),
            ('proposed', 'proposed TEXT NOT NULL', True),
            ('confidence', 'confidence VARCHAR(8)', False),
            ('source', 'source VARCHAR(24)', False),
            ('url', 'url VARCHAR(1024)', False),
            ('decision', 'decision VARCHAR(10)', False),
            ('challenge', "challenge BOOLEAN DEFAULT '0' NOT NULL", False),
            ('agrees', "agrees BOOLEAN DEFAULT '0' NOT NULL", False),
            ('decided_at', 'decided_at DATETIME', False),
        ],
        [
            ('ix_lomax_proposal_run_id', 'CREATE INDEX IF NOT EXISTS ix_lomax_proposal_run_id ON lomax_proposal (run_id)'),
        ]),
    'musician_image': (
        "CREATE TABLE musician_image ( id INTEGER NOT NULL, musician_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, ext VARCHAR(8) NOT NULL, is_primary BOOLEAN DEFAULT '0' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, origin VARCHAR(24) DEFAULT 'upload' NOT NULL, caption VARCHAR(255), credit VARCHAR(255), source_ref VARCHAR(255), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(musician_id) REFERENCES musician (id) ON DELETE CASCADE )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('musician_id', 'musician_id INTEGER NOT NULL REFERENCES musician(id) ON DELETE CASCADE', True),
            ('filename', 'filename VARCHAR(255) NOT NULL', True),
            ('ext', 'ext VARCHAR(8) NOT NULL', True),
            ('is_primary', "is_primary BOOLEAN DEFAULT '0' NOT NULL", False),
            ('sort_order', "sort_order INTEGER DEFAULT '0' NOT NULL", False),
            ('origin', "origin VARCHAR(24) DEFAULT 'upload' NOT NULL", False),
            ('caption', 'caption VARCHAR(255)', False),
            ('credit', 'credit VARCHAR(255)', False),
            ('source_ref', 'source_ref VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_musician_image_musician_id', 'CREATE INDEX IF NOT EXISTS ix_musician_image_musician_id ON musician_image (musician_id)'),
        ]),
    'peer_invite': (
        'CREATE TABLE peer_invite ( id INTEGER NOT NULL, peer_id INTEGER NOT NULL, code_hash VARCHAR(64) NOT NULL, code VARCHAR(128), created_at DATETIME, expires_at DATETIME NOT NULL, consumed_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(peer_id) REFERENCES peer (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('peer_id', 'peer_id INTEGER NOT NULL REFERENCES peer(id)', True),
            ('code_hash', 'code_hash VARCHAR(64) NOT NULL', True),
            ('code', 'code VARCHAR(128)', False),
            ('created_at', 'created_at DATETIME', False),
            ('expires_at', 'expires_at DATETIME NOT NULL', True),
            ('consumed_at', 'consumed_at DATETIME', False),
        ],
        [
            ('ix_peer_invite_code_hash', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_peer_invite_code_hash ON peer_invite (code_hash)'),
            ('ix_peer_invite_peer_id', 'CREATE INDEX IF NOT EXISTS ix_peer_invite_peer_id ON peer_invite (peer_id)'),
        ]),
    'peer_token': (
        'CREATE TABLE peer_token ( id INTEGER NOT NULL, peer_id INTEGER NOT NULL, token_hash VARCHAR(64) NOT NULL, device_label VARCHAR(255), created_at DATETIME, last_used_at DATETIME, revoked_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(peer_id) REFERENCES peer (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('peer_id', 'peer_id INTEGER NOT NULL REFERENCES peer(id)', True),
            ('token_hash', 'token_hash VARCHAR(64) NOT NULL', True),
            ('device_label', 'device_label VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
            ('last_used_at', 'last_used_at DATETIME', False),
            ('revoked_at', 'revoked_at DATETIME', False),
        ],
        [
            ('ix_peer_token_peer_id', 'CREATE INDEX IF NOT EXISTS ix_peer_token_peer_id ON peer_token (peer_id)'),
            ('ix_peer_token_token_hash', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_peer_token_token_hash ON peer_token (token_hash)'),
        ]),
    'remote_favorite': (
        'CREATE TABLE remote_favorite ( id INTEGER NOT NULL, remote_node_id INTEGER NOT NULL, remote_recording_id INTEGER NOT NULL, created_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_remote_favorite_node_recording UNIQUE (remote_node_id, remote_recording_id), FOREIGN KEY(remote_node_id) REFERENCES remote_node (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('remote_node_id', 'remote_node_id INTEGER NOT NULL REFERENCES remote_node(id)', True),
            ('remote_recording_id', 'remote_recording_id INTEGER NOT NULL', True),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_remote_favorite_remote_node_id', 'CREATE INDEX IF NOT EXISTS ix_remote_favorite_remote_node_id ON remote_favorite (remote_node_id)'),
            ('ix_remote_favorite_remote_recording_id', 'CREATE INDEX IF NOT EXISTS ix_remote_favorite_remote_recording_id ON remote_favorite (remote_recording_id)'),
        ]),
    'user_preference': (
        'CREATE TABLE user_preference ( id INTEGER NOT NULL, user_id INTEGER NOT NULL, "key" VARCHAR(64) NOT NULL, value VARCHAR(255) NOT NULL, PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES user (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('user_id', 'user_id INTEGER NOT NULL REFERENCES user(id)', True),
            ('key', '"key" VARCHAR(64) NOT NULL', True),
            ('value', 'value VARCHAR(255) NOT NULL', True),
        ],
        [
        ]),
    'venue_alias': (
        'CREATE TABLE venue_alias ( id INTEGER NOT NULL, venue_id INTEGER NOT NULL, alias VARCHAR(255) NOT NULL, alias_key VARCHAR(255) NOT NULL, created_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_venue_alias_key UNIQUE (venue_id, alias_key), FOREIGN KEY(venue_id) REFERENCES venue (id) ON DELETE CASCADE )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('venue_id', 'venue_id INTEGER NOT NULL REFERENCES venue(id) ON DELETE CASCADE', True),
            ('alias', 'alias VARCHAR(255) NOT NULL', True),
            ('alias_key', 'alias_key VARCHAR(255) NOT NULL', True),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_venue_alias_alias_key', 'CREATE INDEX IF NOT EXISTS ix_venue_alias_alias_key ON venue_alias (alias_key)'),
            ('ix_venue_alias_venue_id', 'CREATE INDEX IF NOT EXISTS ix_venue_alias_venue_id ON venue_alias (venue_id)'),
        ]),
    'venue_image': (
        "CREATE TABLE venue_image ( id INTEGER NOT NULL, venue_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, ext VARCHAR(8) NOT NULL, is_primary BOOLEAN DEFAULT '0' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, origin VARCHAR(24) DEFAULT 'upload' NOT NULL, caption VARCHAR(255), credit VARCHAR(255), source_ref VARCHAR(255), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(venue_id) REFERENCES venue (id) ON DELETE CASCADE )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('venue_id', 'venue_id INTEGER NOT NULL REFERENCES venue(id) ON DELETE CASCADE', True),
            ('filename', 'filename VARCHAR(255) NOT NULL', True),
            ('ext', 'ext VARCHAR(8) NOT NULL', True),
            ('is_primary', "is_primary BOOLEAN DEFAULT '0' NOT NULL", False),
            ('sort_order', "sort_order INTEGER DEFAULT '0' NOT NULL", False),
            ('origin', "origin VARCHAR(24) DEFAULT 'upload' NOT NULL", False),
            ('caption', 'caption VARCHAR(255)', False),
            ('credit', 'credit VARCHAR(255)', False),
            ('source_ref', 'source_ref VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_venue_image_venue_id', 'CREATE INDEX IF NOT EXISTS ix_venue_image_venue_id ON venue_image (venue_id)'),
        ]),
    'artist_alias': (
        'CREATE TABLE artist_alias ( id INTEGER NOT NULL, artist_id INTEGER NOT NULL, alias VARCHAR(255) NOT NULL, alias_key VARCHAR(255) NOT NULL, created_at DATETIME, PRIMARY KEY (id), CONSTRAINT uq_artist_alias_key UNIQUE (artist_id, alias_key), FOREIGN KEY(artist_id) REFERENCES artist (id) ON DELETE CASCADE )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('artist_id', 'artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE', True),
            ('alias', 'alias VARCHAR(255) NOT NULL', True),
            ('alias_key', 'alias_key VARCHAR(255) NOT NULL', True),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_artist_alias_alias_key', 'CREATE INDEX IF NOT EXISTS ix_artist_alias_alias_key ON artist_alias (alias_key)'),
            ('ix_artist_alias_artist_id', 'CREATE INDEX IF NOT EXISTS ix_artist_alias_artist_id ON artist_alias (artist_id)'),
        ]),
    'artist_image': (
        "CREATE TABLE artist_image ( id INTEGER NOT NULL, artist_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, ext VARCHAR(8) NOT NULL, is_primary BOOLEAN DEFAULT '0' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, origin VARCHAR(24) DEFAULT 'upload' NOT NULL, caption VARCHAR(255), credit VARCHAR(255), source_ref VARCHAR(255), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(artist_id) REFERENCES artist (id) ON DELETE CASCADE )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('artist_id', 'artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE', True),
            ('filename', 'filename VARCHAR(255) NOT NULL', True),
            ('ext', 'ext VARCHAR(8) NOT NULL', True),
            ('is_primary', "is_primary BOOLEAN DEFAULT '0' NOT NULL", False),
            ('sort_order', "sort_order INTEGER DEFAULT '0' NOT NULL", False),
            ('origin', "origin VARCHAR(24) DEFAULT 'upload' NOT NULL", False),
            ('caption', 'caption VARCHAR(255)', False),
            ('credit', 'credit VARCHAR(255)', False),
            ('source_ref', 'source_ref VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_artist_image_artist_id', 'CREATE INDEX IF NOT EXISTS ix_artist_image_artist_id ON artist_image (artist_id)'),
        ]),
    'artist_resource': (
        'CREATE TABLE artist_resource ( id INTEGER NOT NULL, artist_id INTEGER NOT NULL, label VARCHAR(255), url VARCHAR(1024) NOT NULL, "order" INTEGER NOT NULL, created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(artist_id) REFERENCES artist (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('artist_id', 'artist_id INTEGER NOT NULL REFERENCES artist(id)', True),
            ('label', 'label VARCHAR(255)', False),
            ('url', 'url VARCHAR(1024) NOT NULL', True),
            ('order', '"order" INTEGER NOT NULL', True),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
        ]),
    'event_image': (
        "CREATE TABLE event_image ( id INTEGER NOT NULL, event_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, ext VARCHAR(8) NOT NULL, is_primary BOOLEAN DEFAULT '0' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, origin VARCHAR(24) DEFAULT 'upload' NOT NULL, caption VARCHAR(255), credit VARCHAR(255), source_ref VARCHAR(255), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(event_id) REFERENCES event (id) ON DELETE CASCADE )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('event_id', 'event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE', True),
            ('filename', 'filename VARCHAR(255) NOT NULL', True),
            ('ext', 'ext VARCHAR(8) NOT NULL', True),
            ('is_primary', "is_primary BOOLEAN DEFAULT '0' NOT NULL", False),
            ('sort_order', "sort_order INTEGER DEFAULT '0' NOT NULL", False),
            ('origin', "origin VARCHAR(24) DEFAULT 'upload' NOT NULL", False),
            ('caption', 'caption VARCHAR(255)', False),
            ('credit', 'credit VARCHAR(255)', False),
            ('source_ref', 'source_ref VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_event_image_event_id', 'CREATE INDEX IF NOT EXISTS ix_event_image_event_id ON event_image (event_id)'),
        ]),
    'membership': (
        'CREATE TABLE membership ( id INTEGER NOT NULL, artist_id INTEGER NOT NULL, musician_id INTEGER NOT NULL, "order" INTEGER NOT NULL, instrument VARCHAR(128), start_year INTEGER, start_month INTEGER, start_day INTEGER, end_year INTEGER, end_month INTEGER, end_day INTEGER, PRIMARY KEY (id), FOREIGN KEY(artist_id) REFERENCES artist (id), FOREIGN KEY(musician_id) REFERENCES musician (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('artist_id', 'artist_id INTEGER NOT NULL REFERENCES artist(id)', True),
            ('musician_id', 'musician_id INTEGER NOT NULL REFERENCES musician(id)', True),
            ('order', '"order" INTEGER NOT NULL', True),
            ('instrument', 'instrument VARCHAR(128)', False),
            ('start_year', 'start_year INTEGER', False),
            ('start_month', 'start_month INTEGER', False),
            ('start_day', 'start_day INTEGER', False),
            ('end_year', 'end_year INTEGER', False),
            ('end_month', 'end_month INTEGER', False),
            ('end_day', 'end_day INTEGER', False),
        ],
        [
        ]),
    'performance': (
        "CREATE TABLE performance ( id INTEGER NOT NULL, artist_id INTEGER NOT NULL, venue_id INTEGER, event_id INTEGER, title VARCHAR(255), stage VARCHAR(128), personnel_mode VARCHAR(16) DEFAULT 'inherit' NOT NULL, start_year INTEGER, start_month INTEGER, start_day INTEGER, end_year INTEGER, end_month INTEGER, end_day INTEGER, city VARCHAR(128), state VARCHAR(64), country VARCHAR(64), notes TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(artist_id) REFERENCES artist (id), FOREIGN KEY(venue_id) REFERENCES venue (id), FOREIGN KEY(event_id) REFERENCES event (id) )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('artist_id', 'artist_id INTEGER NOT NULL REFERENCES artist(id)', True),
            ('venue_id', 'venue_id INTEGER REFERENCES venue(id)', False),
            ('event_id', 'event_id INTEGER REFERENCES event(id)', False),
            ('title', 'title VARCHAR(255)', False),
            ('stage', 'stage VARCHAR(128)', False),
            ('personnel_mode', "personnel_mode VARCHAR(16) DEFAULT 'inherit' NOT NULL", False),
            ('start_year', 'start_year INTEGER', False),
            ('start_month', 'start_month INTEGER', False),
            ('start_day', 'start_day INTEGER', False),
            ('end_year', 'end_year INTEGER', False),
            ('end_month', 'end_month INTEGER', False),
            ('end_day', 'end_day INTEGER', False),
            ('city', 'city VARCHAR(128)', False),
            ('state', 'state VARCHAR(64)', False),
            ('country', 'country VARCHAR(64)', False),
            ('notes', 'notes TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'user_artist_permission': (
        'CREATE TABLE user_artist_permission ( id INTEGER NOT NULL, user_id INTEGER NOT NULL, artist_id INTEGER NOT NULL, PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES user (id), FOREIGN KEY(artist_id) REFERENCES artist (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('user_id', 'user_id INTEGER NOT NULL REFERENCES user(id)', True),
            ('artist_id', 'artist_id INTEGER NOT NULL REFERENCES artist(id)', True),
        ],
        [
        ]),
    'performance_personnel': (
        'CREATE TABLE performance_personnel ( id INTEGER NOT NULL, performance_id INTEGER NOT NULL, musician_id INTEGER NOT NULL, instrument VARCHAR(128), "order" INTEGER NOT NULL, is_guest BOOLEAN NOT NULL, note VARCHAR(255), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(performance_id) REFERENCES performance (id), FOREIGN KEY(musician_id) REFERENCES musician (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('performance_id', 'performance_id INTEGER NOT NULL REFERENCES performance(id)', True),
            ('musician_id', 'musician_id INTEGER NOT NULL REFERENCES musician(id)', True),
            ('instrument', 'instrument VARCHAR(128)', False),
            ('order', '"order" INTEGER NOT NULL', True),
            ('is_guest', 'is_guest BOOLEAN NOT NULL', True),
            ('note', 'note VARCHAR(255)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
        ]),
    'recording': (
        "CREATE TABLE recording ( id INTEGER NOT NULL, performance_id INTEGER NOT NULL, title VARCHAR(255), source VARCHAR(64), lineage TEXT, quality VARCHAR(4), is_complete BOOLEAN NOT NULL, is_official BOOLEAN NOT NULL, folder_path VARCHAR(512) NOT NULL, is_published BOOLEAN DEFAULT '1' NOT NULL, location VARCHAR(16) DEFAULT 'library' NOT NULL, original_folder_name VARCHAR(512), info_file_content TEXT, rating INTEGER, is_favorite BOOLEAN DEFAULT '0' NOT NULL, notes TEXT, source_tag VARCHAR(64), etree_shnid INTEGER, kind VARCHAR(16) DEFAULT 'live' NOT NULL, mb_release_id VARCHAR(36), mb_release_group_id VARCHAR(36), mb_release_status VARCHAR(16), mb_release_type VARCHAR(32), mb_release_db_status VARCHAR(24), mb_label VARCHAR(120), mb_catalog_number VARCHAR(64), mb_release_country VARCHAR(2), mb_release_checked_at DATETIME, images_checked_at DATETIME, completeness_checked_at DATETIME, recording_engineer_id INTEGER, transfer_engineer_id INTEGER, mastering_engineer_id INTEGER, ai_research_json TEXT, resolver_json TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(performance_id) REFERENCES performance (id), FOREIGN KEY(recording_engineer_id) REFERENCES engineer (id) ON DELETE SET NULL, FOREIGN KEY(transfer_engineer_id) REFERENCES engineer (id) ON DELETE SET NULL, FOREIGN KEY(mastering_engineer_id) REFERENCES engineer (id) ON DELETE SET NULL )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('performance_id', 'performance_id INTEGER NOT NULL REFERENCES performance(id)', True),
            ('title', 'title VARCHAR(255)', False),
            ('source', 'source VARCHAR(64)', False),
            ('lineage', 'lineage TEXT', False),
            ('quality', 'quality VARCHAR(4)', False),
            ('is_complete', 'is_complete BOOLEAN NOT NULL', True),
            ('is_official', 'is_official BOOLEAN NOT NULL', True),
            ('folder_path', 'folder_path VARCHAR(512) NOT NULL', True),
            ('is_published', "is_published BOOLEAN DEFAULT '1' NOT NULL", False),
            ('location', "location VARCHAR(16) DEFAULT 'library' NOT NULL", False),
            ('original_folder_name', 'original_folder_name VARCHAR(512)', False),
            ('info_file_content', 'info_file_content TEXT', False),
            ('rating', 'rating INTEGER', False),
            ('is_favorite', "is_favorite BOOLEAN DEFAULT '0' NOT NULL", False),
            ('notes', 'notes TEXT', False),
            ('source_tag', 'source_tag VARCHAR(64)', False),
            ('etree_shnid', 'etree_shnid INTEGER', False),
            ('kind', "kind VARCHAR(16) DEFAULT 'live' NOT NULL", False),
            ('mb_release_id', 'mb_release_id VARCHAR(36)', False),
            ('mb_release_group_id', 'mb_release_group_id VARCHAR(36)', False),
            ('mb_release_status', 'mb_release_status VARCHAR(16)', False),
            ('mb_release_type', 'mb_release_type VARCHAR(32)', False),
            ('mb_release_db_status', 'mb_release_db_status VARCHAR(24)', False),
            ('mb_label', 'mb_label VARCHAR(120)', False),
            ('mb_catalog_number', 'mb_catalog_number VARCHAR(64)', False),
            ('mb_release_country', 'mb_release_country VARCHAR(2)', False),
            ('mb_release_checked_at', 'mb_release_checked_at DATETIME', False),
            ('images_checked_at', 'images_checked_at DATETIME', False),
            ('completeness_checked_at', 'completeness_checked_at DATETIME', False),
            ('recording_engineer_id', 'recording_engineer_id INTEGER REFERENCES engineer(id) ON DELETE SET NULL', False),
            ('transfer_engineer_id', 'transfer_engineer_id INTEGER REFERENCES engineer(id) ON DELETE SET NULL', False),
            ('mastering_engineer_id', 'mastering_engineer_id INTEGER REFERENCES engineer(id) ON DELETE SET NULL', False),
            ('ai_research_json', 'ai_research_json TEXT', False),
            ('resolver_json', 'resolver_json TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
            ('ix_recording_folder_path', 'CREATE INDEX IF NOT EXISTS ix_recording_folder_path ON recording (folder_path)'),
            ('ix_recording_mastering_engineer_id', 'CREATE INDEX IF NOT EXISTS ix_recording_mastering_engineer_id ON recording (mastering_engineer_id)'),
            ('ix_recording_recording_engineer_id', 'CREATE INDEX IF NOT EXISTS ix_recording_recording_engineer_id ON recording (recording_engineer_id)'),
            ('ix_recording_transfer_engineer_id', 'CREATE INDEX IF NOT EXISTS ix_recording_transfer_engineer_id ON recording (transfer_engineer_id)'),
        ]),
    'bulk_ingest_item': (
        "CREATE TABLE bulk_ingest_item ( id INTEGER NOT NULL, run_id INTEGER NOT NULL, rel_path VARCHAR(512) NOT NULL, status VARCHAR(16) DEFAULT 'pending' NOT NULL, reason VARCHAR(32), detail TEXT, kind VARCHAR(16), format VARCHAR(32), meta TEXT, recording_id INTEGER, duplicate_of INTEGER, ingest_requested BOOLEAN DEFAULT '0' NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id), CONSTRAINT uq_bulk_ingest_item_run_rel_path UNIQUE (run_id, rel_path), FOREIGN KEY(run_id) REFERENCES bulk_ingest_run (id) ON DELETE CASCADE, FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE SET NULL, FOREIGN KEY(duplicate_of) REFERENCES recording (id) ON DELETE SET NULL )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('run_id', 'run_id INTEGER NOT NULL REFERENCES bulk_ingest_run(id) ON DELETE CASCADE', True),
            ('rel_path', 'rel_path VARCHAR(512) NOT NULL', True),
            ('status', "status VARCHAR(16) DEFAULT 'pending' NOT NULL", False),
            ('reason', 'reason VARCHAR(32)', False),
            ('detail', 'detail TEXT', False),
            ('kind', 'kind VARCHAR(16)', False),
            ('format', 'format VARCHAR(32)', False),
            ('meta', 'meta TEXT', False),
            ('recording_id', 'recording_id INTEGER REFERENCES recording(id) ON DELETE SET NULL', False),
            ('duplicate_of', 'duplicate_of INTEGER REFERENCES recording(id) ON DELETE SET NULL', False),
            ('ingest_requested', "ingest_requested BOOLEAN DEFAULT '0' NOT NULL", False),
            ('updated_at', 'updated_at DATETIME NOT NULL', True),
        ],
        [
            ('ix_bulk_ingest_item_recording_id', 'CREATE INDEX IF NOT EXISTS ix_bulk_ingest_item_recording_id ON bulk_ingest_item (recording_id)'),
            ('ix_bulk_ingest_item_run_id', 'CREATE INDEX IF NOT EXISTS ix_bulk_ingest_item_run_id ON bulk_ingest_item (run_id)'),
            ('ix_bulk_ingest_item_status', 'CREATE INDEX IF NOT EXISTS ix_bulk_ingest_item_status ON bulk_ingest_item (status)'),
        ]),
    'collection_recording': (
        'CREATE TABLE collection_recording ( id INTEGER NOT NULL, collection_id INTEGER NOT NULL, recording_id INTEGER NOT NULL, "order" INTEGER NOT NULL, added_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(collection_id) REFERENCES collection (id), FOREIGN KEY(recording_id) REFERENCES recording (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('collection_id', 'collection_id INTEGER NOT NULL REFERENCES collection(id)', True),
            ('recording_id', 'recording_id INTEGER NOT NULL REFERENCES recording(id)', True),
            ('order', '"order" INTEGER NOT NULL', True),
            ('added_at', 'added_at DATETIME', False),
        ],
        [
        ]),
    'quality_analysis': (
        "CREATE TABLE quality_analysis ( id INTEGER NOT NULL, folder_path VARCHAR(1024) NOT NULL, source_dir VARCHAR(1024), name VARCHAR(512), triage_status VARCHAR(16) DEFAULT 'pending' NOT NULL, recording_id INTEGER, error TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, listening_quality FLOAT, score_tone FLOAT, score_noise FLOAT, score_dynamics FLOAT, features_json TEXT, technical_issues_json TEXT, flags_json TEXT, sampled_json TEXT, technical_deduction FLOAT, analysis_version VARCHAR(16), score_version VARCHAR(16), PRIMARY KEY (id), FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE SET NULL )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('folder_path', 'folder_path VARCHAR(1024) NOT NULL', True),
            ('source_dir', 'source_dir VARCHAR(1024)', False),
            ('name', 'name VARCHAR(512)', False),
            ('triage_status', "triage_status VARCHAR(16) DEFAULT 'pending' NOT NULL", False),
            ('recording_id', 'recording_id INTEGER REFERENCES recording(id) ON DELETE SET NULL', False),
            ('error', 'error TEXT', False),
            ('created_at', 'created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL', False),
            ('updated_at', 'updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL', False),
            ('listening_quality', 'listening_quality FLOAT', False),
            ('score_tone', 'score_tone FLOAT', False),
            ('score_noise', 'score_noise FLOAT', False),
            ('score_dynamics', 'score_dynamics FLOAT', False),
            ('features_json', 'features_json TEXT', False),
            ('technical_issues_json', 'technical_issues_json TEXT', False),
            ('flags_json', 'flags_json TEXT', False),
            ('sampled_json', 'sampled_json TEXT', False),
            ('technical_deduction', 'technical_deduction FLOAT', False),
            ('analysis_version', 'analysis_version VARCHAR(16)', False),
            ('score_version', 'score_version VARCHAR(16)', False),
        ],
        [
            ('ix_quality_analysis_folder_path', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_quality_analysis_folder_path ON quality_analysis (folder_path)'),
            ('ix_quality_analysis_recording_id', 'CREATE INDEX IF NOT EXISTS ix_quality_analysis_recording_id ON quality_analysis (recording_id)'),
            ('ix_quality_analysis_source_dir', 'CREATE INDEX IF NOT EXISTS ix_quality_analysis_source_dir ON quality_analysis (source_dir)'),
            ('ix_quality_analysis_triage_status', 'CREATE INDEX IF NOT EXISTS ix_quality_analysis_triage_status ON quality_analysis (triage_status)'),
        ]),
    'recording_event': (
        'CREATE TABLE recording_event ( id INTEGER NOT NULL, recording_id INTEGER NOT NULL, user_id INTEGER NOT NULL, event_type VARCHAR(32) NOT NULL, note TEXT, created_at DATETIME NOT NULL, PRIMARY KEY (id), FOREIGN KEY(recording_id) REFERENCES recording (id), FOREIGN KEY(user_id) REFERENCES user (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('recording_id', 'recording_id INTEGER NOT NULL REFERENCES recording(id)', True),
            ('user_id', 'user_id INTEGER NOT NULL REFERENCES user(id)', True),
            ('event_type', 'event_type VARCHAR(32) NOT NULL', True),
            ('note', 'note TEXT', False),
            ('created_at', 'created_at DATETIME NOT NULL', True),
        ],
        [
        ]),
    'recording_fingerprint': (
        'CREATE TABLE recording_fingerprint ( id INTEGER NOT NULL, recording_id INTEGER NOT NULL, fingerprint_type VARCHAR(8) NOT NULL, filename VARCHAR(255), content TEXT, rel_path VARCHAR(512), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(recording_id) REFERENCES recording (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('recording_id', 'recording_id INTEGER NOT NULL REFERENCES recording(id)', True),
            ('fingerprint_type', 'fingerprint_type VARCHAR(8) NOT NULL', True),
            ('filename', 'filename VARCHAR(255)', False),
            ('content', 'content TEXT', False),
            ('rel_path', 'rel_path VARCHAR(512)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
        ]),
    'recording_image': (
        "CREATE TABLE recording_image ( id INTEGER NOT NULL, recording_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, ext VARCHAR(8) NOT NULL, is_primary BOOLEAN DEFAULT '0' NOT NULL, sort_order INTEGER DEFAULT '0' NOT NULL, origin VARCHAR(24) DEFAULT 'upload' NOT NULL, caption VARCHAR(255), credit VARCHAR(255), source_ref VARCHAR(512), created_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE )",
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('recording_id', 'recording_id INTEGER NOT NULL REFERENCES recording(id) ON DELETE CASCADE', True),
            ('filename', 'filename VARCHAR(255) NOT NULL', True),
            ('ext', 'ext VARCHAR(8) NOT NULL', True),
            ('is_primary', "is_primary BOOLEAN DEFAULT '0' NOT NULL", False),
            ('sort_order', "sort_order INTEGER DEFAULT '0' NOT NULL", False),
            ('origin', "origin VARCHAR(24) DEFAULT 'upload' NOT NULL", False),
            ('caption', 'caption VARCHAR(255)', False),
            ('credit', 'credit VARCHAR(255)', False),
            ('source_ref', 'source_ref VARCHAR(512)', False),
            ('created_at', 'created_at DATETIME', False),
        ],
        [
            ('ix_recording_image_recording_id', 'CREATE INDEX IF NOT EXISTS ix_recording_image_recording_id ON recording_image (recording_id)'),
        ]),
    'recording_quality': (
        'CREATE TABLE recording_quality ( id INTEGER NOT NULL, recording_id INTEGER NOT NULL, analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, listening_quality FLOAT, score_tone FLOAT, score_noise FLOAT, score_dynamics FLOAT, features_json TEXT, technical_issues_json TEXT, flags_json TEXT, sampled_json TEXT, technical_deduction FLOAT, analysis_version VARCHAR(16), score_version VARCHAR(16), PRIMARY KEY (id), FOREIGN KEY(recording_id) REFERENCES recording (id) ON DELETE CASCADE )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('recording_id', 'recording_id INTEGER NOT NULL REFERENCES recording(id) ON DELETE CASCADE', True),
            ('analyzed_at', 'analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL', False),
            ('listening_quality', 'listening_quality FLOAT', False),
            ('score_tone', 'score_tone FLOAT', False),
            ('score_noise', 'score_noise FLOAT', False),
            ('score_dynamics', 'score_dynamics FLOAT', False),
            ('features_json', 'features_json TEXT', False),
            ('technical_issues_json', 'technical_issues_json TEXT', False),
            ('flags_json', 'flags_json TEXT', False),
            ('sampled_json', 'sampled_json TEXT', False),
            ('technical_deduction', 'technical_deduction FLOAT', False),
            ('analysis_version', 'analysis_version VARCHAR(16)', False),
            ('score_version', 'score_version VARCHAR(16)', False),
        ],
        [
            ('ix_recording_quality_recording_id', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_recording_quality_recording_id ON recording_quality (recording_id)'),
        ]),
    'track': (
        'CREATE TABLE track ( id INTEGER NOT NULL, recording_id INTEGER NOT NULL, track_number INTEGER NOT NULL, title VARCHAR(255) NOT NULL, set_number VARCHAR(64), disc_number INTEGER, disc_track_number INTEGER, original_file_path VARCHAR(512), duration INTEGER, file_path VARCHAR(512) NOT NULL, is_official BOOLEAN NOT NULL, flags TEXT, songwriter VARCHAR(255), checksum_type VARCHAR(8), expected_checksum VARCHAR(64), checksum_status VARCHAR(16), checksum_verified_at DATETIME, notes TEXT, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(recording_id) REFERENCES recording (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('recording_id', 'recording_id INTEGER NOT NULL REFERENCES recording(id)', True),
            ('track_number', 'track_number INTEGER NOT NULL', True),
            ('title', 'title VARCHAR(255) NOT NULL', True),
            ('set_number', 'set_number VARCHAR(64)', False),
            ('disc_number', 'disc_number INTEGER', False),
            ('disc_track_number', 'disc_track_number INTEGER', False),
            ('original_file_path', 'original_file_path VARCHAR(512)', False),
            ('duration', 'duration INTEGER', False),
            ('file_path', 'file_path VARCHAR(512) NOT NULL', True),
            ('is_official', 'is_official BOOLEAN NOT NULL', True),
            ('flags', 'flags TEXT', False),
            ('songwriter', 'songwriter VARCHAR(255)', False),
            ('checksum_type', 'checksum_type VARCHAR(8)', False),
            ('expected_checksum', 'expected_checksum VARCHAR(64)', False),
            ('checksum_status', 'checksum_status VARCHAR(16)', False),
            ('checksum_verified_at', 'checksum_verified_at DATETIME', False),
            ('notes', 'notes TEXT', False),
            ('created_at', 'created_at DATETIME', False),
            ('updated_at', 'updated_at DATETIME', False),
        ],
        [
        ]),
    'peer_access_log': (
        'CREATE TABLE peer_access_log ( id INTEGER NOT NULL, peer_id INTEGER NOT NULL, track_id INTEGER NOT NULL, occurred_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(peer_id) REFERENCES peer (id), FOREIGN KEY(track_id) REFERENCES track (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('peer_id', 'peer_id INTEGER NOT NULL REFERENCES peer(id)', True),
            ('track_id', 'track_id INTEGER NOT NULL REFERENCES track(id)', True),
            ('occurred_at', 'occurred_at DATETIME', False),
        ],
        [
            ('ix_peer_access_log_occurred_at', 'CREATE INDEX IF NOT EXISTS ix_peer_access_log_occurred_at ON peer_access_log (occurred_at)'),
            ('ix_peer_access_log_peer_id', 'CREATE INDEX IF NOT EXISTS ix_peer_access_log_peer_id ON peer_access_log (peer_id)'),
            ('ix_peer_access_log_track_id', 'CREATE INDEX IF NOT EXISTS ix_peer_access_log_track_id ON peer_access_log (track_id)'),
        ]),
    'play_log': (
        'CREATE TABLE play_log ( id INTEGER NOT NULL, user_id INTEGER NOT NULL, track_id INTEGER NOT NULL, played_at DATETIME NOT NULL, duration_played INTEGER, completed BOOLEAN NOT NULL, PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES user (id), FOREIGN KEY(track_id) REFERENCES track (id) )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('user_id', 'user_id INTEGER NOT NULL REFERENCES user(id)', True),
            ('track_id', 'track_id INTEGER NOT NULL REFERENCES track(id)', True),
            ('played_at', 'played_at DATETIME NOT NULL', True),
            ('duration_played', 'duration_played INTEGER', False),
            ('completed', 'completed BOOLEAN NOT NULL', True),
        ],
        [
        ]),
    'track_analysis': (
        'CREATE TABLE track_analysis ( id INTEGER NOT NULL, track_id INTEGER NOT NULL, rms_db FLOAT, peak_db FLOAT, noise_floor_db FLOAT, dynamic_range_db FLOAT, sample_rate_hz INTEGER, bit_depth INTEGER, bitrate_kbps INTEGER, clipping_pct FLOAT, dc_offset FLOAT, spectral_centroid_hz FLOAT, spectral_cutoff_hz INTEGER, spectral_flatness FLOAT, duration_s FLOAT, non_music_score FLOAT, bpm FLOAT, waveform_json TEXT, analysis_version VARCHAR(16) NOT NULL, analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, PRIMARY KEY (id), FOREIGN KEY(track_id) REFERENCES track (id) ON DELETE CASCADE )',
        [
            ('id', 'id INTEGER NOT NULL', False),
            ('track_id', 'track_id INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE', True),
            ('rms_db', 'rms_db FLOAT', False),
            ('peak_db', 'peak_db FLOAT', False),
            ('noise_floor_db', 'noise_floor_db FLOAT', False),
            ('dynamic_range_db', 'dynamic_range_db FLOAT', False),
            ('sample_rate_hz', 'sample_rate_hz INTEGER', False),
            ('bit_depth', 'bit_depth INTEGER', False),
            ('bitrate_kbps', 'bitrate_kbps INTEGER', False),
            ('clipping_pct', 'clipping_pct FLOAT', False),
            ('dc_offset', 'dc_offset FLOAT', False),
            ('spectral_centroid_hz', 'spectral_centroid_hz FLOAT', False),
            ('spectral_cutoff_hz', 'spectral_cutoff_hz INTEGER', False),
            ('spectral_flatness', 'spectral_flatness FLOAT', False),
            ('duration_s', 'duration_s FLOAT', False),
            ('non_music_score', 'non_music_score FLOAT', False),
            ('bpm', 'bpm FLOAT', False),
            ('waveform_json', 'waveform_json TEXT', False),
            ('analysis_version', 'analysis_version VARCHAR(16) NOT NULL', True),
            ('analyzed_at', 'analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL', False),
        ],
        [
            ('ix_track_analysis_track_id', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_track_analysis_track_id ON track_analysis (track_id)'),
        ]),
}
