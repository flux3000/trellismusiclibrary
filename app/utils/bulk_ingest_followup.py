"""
app/utils/bulk_ingest_followup.py -- Bulk Ingest follow-up queue (spec chunk 6).

Thin wrapper: the actual implementation lives beside the follow-up queue
itself in app/api/ingest.py (enqueue_followups(), _enqueue(), _handle_item()
and friends), because that is where _ANALYSIS_Q, the single worker thread and
the /api/ingest/pipeline reporting already live -- splitting the queue logic
across two modules would only invite the two copies to drift.

This module exists only so app/utils/bulk_ingest_run.py (chunk 5) has something
to import that does not create a circular import: bulk_ingest_run is imported
by app/api routes that themselves import app/api/ingest, so bulk_ingest_run
cannot import app.api.ingest at module load time without risking a cycle.
The import here is deferred into the function body for the same reason.
"""


def enqueue_followups(run=None):
    """
    Queue derived-from-state follow-up work after a Bulk Ingest run.

    `run` (an BulkIngestRun, or None) is accepted for the call site in
    bulk_ingest_run.py but unused: the underlying query is global ("every
    recording with no RecordingQuality row", "every artist never looked
    up"), not scoped to one run, so a specific run object adds nothing here.
    """
    # S4b: reconcile any 'review' BulkIngestItem whose staging row has since
    # been accepted through Review & Ingest, without waiting for a re-run's
    # discover() to notice. Deferred import for the same reason as below.
    from app.utils.bulk_ingest_run import _reconcile_review_items
    _reconcile_review_items(run=run)

    from app.api.ingest import enqueue_followups as _impl
    return _impl()
