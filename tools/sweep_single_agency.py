"""Entities we hold from one agency only: does another agency rate them too?

A single-agency result is the shape a *missed* match leaves behind, so every one
is re-probed against all seven with the hardened matcher. Anything new here was
being lost to a spelling difference, not absent from the agency.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent   # tools/ lives one level down
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.agencies import ADAPTERS  # noqa: E402
from rating_scraper.agencies.base import AmbiguousMatchError  # noqa: E402
from rating_scraper.http_client import Fetcher  # noqa: E402
from rating_scraper.models import Company  # noqa: E402
from rating_scraper.normalize import name_score  # noqa: E402
from rating_scraper.pipeline import months_before  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
CUTOFF = months_before(date.today(), 15)


def probe(key: str, names: list[str]) -> dict[str, dict]:
    fetch = Fetcher(cache_dir=ROOT / "data" / "cache", delay=1.2)
    ad = ADAPTERS[key](fetch)
    out: dict[str, dict] = {}
    for n in names:
        rec = {"status": "-", "detail": ""}
        try:
            doc = ad.latest(Company(borrower_name=n, broad_search=True))
        except AmbiguousMatchError as e:
            rec = {"status": "ambiguous", "detail": "; ".join(c for c, _ in e.choices[:3])}
            out[n] = rec
            continue
        except Exception as e:  # noqa: BLE001
            out[n] = {"status": "error", "detail": type(e).__name__}
            continue
        if doc:
            try:
                recs = ad.parse(doc)
            except Exception:
                recs = []
            fresh = doc.published is None or doc.published >= CUTOFF
            rec = {
                "status": "rated" if fresh else "stale",
                "detail": (f"{doc.published} {recs[0].rating if recs else '?'} "
                           f"{recs[0].outlook if recs else ''}".strip()),
                "entity": doc.company_name,
                "score": round(name_score(n, doc.company_name), 1),
            }
        out[n] = rec
    return out


def main() -> int:
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "sweep_src.xlsx"
    shutil.copy(ROOT / "output" / "watchlist_ratings.xlsx", tmp)
    df = pd.read_excel(tmp, sheet_name="Ratings").fillna("")

    per = df.groupby("Borrower Name")["Rating Agency Name"].nunique()
    singles = sorted(per[per == 1].index)
    have = {n: set(df[df["Borrower Name"] == n]["Rating Agency Name"]) for n in singles}
    print(f"{len(singles)} entities currently held from a single agency\n")

    with ThreadPoolExecutor(max_workers=len(ADAPTERS)) as pool:
        futs = {k: pool.submit(probe, k, singles) for k in ADAPTERS}
        res = {k: f.result() for k, f in futs.items()}

    by_agency = {k: ADAPTERS[k].name for k in ADAPTERS}
    new_hits, rows = 0, []
    for n in singles:
        found_new = []
        for k, name in by_agency.items():
            r = res[k].get(n) or {}
            if r.get("status") == "rated" and name not in have[n]:
                found_new.append((name, r))
        line = f"{n[:44]:<46} have={sorted(have[n])[0][:16]:<17}"
        if found_new:
            new_hits += len(found_new)
            print(f"NEW  {line}")
            for name, r in found_new:
                print(f"        + {name:<18} {r['detail'][:34]:<36} "
                      f"as {r.get('entity','')[:30]!r} ({r.get('score','')})")
                rows.append({"Entity": n, "New agency": name, "Rating": r["detail"],
                             "Agency spelling": r.get("entity", ""), "Score": r.get("score", "")})
        else:
            amb = [by_agency[k] for k in by_agency if (res[k].get(n) or {}).get("status") == "ambiguous"]
            stale = [by_agency[k] for k in by_agency if (res[k].get(n) or {}).get("status") == "stale"]
            extra = ""
            if amb:
                extra += f"  ambiguous at: {', '.join(amb)}"
            if stale:
                extra += f"  stale at: {', '.join(stale)}"
            print(f"     {line}{extra}")

    print(f"\n{'=' * 92}\nNEW cross-agency ratings found: {new_hits}")
    if rows:
        out = ROOT / "output" / "cross_agency_gains.xlsx"
        pd.DataFrame(rows).to_excel(out, index=False)
        print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
