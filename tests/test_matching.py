"""Company-name matching guards.

These encode two failure modes found against live agency data:

  * a short name sharing one distinctive word with a long one scored high
    enough to attach another company's rating to a borrower
    ('R R Infrastructure' vs NaBFID, 90.5 on the word 'infrastructure');
  * sibling SPVs tie at exactly the threshold while the parent - the correct
    answer - falls below it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rating_scraper.agencies.base import AgencyAdapter, AmbiguousMatchError  # noqa: E402
from rating_scraper.models import Company  # noqa: E402
from rating_scraper.normalize import canonical_name, name_score  # noqa: E402

checks = 0
failures = []


def check(cond, label):
    global checks
    checks += 1
    if not cond:
        failures.append(label)


class _Stub(AgencyAdapter):
    name = "Stub"

    def __init__(self):
        pass

    def search(self, company):
        return []

    def parse(self, doc):
        return []


a = _Stub()

# -- false positives must be rejected -----------------------------------------
NABFID = "National Bank for Financing Infrastructure and Development"
check(name_score(NABFID, "R R Infrastructure") < 88,
      f"NaBFID vs R R Infrastructure = {name_score(NABFID, 'R R Infrastructure'):.1f}, want <88")
check(name_score("Adani Power Limited", "Adani Ports and Special Economic Zone Limited") < 88,
      "Adani Power vs Adani Ports should not match")
check(name_score("Reliance Industries Limited", "Reliance Infrastructure Limited") < 88,
      "Reliance Industries vs Reliance Infrastructure should not match")

# Sharing only trade words is not sharing an identity. These three scored
# 87-91 against each other on 'Spinning Mills' alone.
for other in ["S.S. Spinning Mills", "Suryauday Spinning Mills Private Limited",
              "SVR Spinning Mills Private Limited"]:
    s = name_score("Suriya Spinning Mills", other)
    check(s < 88, f"Suriya vs {other} = {s:.1f}, want <88")
check(name_score("Bharat Steel Industries", "Kalyani Steel Industries") < 88,
      "different companies sharing 'Steel Industries' should not match")

# Infrastructure portfolios are full of numbered SPVs. These are separate
# borrowers with separate ratings; string similarity alone put them at 93-98,
# so an unrated one would silently inherit a sibling's rating.
for i in (2, 3, 5, 9, 10, 11):
    s = name_score("Huoban Energy 1 Pvt Ltd", f"Huoban Energy {i} Pvt Ltd")
    check(s < 88, f"Huoban 1 vs {i} = {s:.1f}, want <88")
for i in (3, 14, 21, 25):
    s = name_score("Solarcraft Power India 2 Private Limited",
                   f"Solarcraft Power India {i} Private Limited")
    check(s < 88, f"Solarcraft 2 vs {i} = {s:.1f}, want <88")
check(name_score("Nashik Municipal Corporation (Tranche 1)",
                 "Nashik Municipal Corporation (Tranche 2)") < 88,
      "different tranches are different issues")
check(name_score("MSRDC Pune Ring Road Eastern Limited",
                 "MSRDC Pune Ring Road Western Limited") < 88,
      "Eastern and Western are separate SPVs")
# ...but the number matching must not break an honest match
check(name_score("Huoban Energy 7 Pvt Ltd", "Huoban Energy 7 Private Limited") >= 95,
      "same numbered SPV, different suffix, must still match")
check(name_score("Solarcraft Power India 21 Private Limited",
                 "Solarcraft Power India 21 Pvt Ltd") >= 95,
      "same numbered SPV with abbreviated suffix must still match")

# -- genuine matches must survive ---------------------------------------------
check(name_score("Adani Power Limited", "Adani Power Limited") == 100, "identical = 100")
check(name_score("Adani Power", "Adani Power Limited") == 100, "suffix-only difference = 100")
check(name_score("NTT Global Data Centers & Cloud Infrastructure India Private Limited",
                 "NTT Global Data Centers & Cloud Infrastructure India Private Limited") == 100,
      "long identical = 100")
check(name_score("Shree Ram Proteins Limited", "Shree Ram Proteins Ltd") >= 95,
      "Ltd/Limited variants should match")

# Registry names say 'PRIVATE LIMITED'; agencies often write 'Pvt. Ltd.'. The
# full stops became spaces before suffix-stripping ran, so the 'pvt ltd'
# pattern (single space) never matched and a stray 'pvt' polluted every such
# comparison - it cost Devanahalli 15 points and the match.
for ours, theirs in [
    ("DEVANAHALLI KOLAR HIGHWAYS PRIVATE LIMITED", "Devanahalli Kolar Highway Pvt. Ltd."),
    ("ACHINTYA SOLAR POWER PRIVATE LIMITED", "Achintya Solar Power Pvt. Ltd."),
    ("ANIMALA WIND POWER PRIVATE LIMITED", "Animala Wind Power Pvt. Ltd."),
    ("EVEREST POWER PRIVATE LIMITED", "Everest Power Pvt. Ltd."),
]:
    s = name_score(ours, theirs)
    check(s >= 95, f"Pvt. Ltd. vs PRIVATE LIMITED: {ours[:28]} = {s:.1f}, want >=95")
check("pvt" not in canonical_name("Achintya Solar Power Pvt. Ltd.").split(),
      "canonical_name must not leave a stray 'pvt' token")

# -- ambiguity is flagged, never guessed --------------------------------------
NTT = [
    "NTT Global Data Centers & Cloud Infrastructure India Private Limited",
    "NTT GLOBAL DATA CENTERS BLR4 PRIVATE LIMITED",
    "NTT Global Data Centers Del2 Private Limited",
    "NTT Global Data Centers NAV2 Private Limited",
]
try:
    a.pick_company(Company(borrower_name="NTT Global Data Center"), NTT)
    check(False, "vague NTT name should raise AmbiguousMatchError")
except AmbiguousMatchError as e:
    check(len(e.choices) >= 4, "ambiguity should offer the parent as a choice too")
    check(any("Cloud Infrastructure" in n for n, _ in e.choices),
          "the correct parent must be among the offered choices")

# exact name resolves without ambiguity
matched, score = a.pick_company(
    Company(borrower_name="NTT Global Data Centers & Cloud Infrastructure India Private Limited"),
    NTT,
)
check(matched == NTT[0] and score == 100, "exact parent name resolves cleanly")

# a precise alias rescues a vague borrower name
matched, _ = a.pick_company(
    Company(borrower_name="NTT Global Data Center", aliases=[NTT[0]]), NTT
)
check(matched == NTT[0], "alias should disambiguate a vague borrower name")

# Agency search endpoints match literally, so a suffix or '&' written
# differently from the agency's own spelling finds nothing at all. These
# variants are generated so the query has a second chance - but they must
# never shorten the name enough to reach a sibling SPV.
c = Company(borrower_name="HEMAVATHY POWER AND LIGHT PRIVATE LIMITED")
terms = [t.lower() for t in c.search_terms("CARE")]
check(any("&" in t for t in terms), "an '&' variant must be offered for an 'AND' name")
check(any(t == "hemavathy power and light" for t in terms),
      "a suffix-free variant must be offered")

c2 = Company(borrower_name="CLEAN SOLAR POWER (BANIYANA) PRIVATE LIMITED")
check(all("baniyana" in t.lower() for t in c2.search_terms("CARE")),
      "no search variant may drop the distinguishing site name")

ADANI = ["Adani Power (Jharkhand) Limited", "Adani Power (Mundra) Limited", "Adani Power Limited"]
matched, score = a.pick_company(Company(borrower_name="Adani Power Limited"), ADANI)
check(matched == "Adani Power Limited", "Adani Power resolves against its own SPVs")

print(f"{'OK' if not failures else 'FAILED'} - {checks} checks")
for f in failures:
    print(f"   FAIL: {f}")
sys.exit(1 if failures else 0)
