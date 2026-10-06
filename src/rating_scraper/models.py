"""Core data structures shared by every agency adapter."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

# The exact output column order requested, in one place so the exporter,
# the QA sheet and any downstream consumer can never drift apart.
OUTPUT_COLUMNS = [
    "Borrower Name",
    "CIN",
    "Open Charges",
    "Rating Date",
    "Rating Agency Name",
    "Instrument Category",
    "Instrument Details",
    "Amount",
    "Rating",
    "Development",
    "Outlook",
    "Online url",
    "NaBFID Name",
    "Loan Amount",
    "Unit",
]


@dataclass
class Company:
    """One row of the user-supplied input sheet."""

    borrower_name: str
    cin: str = ""
    pan: str = ""
    aliases: list[str] = field(default_factory=list)
    # agency name -> the exact entity name confirmed for this CIN. A pin is
    # authoritative: it is how the CIN takes preference over name matching.
    # Agencies publish no CIN anywhere, so the link is established once (by
    # confirming an ambiguous match) and then reused for good.
    pinned: dict[str, str] = field(default_factory=dict)
    # Set by the pipeline under strict mode: allow truncated search terms, which
    # are only safe when every match is confirmed by a person. See _prefix_terms.
    broad_search: bool = False
    # agency name -> an exact rating-document URL supplied by a person, for
    # entities the agency hosts but does not expose through its own search.
    # Overrides discovery entirely for that agency.
    pinned_urls: dict[str, str] = field(default_factory=dict)

    def pin_for(self, agency: str) -> str:
        return (self.pinned or {}).get(agency, "")

    def search_terms(self, agency: str = "") -> list[str]:
        """Names to try against an agency search, most specific first.

        A pinned name for this agency goes first: it is the confirmed spelling
        and should be what we actually query.

        Broader fallbacks follow the exact names. Agency search endpoints match
        the query literally, so a registry name that differs from the agency's
        own spelling in any single character finds nothing at all: CARE lists
        'Hemavathy Power & Light Private Limited' while the registry says
        'HEMAVATHY POWER AND LIGHT PRIVATE LIMITED', and searching the latter
        returns zero rows even though the company is rated. Adapters stop at the
        first term that returns candidates, so these cost nothing for names that
        already resolve - and whatever they do turn up still has to clear the
        normal matching threshold before it is accepted.
        """
        terms = []
        if agency:
            pin = self.pin_for(agency)
            if pin:
                terms.append(pin)
        exact = [self.borrower_name, *self.aliases]
        terms += exact

        for name in exact:
            terms.extend(self._fallback_terms(name))

        # Truncations are held back until last and only under broad_search,
        # which the pipeline sets in strict mode. See _prefix_terms().
        if self.broad_search:
            for name in exact:
                terms.extend(self._prefix_terms(name))

        seen, out = set(), []
        for t in terms:
            t = (t or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    @staticmethod
    def _prefix_terms(name: str) -> list[str]:
        """Progressively shorter prefixes, for strict mode only.

        A literal search finds nothing when the agency's own spelling differs by
        one character, and they do misspell their own entities: India Ratings
        indexes 'MSIDC NASHIK REGION ROAD DEVELOPMENT CORPORTION LIMITED', so
        the correctly spelled full name returns zero rows and the entity is
        invisible. 'MSIDC Nashik' finds it.

        Truncation was removed from the general fallbacks because a term loose
        enough to rescue a misspelling also drags in the neighbours: 'Clean
        Solar' pulls five sibling SPVs. That objection is specific to matching
        without a human - the harm was a silent wrong match, or an ambiguity
        raised for a company that has no rating at all.

        Under strict mode neither can happen. Nothing is accepted without a
        confirmation, so extra candidates cost a moment of review once, while a
        query that finds nothing costs the rating entirely.
        """
        words = [w for w in (name or "").split() if w]
        # Drop the legal suffix first - it carries no search value and is the
        # most common source of spelling divergence.
        while words and re.fullmatch(
            r"(private|pvt\.?|limited|ltd\.?|llp|corporation|corportion|corp\.?)",
            words[-1], re.I,
        ):
            words.pop()
        out = []
        for n in range(len(words) - 1, 1, -1):
            out.append(" ".join(words[:n]))
        return out[:4]

    @staticmethod
    def _fallback_terms(name: str) -> list[str]:
        """Looser queries to try when the exact name matches nothing.

        Only punctuation-level variants, never a truncation. Shortening the
        name to its first couple of words was tried and removed: 'Clean Solar
        Power (Baniyana)' becomes 'Clean Solar', which matches five sibling
        SPVs (Bhainsada, Dhar, Gulbarga, Jodhpur, Tumkur) at 97 each and turns
        a correct 'not rated' into an ambiguity the user has to adjudicate -
        for a company that genuinely has no rating. A search term loose enough
        to rescue a misspelling is also loose enough to drag in the neighbours.
        """
        if not name:
            return []
        out = []

        # '&' and 'and' are used interchangeably between the registry and the
        # agencies, and a literal search treats them as different strings:
        # CARE lists 'Hemavathy Power & Light Private Limited', the registry
        # says 'HEMAVATHY POWER AND LIGHT PRIVATE LIMITED', and the exact
        # search returns nothing despite the company being rated CARE A.
        if re.search(r"\band\b", name, re.I):
            out.append(re.sub(r"\band\b", "&", name, flags=re.I))
        elif "&" in name:
            out.append(re.sub(r"\s*&\s*", " and ", name))

        # The same literal-match problem applies to the legal suffix: the
        # registry says 'PRIVATE LIMITED' where an agency may store 'Pvt. Ltd.'
        # (or the reverse), and searching one form returns nothing for a company
        # filed under the other. Dropping the suffix entirely matches both, and
        # unlike truncating the name it keeps every distinctive word, so it
        # cannot pull in sibling SPVs.
        without_suffix = re.sub(
            r"\b(private\s+limited|pvt\.?\s*ltd\.?|pvt\.?\s+limited|limited|ltd\.?|llp)\b\.?\s*$",
            "", name, flags=re.I,
        ).strip(" .,-")
        if without_suffix and without_suffix.lower() != name.lower():
            out.append(without_suffix)
        return out


@dataclass
class Document:
    """A press release / rationale located on an agency site."""

    agency: str
    company_name: str          # name as the agency spells it
    url: str                   # canonical public URL (goes in "Online url")
    published: Optional[date]
    doc_id: str = ""           # agency-native id (encrypted or numeric)
    kind: str = "PR"
    lender_url: str = ""       # lender-wise endpoint, when separate
    # Rating action as stated in the agency's own results listing. Used as a
    # fallback when the document body does not spell the action out - a BWR
    # withdrawal notice, for instance, says so in the listing but not always
    # inside the rated-instrument table.
    action_hint: str = ""


@dataclass
class LenderLine:
    """One row of an agency's lender-wise bank facility table."""

    lender_name: str
    facility: str
    amount: Optional[float]
    unit: str = "Rs. Crore"


