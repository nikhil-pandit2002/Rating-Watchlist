"""Unit tests for the parsing rules - no network required.

    python tests/test_normalize.py
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rating_scraper.normalize import (  # noqa: E402
    classify_instrument,
    extract_action,
    is_nabfid,
    parse_amount,
    parse_date,
    split_rating,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got  {got!r}\n     want {want!r}")


# --- rating / outlook splitting, all six house styles ---------------------
RATING_CASES = [
    ("CARE AAA; Stable", ("CARE AAA", "Stable")),
    ("CARE A1+", ("CARE A1+", "")),
    ("[ICRA]AAA (Stable); Reaffirmed", ("[ICRA]AAA", "Stable")),
    ("[ICRA]A1+ ; Reaffirmed", ("[ICRA]A1+", "")),
    ("CRISIL AAA/Stable (Reaffirmed)", ("CRISIL AAA", "Stable")),
    ("IND AA-/Negative", ("IND AA-", "Negative")),
    ("IVR BBB+/Stable", ("IVR BBB+", "Stable")),
    # trailing '-' is part of the grade and must survive
    ("BWR BBB-/Stable / Reaffirmation", ("BWR BBB-", "Stable")),
    ("BWR BBB-/ Assignment", ("BWR BBB-", "")),
    ("BWRAAA/Stable", ("BWR AAA", "Stable")),
    ("IND A+/Rating Watch with Developing Implications",
     ("IND A+", "Rating Watch with Developing Implications")),
]
for raw, want in RATING_CASES:
    check(f"split_rating({raw!r})", split_rating(raw), want)


# --- rating actions -------------------------------------------------------
ACTION_CASES = [
    ("Reaffirmed", "Reaffirmed"),
    ("Assignment", "Assigned"),
    ("Assigned", "Assigned"),
    ("Upgraded from [ICRA]BB+", "Upgraded"),
    ("Downgraded", "Downgraded"),
    ("Withdrawal", "Withdrawn"),
    ("removed from ISSUER NOT COOPERATING category and Reaffirmed",
     "Removed from Issuer Not Cooperating"),
    ("Continues to remain under ISSUER NOT COOPERATING",
     "Continues under Issuer Not Cooperating"),
]
for raw, want in ACTION_CASES:
    check(f"extract_action({raw!r})", extract_action(raw), want)


# --- NaBFID detection -----------------------------------------------------
NABFID_TRUE = [
    "NaBFID",
    "National Bank for Financing Infrastructure and Development",
    # CARE truncates the name in its lender feed - this must still match
    "National Bank for Financing Infrastructure and Dev",
    "NATIONAL BANK FOR FINANCING INFRASTRUCTURE",
]
NABFID_FALSE = [
    "National Bank for Agriculture and Rural Development",
    "NABARD",
    "State Bank of India",
    "National Housing Bank",
    "",
]
for n in NABFID_TRUE:
    check(f"is_nabfid({n!r})", is_nabfid(n), True)
for n in NABFID_FALSE:
    check(f"is_nabfid({n!r})", is_nabfid(n), False)


# --- amounts --------------------------------------------------------------
AMOUNT_CASES = [
    ("45,000.00", 45000.0),
    ("45,000.00 (Enhanced from 25,000.00)", 45000.0),
    ("1,02,712", 102712.0),   # Indian digit grouping
    ("-", None),
    ("Nil", None),
    ("", None),
]
for raw, want in AMOUNT_CASES:
    check(f"parse_amount({raw!r})", parse_amount(raw), want)


# --- dates ----------------------------------------------------------------
DATE_CASES = [
    ("May 29, 2026", date(2026, 5, 29)),
    ("28 Jul 2026", date(2026, 7, 28)),
    ("22 July 2026", date(2026, 7, 22)),
    ("2026-05-29 00:00:00.000", date(2026, 5, 29)),
    ("July 03, 2026", date(2026, 7, 3)),
]
for raw, want in DATE_CASES:
    check(f"parse_date({raw!r})", parse_date(raw), want)


# --- instrument classification -------------------------------------------
CLASS_CASES = [
    ("Long-term bank facilities", "Long Term Bank Facilities"),
    ("Long-term / Short-term bank facilities", "Long Term / Short Term Bank Facilities"),
    ("Commercial Paper", "Commercial Paper"),
    ("Non-convertible debentures", "Debt Instrument"),
    ("Market borrowing programme (FY21)", "Debt Instrument"),
    ("Term Loans (Existing)", "Bank Facilities"),
]
for raw, want in CLASS_CASES:
    check(f"classify_instrument({raw!r})", classify_instrument(raw), want)


if failures:
    print(f"FAILED ({len(failures)}):\n")
    for f in failures:
        print(f"  - {f}\n")
    sys.exit(1)

total = (
    len(RATING_CASES) + len(ACTION_CASES) + len(NABFID_TRUE) + len(NABFID_FALSE)
    + len(AMOUNT_CASES) + len(DATE_CASES) + len(CLASS_CASES)
)
print(f"OK - {total} assertions passed")
