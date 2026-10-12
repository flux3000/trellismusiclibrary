"""app/utils/genre_color.py: the colour a new Genre gets when none is given.

Every Genre has a colour. Callers that create one without an explicit colour
use default_genre_color(); the picker's choice, when there is one, wins.
"""

DEFAULT_GENRE_COLORS = (
    "#c9605a", "#3f7fa8", "#6a7fb5", "#7a9a5b", "#5fa39a", "#9a8f5e",
    "#c08a4a", "#d08a2e", "#a05a3c", "#3f9a6a", "#b06a8a", "#7d7f88",
)


def default_genre_color(existing_colors):
    """The least-used palette colour among `existing_colors` (any iterable of
    '#rrggbb' strings or None); ties go to the earlier palette entry."""
    used = {}
    for c in existing_colors or ():
        if c:
            k = str(c).strip().lower()
            used[k] = used.get(k, 0) + 1
    return min(DEFAULT_GENRE_COLORS,
               key=lambda c: (used.get(c, 0), DEFAULT_GENRE_COLORS.index(c)))


def next_genre_color(session):
    """default_genre_color() against the genres already in `session`'s database."""
    from app.models.genre import Genre
    return default_genre_color(c for (c,) in session.query(Genre.color))
