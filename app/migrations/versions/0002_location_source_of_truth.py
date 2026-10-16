"""location is the source of truth for where a recording is

Revision ID: 0002_location_source_of_truth
Revises: 0001_baseline
Create Date: 2026-10-15

Recording.location now says where a recording is (queue, workshop, backlog,
library) and Recording.is_published is derived from it. The only schema change is
an index on location, because every library list now filters on it.

The old is_published column STAYS in the table. The model maps it as a legacy
column that mirrors location, so a build from before this change still reads the
right answer. A later revision drops it once every install has started this
version.

No data step. A recording that was catalogued in place inside a working folder
(is_published = 0, location = 'library') can only be mapped to Workshop or Backlog
with the configured folder paths and the disk, which a migration does not have. The
boot pass app.utils.published.unpublish_working_folder_recordings does it, and
logs how many it could not map.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_location_source_of_truth"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    have = {i["name"] for i in sa.inspect(bind).get_indexes("recording")}
    if "ix_recording_location" not in have:
        op.create_index("ix_recording_location", "recording", ["location"])


def downgrade():
    op.drop_index("ix_recording_location", table_name="recording")
