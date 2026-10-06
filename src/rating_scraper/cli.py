"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .agencies import ADAPTERS
from .excel_export import write_workbook
from .http_client import Fetcher
from .models import Company
from .pipeline import Pipeline
from .resolutions import ResolutionStore

log = logging.getLogger("rating_scraper")

# Accept the obvious spelling variants people use in their own sheets.
COLUMN_ALIASES = {
    "borrower name": "borrower_name",
    "borrower": "borrower_name",
    "company name": "borrower_name",
    "company": "borrower_name",
    "name": "borrower_name",
    "cin": "cin",
    "pan": "pan",
    "alias": "aliases",
    "aliases": "aliases",
    "other names": "aliases",
}


def load_companies(path: Path) -> list[Company]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"input file not found: {path}")

    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)

    df.columns = [COLUMN_ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in df.columns]
    if "borrower_name" not in df.columns:
        raise SystemExit(
            f"input sheet must have a 'Borrower Name' column; found: {list(df.columns)}"
        )

    companies: list[Company] = []
    for _, row in df.iterrows():
        name = str(row.get("borrower_name") or "").strip()
        if not name or name.lower() == "nan":
            continue
        raw_alias = str(row.get("aliases") or "").strip()
        aliases = [a.strip() for a in raw_alias.split(";") if a.strip() and a.strip().lower() != "nan"]
        companies.append(
            Company(
                borrower_name=name,
                cin=_clean(row.get("cin")),
                pan=_clean(row.get("pan")),
                aliases=aliases,
            )
        )
    return companies


def _clean(v) -> str:
    s = str(v or "").strip()
    return "" if s.lower() in ("nan", "none") else s


def write_template(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "Borrower Name": "National Bank For Financing Infrastructure And Development",
                "CIN": "U65999DL2021PLC376693",
                "PAN": "",
                "Aliases": "NaBFID",
            },
            {
                "Borrower Name": "Adani Ports and Special Economic Zone Limited",
                "CIN": "L63090GJ1998PLC034182",
                "PAN": "",
                "Aliases": "APSEZ",
            },
        ]
    ).to_excel(path, index=False)
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rating-scraper",
        description="Collect latest credit ratings for a list of borrowers from Indian CRAs.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter input workbook")
    init.add_argument("--output", default="input/companies.xlsx")

    run = sub.add_parser("run", help="collect ratings and write the Excel output")
    run.add_argument("--input", default="input/companies.xlsx", help="borrower list")
    run.add_argument("--output", default="output/ratings.xlsx", help="Excel output path")
    run.add_argument(
        "--agencies",
        default="all",
        help=f"comma separated subset of: {','.join(ADAPTERS)} (default: all)",
    )
    run.add_argument("--cache-dir", default="data/cache")
    run.add_argument("--delay", type=float, default=1.5, help="seconds between requests per host")
    run.add_argument("--limit", type=int, default=0, help="only process the first N borrowers")
    run.add_argument("-v", "--verbose", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "init":
        path = write_template(Path(args.output))
        print(f"wrote starter workbook: {path}")
        print("Fill in Borrower Name (required), CIN, PAN, and optional Aliases (semicolon separated).")
        return 0

    companies = load_companies(Path(args.input))
    if args.limit:
        companies = companies[: args.limit]
    if not companies:
        raise SystemExit("no borrowers found in the input sheet")

    if args.agencies.strip().lower() == "all":
        wanted = list(ADAPTERS)
    else:
        wanted = [a.strip() for a in args.agencies.split(",") if a.strip()]
        unknown = [a for a in wanted if a not in ADAPTERS]
        if unknown:
            raise SystemExit(f"unknown agencies: {unknown}. available: {list(ADAPTERS)}")

    fetcher = Fetcher(cache_dir=Path(args.cache_dir), delay=args.delay)
    adapters = [ADAPTERS[a](fetcher) for a in wanted]

    # Shared with the web app (data/resolutions.db) so a company confirmed in
    # either interface is remembered by the other. Without this, every CLI run
    # re-resolves every borrower from scratch, and confident matches made here
    # are never saved for the next run either.
    resolutions = ResolutionStore(Path(args.cache_dir).parent / "resolutions.db")

    log.info("%d borrower(s) x %d agenc(ies): %s", len(companies), len(adapters), ", ".join(wanted))

    result = Pipeline(adapters, resolutions=resolutions).run(companies)
    out = write_workbook(result, Path(args.output))

    stats = result.summary()
    print("\n" + "=" * 60)
    print(f"  rating rows          : {stats['records']}")
    print(f"  borrowers with data  : {stats['companies_with_ratings']} / {len(companies)}")
    print(f"  rows naming NaBFID   : {sum(1 for r in result.records if r.nabfid_name)}")
    for agency, n in sorted(stats["by_agency"].items()):
        print(f"    {agency:<22}: {n}")
    print(f"  QA rows              : {stats['qa_rows']}")
    print(f"  output               : {out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
