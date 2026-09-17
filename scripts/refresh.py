#!/usr/bin/env python3
"""Rebuild the Gutter Macro Dashboard payload.

    python scripts/refresh_macro.py                    # build to site/data.json
    python scripts/refresh_macro.py --out /data/macro.json --years 5
    python scripts/refresh_macro.py --probe eia        # confirm a keyed API's shape

Credentials come from the environment (or a .env beside this repo):

    BLS_API_KEY       optional — raises the BLS rate limit, nothing more
    EIA_API_KEY       Henry Hub futures curve
    PJM_API_KEY       PJM Western Hub day-ahead LMP
    ERCOT_API_KEY     ERCOT subscription key ...
    ERCOT_USERNAME    ... plus the two credentials its B2C token flow needs
    ERCOT_PASSWORD

Missing credentials are reported, never fatal: the build ships the panels it can.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def load_dotenv(path: Path) -> int:
    """Minimal .env reader — no dependency, and it never overrides a real env var."""
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=REPO / "site" / "data.json",
                    help="where to write the payload (default: site/data.json)")
    ap.add_argument("--site", type=Path, metavar="DIR",
                    help="also assemble a publishable static site in DIR")
    ap.add_argument("--years", type=int, default=10,
                    help="widest range the payload carries (default: 10)")
    ap.add_argument("--probe", choices=("eia", "pjm", "ercot"),
                    help="print a keyed API's live response shape and exit")
    args = ap.parse_args()

    n = load_dotenv(REPO / ".env")
    if n:
        print(f"loaded {n} variable(s) from .env")

    if args.probe:
        return probe(args.probe)

    from gutter_macro.build import build

    result = build(history_years=args.years, out_path=args.out)

    if args.site:
        from gutter_macro.site import build_site

        written = build_site(result.payload, args.site)
        print(f"site assembled in {args.site}: "
              f"{', '.join(p.name for p in written)}")

    print(f"\n{result.ok_count}/{len(result.status)} series built -> {args.out}")
    for key, state in result.status.items():
        if state == "ok":
            continue
        print(f"  - {key:26s} {state}")

    panels = result.payload["panels"]
    print()
    for panel in panels:
        pts = sum(len(s["points"]) for s in panel["series"])
        print(f"  {panel['key']:14s} {len(panel['series'])} series, {pts:>6d} points")
    cap = result.payload.get("capacity")
    if cap:
        print(f"  {'capacity':14s} {len(cap['auctions'])} auctions")

    # A build with nothing in it is a failure even though every step "succeeded".
    return 0 if result.ok_count else 1


def probe(which: str) -> int:
    """Ask a keyed API what it actually returns. The fastest way to confirm the
    field names these modules were written against, the first time a key exists."""
    import json

    if which == "eia":
        from gutter_macro.sources import eia

        key = os.environ.get("EIA_API_KEY")
        if not key:
            print("EIA_API_KEY is not set")
            return 1
        print(json.dumps(eia.probe_route(key), indent=2))
    elif which == "pjm":
        from gutter_macro.sources import pjm

        key = os.environ.get("PJM_API_KEY")
        if not key:
            print("PJM_API_KEY is not set")
            return 1
        print(json.dumps(pjm.probe(key), indent=2, default=str))
        print("\nWESTERN HUB pnode_id:", pjm.resolve_pnode_id(key))
    else:
        from gutter_macro.sources import ercot

        creds = [os.environ.get(k) for k in
                 ("ERCOT_API_KEY", "ERCOT_USERNAME", "ERCOT_PASSWORD")]
        if not all(creds):
            print("ERCOT needs ERCOT_API_KEY, ERCOT_USERNAME and ERCOT_PASSWORD")
            return 1
        print(json.dumps(ercot.probe(*creds), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