@dataclass
class RatingRecord:
    """One instrument line from one press release: becomes one Excel row."""

    borrower_name: str = ""
    cin: str = ""
    pan: str = ""
    location: str = ""
    open_charges: str = ""
    rating_date: Optional[date] = None
    agency: str = ""
    instrument_category: str = ""
    instrument_details: str = ""
    amount: Optional[float] = None
    rating: str = ""
    development: str = ""
    outlook: str = ""
    url: str = ""
    nabfid_name: str = ""
    loan_amount: Optional[float] = None
    unit: str = ""

    # not exported - used for the QA sheet and debugging
    confidence: str = "ok"
    note: str = ""

    def to_row(self) -> dict:
        return {
            "Borrower Name": self.borrower_name,
            "CIN": self.cin,
            "Open Charges": self.open_charges,
            "Rating Date": self.rating_date,
            "Rating Agency Name": self.agency,
            "Instrument Category": self.instrument_category,
            "Instrument Details": self.instrument_details,
            "Amount": self.amount,
            "Rating": self.rating,
            "Development": self.development,
            "Outlook": self.outlook,
            "Online url": self.url,
            # The output column is a flag, not the entity name: self.nabfid_name
            # (the full canonical name) is kept on the record for internal use
            # - counts, highlighting, QA - and only rendered as Yes/No here.
            "NaBFID Name": "Yes" if self.nabfid_name else "No",
            "Loan Amount": self.loan_amount,
            "Unit": self.unit,
        }

    def as_dict(self) -> dict:
        return asdict(self)
