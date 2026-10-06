"""India Ratings & Research (Ind-Ra).

The public site is an Angular shell; a plain GET returns ~6.9 KB of bootstrap
HTML and no data. Everything comes from an open JSON API behind it - no auth,
no captcha - recovered from the site's own bundle:

  GET /home/GetSearch?searchKey={name}
        -> {issuerList:      [{issuerID, name}],
            pressreleaseList:[{pressReleaseID, pressReleaseTitle, urlKey,
                               issuerName, prDate}]}          newest first
  GET /pressReleases/GetPressreleaseData?pressReleaseId={id}&issuerconcat=undefined
        -> {currentRatings: <html table>, issuerID, sector, dateOfCreation, ...}
  GET /pressReleases/GetBankFacilityDataRatingLetter?pressReleaseId={id}
        -> {bankFacilitiesList:[{bankName, instrument, ratedAmount, rating}]}

One search call yields both the company match and its dated press releases, so
discovery is a single request per borrower - the cheapest of the six agencies.

Units: Ind-Ra reports in INR **million** ('Size of Issue (million)'), whereas
CARE, ICRA and Brickwork report in crore. Amounts are converted to crore here
so the Amount / Loan Amount columns stay comparable across agencies. This was
verified against Adani Power, where Ind-Ra's NaBFID term loan of 36,000 million
matches the 3,600 crore that CARE and ICRA independently report.

Endpoints requiring a login (home/GetIssuers, the *_User variants) return 401
and are deliberately not used.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Company, Document, LenderLine, RatingRecord
from ..normalize import (
    classify_instrument,
    extract_action,
    parse_amount,
    parse_date,
    split_rating,
    squash_space,
)
from .base import AgencyAdapter

log = logging.getLogger(__name__)

BASE = "https://www.indiaratings.co.in"

# GetSearch returns the issuer list *and* that issuer's press releases in one
# payload, so caching it indefinitely would hide every rating published after
# the first run - the collection is recurring, and a newly issued rating is
# exactly what it exists to find. Rating documents themselves are immutable once
# published and stay cached forever; only this listing needs to expire.
SEARCH_MAX_AGE = 6 * 3600

# Ind-Ra states amounts in INR million; the rest of the workbook is in crore.
MILLION_TO_CRORE = 0.1
UNIT = "Rs. Crore"


def _to_crore(value: Optional[float], header: str) -> Optional[float]:
    """Convert a rated amount to crore based on the unit named in the header."""
    if value is None:
        return None
    h = (header or "").lower()
    if "crore" in h or " cr" in h:
        return value
    if "billion" in h:
        return value * 100.0
    if "lakh" in h or "lac" in h:
        return value * 0.01
    # Ind-Ra's default, and what every observed header states.
    return value * MILLION_TO_CRORE


class IndiaRatingsAdapter(AgencyAdapter):
    name = "India Ratings"

    @property
    def _hdrs(self) -> dict:
        return {"Accept": "application/json, text/plain, */*", "Referer": f"{BASE}/"}

    # -- discovery ---------------------------------------------------------

    def candidate_names(self, company: Company) -> list[str]:
        names: set[str] = set()
        for term in company.search_terms(self.name):
            r = self.fetcher.get(f"{BASE}/home/GetSearch",
                                 params={"searchKey": term}, headers=self._hdrs,
                                 max_age=SEARCH_MAX_AGE)
            if not r.ok:
                continue
            try:
                payload = r.json()
            except Exception:
                continue
            for i in (payload.get("issuerList") or []) if isinstance(payload, dict) else []:
                n = str(i.get("name", "")).strip()
                if n:
                    names.add(n)
            if names:
                break
        return sorted(names)

    def search(self, company: Company) -> list[Document]:
        """One GetSearch call resolves the issuer and lists its press releases."""
        for term in company.search_terms(self.name):
            r = self.fetcher.get(
                f"{BASE}/home/GetSearch", params={"searchKey": term}, headers=self._hdrs,
                max_age=SEARCH_MAX_AGE,
            )
            if not r.ok:
                continue
            try:
                payload = r.json()
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            issuers = payload.get("issuerList") or []
            releases = payload.get("pressreleaseList") or []
            if not issuers or not releases:
                continue

            names = [str(i.get("name", "")).strip() for i in issuers if i.get("name")]
            matched, score = self.pick_company(company, names)
            if not matched:
                log.debug("India Ratings: no confident match for %r (best %.0f)", term, score)
                continue

            docs: list[Document] = []
            for pr in releases:
                # A search for "Adani Power" also returns the subsidiaries'
                # releases; keep only those belonging to the matched issuer.
                if str(pr.get("issuerName", "")).strip() != matched:
                    continue
                pid = str(pr.get("pressReleaseID") or "").strip()
                if not pid:
                    continue
                url_key = str(pr.get("urlKey") or "").strip()
                docs.append(
                    Document(
                        agency=self.name,
                        company_name=matched,
                        url=self._public_url(url_key, matched, pid),
                        published=parse_date(pr.get("prDate")),
                        doc_id=pid,
                    )
                )
            if docs:
                return docs
        return []

    # How many releases back to look for one with an actual rated-instrument
    # table before giving up and returning the literal newest.
    _LOOKBACK = 6

    def latest(self, company: Company) -> Optional[Document]:
        """The most recent press release that actually carries a rating table.

        India Ratings issues periodic surveillance notices - 'Update on Surat
        Municipal Corporation', 'TrinityRail Global Inc.'s Capital Infusion
        into Touax Texmaco...' - between rating actions. These are frequently
        the newest document for an issuer, and they carry no currentRatings
        table at all (confirming timely debt servicing is not a rating
        action), so picking strictly by date silently returns nothing to
        parse even though a perfectly good rating sits one or two releases
        earlier. Surat's real rating action is from 2026-01-09; the newest
        document, from 2026-04-17, is such an update.
        """
        docs = self.search(company)
        if not docs:
            return None
        dated = sorted((d for d in docs if d.published), key=lambda d: d.published, reverse=True)
        if not dated:
            return docs[0]

        for doc in dated[: self._LOOKBACK]:
            rec = self._press_release(doc)
            if rec and (rec.get("currentRatings") or "").strip():
                return doc
        # Nothing in the lookback window had a table; return the newest so the
        # caller still gets *something* rather than a silent None, even though
        # parse() will most likely come back empty for it.
        return dated[0]

    @staticmethod
    def _public_url(url_key: str, issuer_name: str, pid: str) -> str:
        """The human-facing press release URL, for the 'Online url' column."""
        if not url_key:
            return f"{BASE}/pressrelease/{pid}"
        slug = re.sub(r"[^a-z0-9]", "", issuer_name.lower())
        return f"{BASE}/pressrelease/{url_key}/{slug}"

    # -- parsing -----------------------------------------------------------

    def _press_release(self, doc: Document) -> Optional[dict]:
        r = self.fetcher.get(
            f"{BASE}/pressReleases/GetPressreleaseData",
            params={"pressReleaseId": doc.doc_id, "issuerconcat": "undefined"},
            headers=self._hdrs,
        )
        if not r.ok:
            return None
        try:
            payload = r.json()
        except Exception:
            return None
        if isinstance(payload, list):
            return payload[0] if payload else None
        return payload if isinstance(payload, dict) else None

    def parse(self, doc: Document) -> list[RatingRecord]:
        rec = self._press_release(doc)
        if not rec:
            log.warning("India Ratings: no payload for %s", doc.url)
            return []

        rating_date = doc.published or parse_date(rec.get("dateOfCreation"))

        html = rec.get("currentRatings") or ""
        if not html.strip():
            log.warning("India Ratings: no currentRatings table for %s", doc.url)
            return []

        soup = BeautifulSoup(html, "lxml")
        rows = soup.find_all("tr")
        if not rows:
            return []

        header_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"])]
        header = " | ".join(header_cells)
        idx = self._column_index(header_cells)
        if idx is None:
            log.warning("India Ratings: unrecognised currentRatings header %r", header)
            return []
        i_instr, i_amt, i_rating, i_action = idx

        # The unit is stated in the size column header, e.g. 'Size of Issue (million)'.
        amount_header = header_cells[i_amt] if i_amt < len(header_cells) else ""

        records: list[RatingRecord] = []
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) <= max(i_instr, i_rating):
                continue
            instrument = squash_space(cells[i_instr])
            raw_rating = squash_space(cells[i_rating])
            if not instrument or not raw_rating:
                continue

            amount = None
            if i_amt < len(cells):
                amount = _to_crore(parse_amount(cells[i_amt]), amount_header)

            action_text = cells[i_action] if i_action < len(cells) else ""
            rating, outlook = split_rating(raw_rating, self.name)

            records.append(
                RatingRecord(
                    borrower_name=doc.company_name,
                    rating_date=rating_date,
                    agency=self.name,
                    instrument_category=classify_instrument(instrument),
                    instrument_details=instrument,
                    amount=amount,
                    rating=rating,
                    development=extract_action(action_text) or extract_action(raw_rating),
                    outlook=outlook,
                    url=doc.url,
                    unit=UNIT,
                )
            )
        return records

    @staticmethod
    def _column_index(header_cells: list[str]) -> Optional[tuple[int, int, int, int]]:
        """Locate (instrument, size, rating, action) columns by header text."""
        lowered = [h.lower() for h in header_cells]

        def find(*needles: str) -> Optional[int]:
            for i, h in enumerate(lowered):
                if all(n in h for n in needles):
                    return i
            return None

        i_instr = find("instrument")
        i_amt = find("size")
        i_action = find("rating action")

        # The corporate-issuer table folds outlook into the rating cell:
        # 'Rating assigned along with Outlook/Watch'. Municipal bodies, trusts
        # and InvITs often use a plainer table with the two split into their
        # own columns - 'Rating' | 'Rating Action' - so 'rating'+'outlook'
        # together never matches and the whole table was rejected even though
        # it parses perfectly well; outlook then simply comes out blank via
        # split_rating, which already tolerates that. Bombay Education Trust
        # is exactly this shape.
        i_rating = find("rating", "outlook")
        if i_rating is None:
            i_rating = find("rating assigned")
        if i_rating is None:
            i_rating = next(
                (i for i, h in enumerate(lowered) if "rating" in h and i != i_action), None
            )

        if i_instr is None or i_rating is None:
            return None
        # Size and action are optional; fall back to sane positions.
        return i_instr, i_amt if i_amt is not None else -1, i_rating, (
            i_action if i_action is not None else -1
        )

    # -- lenders -----------------------------------------------------------

    def lenders(self, doc: Document) -> list[LenderLine]:
        r = self.fetcher.get(
            f"{BASE}/pressReleases/GetBankFacilityDataRatingLetter",
            params={"pressReleaseId": doc.doc_id},
            headers=self._hdrs,
            max_age=6 * 3600,
        )
        if not r.ok:
            return []
        try:
            payload = r.json()
        except Exception:
            return []

        rec = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(rec, dict):
            return []

        out: list[LenderLine] = []
        for row in rec.get("bankFacilitiesList") or []:
            name = squash_space(str(row.get("bankName") or ""))
            # 'NA' marks a proposed/unallocated facility with no lender.
            if not name or name.upper() in {"NA", "N/A", "NOT APPLICABLE", "-"}:
                continue
            out.append(
                LenderLine(
                    lender_name=name,
                    facility=squash_space(str(row.get("instrument") or "")),
                    amount=_to_crore(parse_amount(row.get("ratedAmount")), "million"),
                    unit=UNIT,
                )
            )
        return out
