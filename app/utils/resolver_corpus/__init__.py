"""
app/utils/resolver_corpus -- dev-only collectors for the Resolver v2 corpora.

    G3  bluegrassarchive.com catalog (content on gdarchive.net/Bluegrass/)   bluegrass.py
    G2  Live Music Archive sample (official search / metadata APIs)          lma.py

Run on Ryan's Mac, never from the app, never in tests (tests use canned
fixtures and an injected opener):

    python3 -m app.utils.resolver_corpus g3 [--limit N] [--resume] [--plan]
    python3 -m app.utils.resolver_corpus g2 [--limit N] [--resume] [--plan]

Text and metadata only; audio is never fetched (polite.py refuses). Output goes
to ~/Workshop/dev/resolver-corpus/ (outside every repo; the repo is public).
"""
