"""Alembic environment for the Trellis library database.

Developer note (how schema changes work from now on)
----------------------------------------------------
* A schema change is an Alembic revision. Change the model, then from the repo
  root with the venv active:
      alembic revision --autogenerate -m "short description"
  Read the generated file in app/migrations/versions/ before keeping it:
  autogenerate misses renames and data changes, and it can propose drops.
* Batch mode is on (render_as_batch). SQLite cannot drop or alter a column in
  place; batch_alter_table rebuilds the table. Use op.batch_alter_table for
  anything beyond add_column / create_table / create_index.
* Every revision must work on SQLite, which is the only database Trellis uses.
* Revisions run inside one transaction at app start (app/utils/db_upgrade.py),
  after a backup. Do not commit, and do not open another connection.
* Keep revisions to the database. Backfills that read or move files in the
  library or on the NAS do not belong in a migration: a migration runs at every
  start of an older install, on a machine whose drive may not be mounted.
* Never edit a revision that has shipped. Add a new one.
* `alembic revision --autogenerate` / `alembic check` against a database that the baseline upgraded
  (not one built by create_all) reports add_fk/remove_fk for recording.recording_engineer_id,
  transfer_engineer_id and mastering_engineer_id. Ignore them: ALTER TABLE ADD COLUMN leaves an
  inline `REFERENCES ... ON DELETE SET NULL` that SQLAlchemy cannot read back (PRAGMA
  foreign_key_list shows the constraint is right).
* recording.is_published is a legacy column mapped as Recording._is_published_legacy (the public
  is_published is a hybrid on location). It is still in the model on purpose, so autogenerate
  sees no difference. Drop column and attribute together in a later revision (0002_location_source_of_truth).
* 0001_baseline reads app/utils/baseline_schema.py, not the models. Leave both alone.

The app runs revisions itself at start. The alembic command is for developers:
  alembic upgrade head | alembic current | alembic history
(it uses the database in config.py, or sqlalchemy.url if you set one).
"""
from alembic import context
from sqlalchemy import pool

import app.models  # noqa: F401  registers every model on db.metadata
from app.extensions import db

config = context.config
target_metadata = db.metadata

_OPTS = dict(
    target_metadata=target_metadata,
    render_as_batch=True,       # SQLite cannot alter or drop a column in place
    compare_type=True,
    transactional_ddl=True,     # SQLite can roll DDL back; db_upgrade.migration_engine makes it so
)


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, literal_binds=True, **_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection):
    context.configure(connection=connection, **_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is not None:      # the app: db_upgrade passes its own connection
        _run(connection)
        return
    from config import Config       # the alembic command
    from app.utils.db_upgrade import migration_engine, sqlite_path_from_uri
    url = config.get_main_option("sqlalchemy.url") or Config.SQLALCHEMY_DATABASE_URI
    path = sqlite_path_from_uri(url)
    engine = migration_engine(path)
    try:
        with engine.begin() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
