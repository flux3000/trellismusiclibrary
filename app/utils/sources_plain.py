"""
Plain-language sources for the Resolver's Sources popover (Lomax UI).

Pure: turns one resolver field (Field.to_dict()) into {metadata, info_files, folder,
reference_match}. Each value is the quoted text that source offered, or None when it had
nothing. reference_match is the text the library or the Atlas matched, or None. The page
supplies the labels; this module only maps the resolver's evidence to the four rows.
"""

FIELD_NAMES = ("date", "artist", "venue", "event", "stage", "city", "state", "country", "source", "lineage")
_ROWS = {"tags": "metadata", "info": "info_files", "folder": "folder", "library": "reference_match", "atlas": "reference_match"}


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def sources_plain_field(field):
    """One resolver field dict -> the four plain rows (None where a source had nothing)."""
    out = {"metadata": None, "info_files": None, "folder": None, "reference_match": None}
    if not isinstance(field, dict):
        return out
    for src, value in (field.get("candidates") or {}).items():
        row = _ROWS.get(src)
        if row and row != "reference_match":
            out[row] = _text(value)
    for ev in field.get("evidence") or []:
        row = _ROWS.get((ev or {}).get("source"))
        if row and out[row] is None:
            out[row] = _text(ev.get("text"))
    return out


def with_sources_plain(resolved):
    """Add a top-level `sources_plain` {field: rows} to a resolver dict (in place); returns it.
    A missing or non-dict reading is returned unchanged."""
    if isinstance(resolved, dict):
        resolved["sources_plain"] = {n: sources_plain_field(resolved.get(n)) for n in FIELD_NAMES
                                     if isinstance(resolved.get(n), dict)}
    return resolved
