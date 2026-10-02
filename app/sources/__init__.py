"""
app/sources/ -- archive connectors for the Downloads feature (2026-10-01).

Each source implements app.sources.base.Source. Today only the Live Music
Archive ("lma"); Bluegrass Archive ("bga") is a later second source and
needs nothing here beyond another entry.

register_source() exists so tests can inject a fake: the VM has no route to
archive.org and nothing under TESTING may touch the network.
"""

from .base import Source, SourceError, SourceNotFound  # noqa: F401

_REGISTRY = {}


def get_source(name):
    """The connector for `name`, or None for an unknown source."""
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name == "lma":
        from .lma import LmaSource
        _REGISTRY["lma"] = LmaSource()
        return _REGISTRY["lma"]
    return None


def register_source(name, source):
    """Replace (or add) a connector. Pass None to forget it."""
    if source is None:
        _REGISTRY.pop(name, None)
    else:
        _REGISTRY[name] = source
