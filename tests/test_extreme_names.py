"""Spelling variants agencies actually use, and look-alikes they must not blur.

Every MATCH case here was seen in real agency data. Every APART case is a pair of
genuinely different entities, most of which scored high enough at some point to
be accepted - they are the reason the guards exist.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.models import Company  # noqa: E402
from rating_scraper.normalize import name_score  # noqa: E402

THRESHOLD = 88.0
AUTO = 88.0

# (ours, agency's spelling, why)
MATCH = [
    # -- concatenated compounds: agencies close up words at will --------
    ("Oriental Infra Trust", "Oriental Infratrust", "CRISIL closes the compound"),
    ("Nxt-Infra Trust", "Nxt InfraTrust", "hyphen vs space, closed compound"),
    ("Sustainable Energy Infra Trust", "Sustainable Energy InfraTrust", "closed compound"),
    ("Cube Highways Trust", "CubeHighways Trust", "closed compound"),
    ("Tamil Nadu Power Distribution Corporation Limited",
     "Tamilnadu Power Distribution Corporation Ltd", "place name closed up"),
    # -- separators -----------------------------------------------------
    ("Nxt-Infra Trust", "Nxt_InfraTrust", "underscore from a filename"),
    ("Pimpri Chinchwad Municipal Corporation",
     "Pimpri-Chinchwad Municipal Corporation", "hyphenated place name"),
    ("Nxt-Infra Trust", "Nxt Infra Trust", "hyphen vs space"),
    # -- legal suffix forms ---------------------------------------------
    ("Arya Tankers Private Limited", "Arya Tankers Pvt. Ltd.", "suffix abbreviated"),
    ("Mumbai Urja Marg Limited", "Mumbai Urja Marg Ltd", "suffix abbreviated"),
    ("Maple Infrastructure Trust", "Maple Infrastructure Trust.", "trailing dot"),
    # -- acronym decoration ---------------------------------------------
    ("Surat Municipal Corporation", "Surat Municipal Corporation (SMC)", "acronym suffix"),
    ("KJ Foundation", "KJ Foundation (KJF)", "acronym includes an initialism"),
    ("National Highways Infra Trust", "National Highways Infra Trust (NHIT)", "acronym suffix"),
    # -- agency typos ---------------------------------------------------
    ("MSIDC Nashik Region Road Development Corporation Limited",
     "MSIDC NASHIK REGION ROAD DEVELOPMENT CORPORTION LIMITED", "agency misspells 'corporation'"),
    ("NDR InvIT Trust", "NDR InvIT Trsut", "agency misspells 'trust'"),
    # -- case, spacing, plurals -----------------------------------------
    ("Greater Chennai Corporation", "GREATER CHENNAI CORPORATION", "upper case"),
    ("Interise Trust", "  Interise   Trust  ", "stray whitespace"),
    ("Blooming Blossom Educational Society",
     "Blooming Blossoms Educational Society", "agency uses the plural"),
    # -- ampersand ------------------------------------------------------
    ("Hemavathy Power and Light Private Limited",
     "Hemavathy Power & Light Private Limited", "'and' vs '&'"),
]

# (a, b, why they are different)
APART = [
    ("Oriental Infra Trust", "Indus Infra Trust", "different sponsors"),
    ("Oriental Infra Trust", "Sustainable Energy Infra Trust", "different trusts"),
    ("Nxt-Infra Trust", "Oriental Infratrust", "different trusts"),
    ("MSIDC Nashik Region Road Development Corporation Limited",
     "MSIDC NASHIK REGION II ROAD DEVELOPMENT CORPORATION LIMITED", "Region vs Region II"),
    ("MSRDC Pune Ring Road Eastern Limited", "MSRDC Pune Ring Road Western Limited", "E vs W"),
    ("IndiGrid Trust", "Indigrid Infrastructure Trust", "subset - must be confirmed"),
    ("BNP PARIBAS", "BNP Paribas India Solutions Private Limited", "bank vs captive arm"),
    ("Clean Solar Power (Baniyana) Private Limited",
     "Clean Solar Power (Bhadla) Private Limited", "sibling SPVs"),
    ("Jindal Urban Waste Management (Bawana) Limited",
     "Jindal Urban Waste Management (Kakinada) Limited", "sibling SPVs"),
    ("Anzen India Energy Yield Plus Trust",
     "Anzen Infrastructure Yield Plus Trust", "sibling trusts"),
    ("CITIUS Transnet Infrastructure Trust",
     "Citius Transnet Investment Trust", "sibling trusts"),
    ("Huoban Energy 1 Private Limited", "Huoban Energy 11 Private Limited", "digits differ"),
    ("Surat Municipal Corporation", "Nashik Municipal Corporation", "different cities"),
    ("Global Education Foundation", "Pinnacle Global Foundation", "different foundations"),
    ("IRB Infrastructure Trust", "IRB InvIT Fund", "different vehicles"),
]

# A borrower name must generate a query that can actually retrieve the agency's
# spelling. Literal search endpoints return nothing for a near miss.
SEARCH_TERMS = [
    ("Oriental Infra Trust", "orientalinfratrust"),
    ("Nxt-Infra Trust", "nxtinfratrust"),
    ("Sustainable Energy Infra Trust", "sustainableenergyinfratrust"),
]


def check_agency_name_consistency() -> list[str]:
    """`adapter.name` must be the only spelling of an agency in the system.

    Pins are keyed by adapter.name while output rows carry RatingRecord.agency.
    Three adapters used to hardcode a different string in parse() than their
    name attribute - 'CARE' vs 'CARE Ratings' - so anything that read the agency
    off a result row and wrote a pin with it stored a key no lookup would ever
    match, and the confirmation silently did nothing.
    """
    import re
    from rating_scraper.agencies import ADAPTERS

    bad = []
    src = Path(__file__).resolve().parent.parent / "src" / "rating_scraper" / "agencies"
    names = {c.name for c in ADAPTERS.values()}
    if len(names) != len(ADAPTERS):
        bad.append(f"adapter names are not unique: {sorted(names)}")
    for path in src.glob("*.py"):
        for lit in re.findall(r'agency=(["\'])(.+?)\1', path.read_text()):
            bad.append(f"{path.name}: agency= hardcodes {lit[1]!r}; use self.name")
    return bad


def main() -> int:
    fails = []

    print("MUST MATCH (same entity, different spelling)")
    print("-" * 96)
    for a, b, why in MATCH:
        s = name_score(a, b)
        ok = s >= AUTO
        if not ok:
            fails.append(f"MATCH {a!r} vs {b!r} = {s:.1f} ({why})")
        print(f"  {'PASS' if ok else 'FAIL'} {s:5.1f}  {a[:34]:<36} {b[:36]:<38} {why}")

    print("\nMUST STAY APART (different entities)")
    print("-" * 96)
    for a, b, why in APART:
        s = name_score(a, b)
        ok = s < THRESHOLD
        if not ok:
            fails.append(f"APART {a!r} vs {b!r} = {s:.1f} ({why})")
        print(f"  {'PASS' if ok else 'FAIL'} {s:5.1f}  {a[:34]:<36} {b[:36]:<38} {why}")

    print("\nSEARCH TERMS must include a form that retrieves the agency spelling")
    print("-" * 96)
    for name, wanted in SEARCH_TERMS:
        c = Company(borrower_name=name, broad_search=True)
        squashed = {t.lower().replace(" ", "").replace("-", "").replace("_", "")
                    for t in c.search_terms()}
        ok = wanted in squashed
        if not ok:
            fails.append(f"TERMS {name!r} never produces {wanted!r}")
        print(f"  {'PASS' if ok else 'FAIL'}  {name[:34]:<36} -> needs {wanted!r}")
        if not ok:
            print(f"        produced: {sorted(squashed)[:6]}")

    print("\nAGENCY NAME CONSISTENCY")
    print("-" * 96)
    problems = check_agency_name_consistency()
    fails.extend(problems)
    for msg in problems:
        print(f"  FAIL  {msg}")
    if not problems:
        print("  PASS  one canonical name per adapter, used for pins and output alike")

    print(f"\n{'=' * 96}\nFAILURES: {len(fails)}")
    for f in fails:
        print(f"   {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
