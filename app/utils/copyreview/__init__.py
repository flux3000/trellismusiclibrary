"""
copyreview — extract, review and sign off every string the app shows a user.

Written because Trellis's copy is almost entirely AI-authored and largely
unreviewed, and because several hundred of those strings sit on paths nobody
can reach by using the app: catch blocks, empty-state guards, API errors.

The standing rule this serves (Ryan, 2026-09-17): no user-facing text ships
authored solely by an agent. An agent may propose copy; a human approves it.
The drift gate in tests/ is a backstop for text that slipped through, not a
licence to add unreviewed strings and let something else notice.

Entry points:

    python3 -m app.utils.copyreview --report      # what is unreviewed
    python3 tools/copy_review.py                  # the review surface
"""

from .extract import Extraction, Site, Unit, extract      # noqa: F401
from .ledger import Ledger                                # noqa: F401
