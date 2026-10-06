"""Ask every agency what it actually calls each watchlist entity.

The watchlist carries names as the lending team writes them. What fetches a
rating is the name the agency's own index uses. This queries all six indexes
(search endpoints only - no rating documents are downloaded) and reports, per
entity, the closest entity name each agency holds.

Nothing is auto-applied: the output is a proposal for review, because renaming a
watchlist row to the wrong agency entity is exactly the error the whole design
exists to prevent.
"""
from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent   # tools/ lives one level down
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.agencies import ADAPTERS  # noqa: E402
from rating_scraper.entities import EntityStore  # noqa: E402
from rating_scraper.http_client import Fetcher  # noqa: E402
from rating_scraper.models import Company  # noqa: E402
from rating_scraper.normalize import canonical_name, name_score  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# An exact-or-near hit: the agency holds this entity under this spelling.
STRONG = 95.0
# Worth a human look - usually the right entity under a fuller registered name.
MAYBE = 70.0


def probe(agency_key: str, names: list[str]) -> dict[str, list[tuple[str, float]]]:
    fetch = Fetcher(cache_dir=ROOT / "data" / "cache", delay=1.2)
    ad = ADAPTERS[agency_key](fetch)
    out: dict[str, list[tuple[str, float]]] = {}
    for n in names:
        try:
            cands = ad.candidates(Company(borrower_name=n))
        except Exception as e:  # noqa: BLE001
            logging.warning("%s: %s -> %s", ad.name, n, e)
            cands = []
        out[n] = cands[:4]
    return out


def main() -> int:
    store = EntityStore(ROOT / "data" / "entities.db")
    names = [e.name for e in store.list(active_only=True)]
    print(f"probing {len(names)} entities x {len(ADAPTERS)} agencies "
          f"({len(names) * len(ADAPTERS)} index lookups)\n")

    with ThreadPoolExecutor(max_workers=len(ADAPTERS)) as pool:
        futs = {k: pool.submit(probe, k, names) for k in ADAPTERS}
        results = {k: f.result() for k, f in futs.items()}

    rows = []
    for n in names:
        best_overall, best_score, found_at = "", 0.0, []
        per_agency = {}
        for key, res in results.items():
            cands = res.get(n) or []
            if not cands:
                per_agency[key] = ""
                continue
            top, score = cands[0]
            per_agency[key] = f"{top}  ({score:.0f})"
            if score >= STRONG:
                found_at.append(key)
            if score > best_score:
                best_overall, best_score = top, score
        rows.append({
            "Watchlist name": n,
            "Best agency spelling": best_overall,
            "Score": round(best_score, 1),
            "Agencies with a strong hit": ", ".join(found_at),
            "n_strong": len(found_at),
            **{f"[{k}]": per_agency.get(k, "") for k in ADAPTERS},
        })

    df = pd.DataFrame(rows)

    print("=" * 118)
    print("A. NAME IS ALREADY RIGHT (an agency holds it verbatim)")
    print("=" * 118)
    ok = df[df["Score"] >= STRONG]
    for _, r in ok.iterrows():
        same = canonical_name(r["Watchlist name"]) == canonical_name(r["Best agency spelling"])
        mark = "" if same else "   -> agency spells it: " + r["Best agency spelling"]
        print(f"   [{r['n_strong']}] {r['Watchlist name'][:52]:<54}{mark}")

    print("\n" + "=" * 118)
    print("B. NEEDS A RENAME (closest agency entity differs)")
    print("=" * 118)
    mid = df[(df["Score"] < STRONG) & (df["Score"] >= MAYBE)].sort_values("Score", ascending=False)
    for _, r in mid.iterrows():
        print(f"   {r['Score']:5.1f}  {r['Watchlist name'][:46]:<48} -> {r['Best agency spelling'][:56]}")

    print("\n" + "=" * 118)
    print("C. NOT FOUND AT ANY AGENCY (unrated, or named quite differently)")
    print("=" * 118)
    low = df[df["Score"] < MAYBE].sort_values("Score", ascending=False)
    for _, r in low.iterrows():
        near = f"   closest: {r['Best agency spelling'][:44]} ({r['Score']:.0f})" if r["Best agency spelling"] else ""
        print(f"   {r['Watchlist name'][:50]:<52}{near}")

    out = ROOT / "output" / "entity_name_check.xlsx"
    out.parent.mkdir(exist_ok=True)
    df.drop(columns=["n_strong"]).to_excel(out, index=False)
    print(f"\nfull per-agency detail: {out}")
    print(f"\nsummary: verbatim={len(ok)}  rename-candidates={len(mid)}  not-found={len(low)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
