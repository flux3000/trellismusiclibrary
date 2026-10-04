"""python3 -m app.utils.resolver_corpus g3|g2 [--limit N] [--resume] [--plan]"""
import argparse
import sys
from pathlib import Path

from . import common
from .polite import BotProtection, FetchError, PoliteFetcher, RequestCap


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m app.utils.resolver_corpus",
                                 description="Collect a Resolver v2 corpus (dev only; text and metadata, never audio)")
    ap.add_argument("tier", choices=("g3", "g2"))
    ap.add_argument("--limit", type=int, help="hard cap on items collected (a smoke run)")
    ap.add_argument("--resume", action="store_true",
                    help="reuse everything already cached (this is also the default; a rerun never refetches)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and fetch everything again")
    ap.add_argument("--plan", action="store_true", help="print request counts and time, fetch nothing")
    ap.add_argument("--sample", type=int, default=common.SAMPLE_SIZE, help="sample size (default 1000)")
    ap.add_argument("--max-requests", type=int, help="hard cap on network requests this run")
    ap.add_argument("--out", help="corpus folder (default ~/Workshop/dev/resolver-corpus)")
    args = ap.parse_args(argv)

    out = Path(args.out).expanduser() if args.out else common.DEFAULT_CORPUS
    cache = out / "cache" / args.tier
    f = PoliteFetcher(cache, min_interval=common.INTERVAL, jitter=common.JITTER,
                      max_requests=args.max_requests, refresh=args.refresh)
    if args.tier == "g3":
        from . import bluegrass as mod
    else:
        from . import lma as mod
    if args.plan:
        print(mod.plan(f, args.sample, args.limit))
        return 0
    n = f.cache_size()
    print(f"{args.tier}: cache {cache} holds {n} entries" + (" (ignored: --refresh)" if args.refresh else ""))
    try:
        mod.collect(f, out, args.sample, args.limit)
    except BotProtection as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 2
    except RequestCap as e:
        print(f"STOPPED: {e}; rerun with --resume to continue from the cache", file=sys.stderr)
        return 3
    except FetchError as e:
        print(f"STOPPED: {e}; rerun to continue from the cache", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\ninterrupted; rerun the same command to continue from the cache", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
