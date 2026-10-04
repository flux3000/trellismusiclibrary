"""
A synthetic Atlas of realistic size, for timing the lookup path (never shipped, never used by tests).

    python3 -m app.atlas.synth /tmp/synth.sqlite [--acts 150000]
    python3 -m app.utils.resolver_eval --tier G1 --reader-only --atlas /tmp/synth.sqlite --timing 2 --no-write

Same seed, same file. Names are made-up syllables, so it measures index and scoring cost,
not accuracy.
"""
import argparse
import random
import time

from app.atlas.build import StaticSources, build_atlas

_SYL = ("ba be bi bo bu ka ke ki ko ku la le li lo lu ma me mi mo mu na ne ni no nu ra re ri ro ru "
        "sa se si so su ta te ti to tu th sh ch st gr br cr tr dr fl pl").split()


def make(path, acts=150000, places=30000, events=40000, seed=7, log=print):
    rnd = random.Random(seed)
    word = lambda: "".join(rnd.choice(_SYL) for _ in range(rnd.randint(2, 4))).capitalize()
    uid = lambda n: f"{n:08x}-0000-4000-8000-000000000000"
    areas = [{"id": uid(1), "name": "Testland", "type": "Country", "iso-3166-1-codes": ["TL"]}]
    pl = [{"id": uid(1000000 + i), "name": f"{word()} {rnd.choice(['Theater', 'Hall', 'Club', 'Arena', 'Ballroom', 'Room'])}",
           "type": "Venue", "area": {"id": uid(1)}} for i in range(places)]
    ev = [{"id": uid(2000000 + i), "name": f"{word()} {word()} Festival {rnd.randint(1970, 2020)}", "type": "Festival",
           "life-span": {"begin": f"{rnd.randint(1970, 2020)}-06-{rnd.randint(10, 28)}"},
           "relations": [{"type": "main performer", "target-type": "artist",
                          "artist": {"id": uid(3000000 + rnd.randrange(acts))}}]} for i in range(events)]
    artists, rgs = [], []
    for i in range(acts):
        a = {"id": uid(3000000 + i), "name": f"{word()} {word()}" if rnd.random() < .7 else word(),
             "type": rnd.choice(["Person", "Group"]),
             "aliases": [{"name": f"{word()} {word()}", "type": "Search hint"}] if rnd.random() < .4 else []}
        artists.append(a)
        rgs.append({"artist-credit": [{"artist": {"id": a["id"]}}]})
    t = time.time()
    res = build_atlas(path, StaticSources(areas=areas, places=pl, events=ev, artists=artists, release_groups=rgs),
                      min_release_groups=1, log=lambda *_: None)
    log(f"{path}: {res['bytes'] / 1e6:.1f} MB, {res['counts']}, {time.time() - t:.0f} s")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1].strip())
    ap.add_argument("out")
    ap.add_argument("--acts", type=int, default=150000)
    a = ap.parse_args()
    make(a.out, acts=a.acts)
