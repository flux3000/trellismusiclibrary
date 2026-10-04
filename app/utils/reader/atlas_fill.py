"""
Country and state for a bare city, from the Atlas (Resolver v2, chunk 4b).

"Royal Albert Hall, London" names a city and nothing else. The Atlas holds every GeoNames
city of 5,000 people or more and every MusicBrainz place, so it can often say which London.
A value is filled only when it is not a guess:

    1. the venue is an Atlas place that sits in this city: its country (and state) is used;
    2. else the city name belongs to one country in the Atlas, or one country's city is
       more than RATIO times the size of every other's (Paris is France, Paris, Texas is
       a town); the state follows the same rule inside that country.

Otherwise nothing is filled. Pure given an Atlas; None reads as "no help".
"""
from .library import norm_key
from .place import _AU, _CA, _US, _country_display

RATIO = 20.0
MIN_POPULATION = 100000

_STATE_CODES = {
    "US": {name.lower(): code for code, name in _US.items()},
    "CA": {v[0].lower(): k for k, v in _CA.items()},
    "AU": {v[0].lower(): k for k, v in _AU.items()},
}


def _state(iso, region):
    return _STATE_CODES.get(iso, {}).get((region or "").lower())


def _dominant(pops):
    """The one key of {key: population} that is unambiguous, else None."""
    if not pops:
        return None
    if len(pops) == 1:
        return next(iter(pops))
    ranked = sorted(pops.items(), key=lambda kv: -kv[1])
    (k1, p1), (_, p2) = ranked[0], ranked[1]
    return k1 if p1 >= MIN_POPULATION and p1 >= RATIO * max(p2, 1) else None


def fill_place(atlas, city, venue=None):
    """(state, country) for `city`, either may be None. See the module note."""
    if atlas is None or not city:
        return None, None
    ck = norm_key(city)
    if venue:
        for c in atlas.venue(venue, limit=5, fuzzy=False):
            if c.extra.get("country") and c.extra.get("city") and norm_key(c.extra["city"]) == ck:
                return _state(c.extra["country"], c.extra.get("region")), _display(c.extra["country"])
    cands = [c for c in atlas.area(city, limit=80)
             if c.extra.get("country") and c.extra.get("area_kind") in ("city", "municipality", "district")]
    pops = {}
    for c in cands:
        pops[c.extra["country"]] = max(pops.get(c.extra["country"], 0), c.extra.get("population") or 0)
    iso = _dominant(pops)
    if iso is None:
        return None, None
    regions = {}
    for c in cands:
        if c.extra["country"] == iso and c.extra.get("region"):
            regions[c.extra["region"]] = max(regions.get(c.extra["region"], 0), c.extra.get("population") or 0)
    region = _dominant(regions)
    return (_state(iso, region) if region else None), _display(iso)


def _display(iso):
    try:
        return _country_display(iso)
    except KeyError:
        return None
