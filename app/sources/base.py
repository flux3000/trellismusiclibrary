"""
app/sources/base.py -- the interface every archive connector implements.

Item dicts use the shape in the Archive Downloads spec (section 6) minus the
two fields that depend on the local install, `in_library` and `job`, which the
API layer adds.
"""


class SourceError(Exception):
    """The archive could not be reached or answered badly."""


class SourceNotFound(SourceError):
    """The archive has no such item."""


class Source:
    name = ""

    def recent(self, page=1, sort="newest"):
        """-> ([Item], has_more)"""
        raise NotImplementedError

    def search(self, q, page=1, sort="newest"):
        """-> ([Item], has_more)"""
        raise NotImplementedError

    def item(self, item_id):
        """-> ItemDetail dict (Item + provenance, tracks, info text)."""
        raise NotImplementedError

    def download_plan(self, item_id):
        """-> [{name, url, size, md5}] for every file worth fetching."""
        raise NotImplementedError
