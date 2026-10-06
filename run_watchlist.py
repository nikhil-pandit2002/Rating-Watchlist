"""Collect the watchlist's ratings from all six agencies.

  python run_watchlist.py                 # strict: only confirmed matches count
  python run_watchlist.py --discover      # measurement run, auto-match allowed
  python run_watchlist.py --months 15     # age window (default 15, 0 disables)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.agencies import ADAPTERS  # noqa: E402
from rating_scraper.entities import EntityStore  # noqa: E402
from rating_scraper.excel_export import write_workbook  # noqa: E402
from rating_scraper.http_client import Fetcher  # noqa: E402
from rating_scraper.models import Company  # noqa: E402
from rating_scraper.pipeline import Pipeline  # noqa: E402
from rating_scraper.resolutions import ResolutionStore  # noqa: E402

# Confirmed against each agency's own index by check_entity_names.py. India Grid
# Trust and Anzen Energy are not separate entities - the agencies carry only the
# renamed / fuller form, and IndiGrid's index entry says so outright:
# 'IndiGrid Infrastructure Trust (Formerly India Grid Trust)'.
RENAMES = {
    "Cube Highways InvIT Trust": "Cube Highways Trust",
}
MERGES = {
    "India Grid Trust": "IndiGrid Infrastructure Trust",
    "Anzen Energy Yield Plus Trust": "Anzen India Energy Yield Plus Trust",
}


def apply_corrections(store: EntityStore) -> None:
    for old, new in RENAMES.items():
        e = store.find(old)
        if e:
            store.update(e.id, name=new)
            print(f"   renamed: {old}  ->  {new}")
    for old, into in MERGES.items():
        e = store.find(old)
        if e and store.find(into):
            store.delete(e.id)
            print(f"   merged : {old}  ->  {into}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true",
                    help="allow auto-matching (measurement only, not for reporting)")
    ap.add_argument("--months", type=int, default=15, help="age window; 0 disables")
    ap.add_argument("--output", default="output/watchlist_ratings.xlsx")
    ap.add_argument("--fix-names", action="store_true", help="apply verified renames/merges")
    ap.add_argument("--pin-url", nargs=3, metavar=("ENTITY", "AGENCY", "URL"),
                    help="record a rating-document URL for an entity an agency "
                         "hosts but does not expose through its own search")
    ap.add_argument("--list-pins", action="store_true", help="show pinned document URLs")
    args = ap.parse_args()

    if args.pin_url or args.list_pins:
        res = ResolutionStore(ROOT / "data" / "resolutions.db")
        if args.pin_url:
            entity, agency, url = args.pin_url
            known = {a.name for a in (ADAPTERS[k] for k in ADAPTERS)}
            if agency not in known:
                raise SystemExit(f"unknown agency {agency!r}; expected one of {sorted(known)}")
            if not res.set_url("", agency, url, entity, "manually pinned"):
                raise SystemExit("could not store that pin - the URL must start with http")
            print(f"pinned {entity} @ {agency}\n   {url}")
        for row in res.all_urls():
            print(f"   {row['borrower_name'][:40]:<42} {row['agency']:<18} {row['url'][:70]}")
        return 0

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    store = EntityStore(ROOT / "data" / "entities.db")
    if args.fix_names:
        print("applying verified name corrections:")
        apply_corrections(store)
        print()

    entities = store.list(active_only=True)
    companies = [Company(borrower_name=e.name) for e in entities]

    fetch = Fetcher(cache_dir=ROOT / "data" / "cache", delay=1.2)
    adapters = [ADAPTERS[k](fetch) for k in ADAPTERS]
    res = ResolutionStore(ROOT / "data" / "resolutions.db")

    pipe = Pipeline(
        adapters,
        resolutions=res,
        strict=not args.discover,
        max_age_months=args.months or None,
    )
    print(f"{len(companies)} entities x {len(adapters)} agencies"
          f"   mode={'DISCOVER' if args.discover else 'STRICT'}"
          f"   window={args.months or 'off'}m"
          f"   cut-off={pipe.cutoff}\n")

    result = pipe.run(companies)
    out = write_workbook(result, ROOT / args.output)

    with_data = {r.borrower_name for r in result.records}
    print("\n" + "=" * 74)
    print(f"  entities with a rating : {len(with_data)} / {len(companies)}")
    print(f"  rating rows            : {len(result.records)}")
    print(f"  rows naming NaBFID     : {sum(1 for r in result.records if r.nabfid_name)}")
    for a, n in sorted(result.summary()["by_agency"].items()):
        print(f"     {a:<22}: {n}")

    by_status: dict[str, int] = {}
    for q in result.qa:
        by_status[q.status] = by_status.get(q.status, 0) + 1
    print("\n  QA:")
    for s, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"     {s:<22}: {n}")

    aged = [q for q in result.qa if q.status == "outside_age_window"]
    if aged:
        print(f"\n  excluded by the {args.months}-month window ({len(aged)}):")
        for q in aged:
            print(f"     {q.borrower_name[:44]:<46} [{q.agency}] {q.detail[:60]}")

    print(f"\n  missing entirely:")
    for e in entities:
        if e.name not in with_data:
            print(f"     {e.name}")

    print(f"\n  output: {out}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
