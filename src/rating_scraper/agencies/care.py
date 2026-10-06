"""CARE Ratings (CareEdge).

Endpoints (all plain HTTP, no browser required - verified live):
  GET /rrcompany?companyName=&YearID=&fdate=&tdate=
        -> {"data":[{CompanyID(enc), CompanyName, FileType, FileURL, PublishedDate}]}
  GET /upload/CompanyFiles/PR/{FileURL}          -> press release PDF
  GET /getSearchBankListed?companyName={enc_id}  -> lender-wise bank facilities
  GET /getCompanyClassification?companyName={id} -> sector / industry
  GET /getSearchBreadcrumb?companyName={id}      -> full company master incl. City

Note: the timestamp in FileURL is NOT the rating date. The real date is in the
document body (and in PublishedDate); the filename prefix is an upload stamp.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Optional
from urllib.parse import unquote

from ..models import Company, Document, LenderLine, RatingRecord
from ..normalize import (
    classify_instrument,
    detect_unit,
    extract_action,
    name_score,
    parse_amount,
    parse_date,
    split_rating,
    squash_space,
)
from ..pdf_utils import PdfDoc
from .base import AgencyAdapter

log = logging.getLogger(__name__)

BASE = "https://www.careratings.com"


class CareAdapter(AgencyAdapter):
    name = "CARE Ratings"

    # -- discovery ---------------------------------------------------------

    def search(self, company: Company) -> list[Document]:
        docs: list[Document] = []
        for term in company.search_terms(self.name):
            payload = self._search_raw(term)
            if not payload:
                continue

            names = sorted({row.get("CompanyName", "") for row in payload})
            matched, score = self.pick_company(company, names)
            if not matched:
                log.debug("CARE: no confident match for %r (best %.0f)", term, score)
                continue

            for row in payload:
                if row.get("CompanyName") != matched:
                    continue
                if (row.get("FileType") or "").upper() != "PR":
                    continue
                file_url = row.get("FileURL") or ""
                if not file_url:
                    continue
                docs.append(
                    Document(
                        agency=self.name,
                        company_name=matched,
                        url=f"{BASE}/upload/CompanyFiles/PR/{file_url}",
                        published=parse_date(row.get("PublishedDate")),
                        doc_id=row.get("CompanyID", ""),
                        lender_url=f"{BASE}/getSearchBankListed?companyName={row.get('CompanyID', '')}",
                    )
                )
            if docs:
                break
        return docs

    def candidate_names(self, company: Company) -> list[str]:
        names: set[str] = set()
        for term in company.search_terms(self.name):
            for row in self._search_raw(term):
                n = (row.get("CompanyName") or "").strip()
                if n:
                    names.add(n)
            if names:
                break
        return sorted(names)

    def _search_raw(self, term: str) -> list[dict]:
        r = self.fetcher.get(
            f"{BASE}/rrcompany",
            params={"companyName": term, "YearID": "", "fdate": "", "tdate": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
            max_age=6 * 3600,
        )
        if not r.ok:
            return []
        try:
            return r.json().get("data") or []
        except Exception as e:
            log.warning("CARE: bad JSON for %r: %s", term, e)
            return []

    # -- lenders -----------------------------------------------------------

    def lenders(self, doc: Document) -> list[LenderLine]:
        if not doc.lender_url:
            return []
        r = self.fetcher.get(
            doc.lender_url,
            headers={"X-Requested-With": "XMLHttpRequest"},
            max_age=6 * 3600,
        )
        if not r.ok:
            return []
        try:
            data = r.json().get("data") or []
        except Exception:
            return []

        out: list[LenderLine] = []
        for block in data:
            for row in block.get("getBankListed") or []:
                out.append(
                    LenderLine(
                        lender_name=squash_space(row.get("BankName", "")),
                        facility=squash_space(
                            f"{row.get('Instrument', '')} {row.get('InstrumentName', '')}"
                        ),
                        amount=parse_amount(row.get("RatedAmount")),
                        unit="Rs. Crore",  # CARE publishes this table in ₹ Cr
                    )
                )
        return out

    def location(self, doc: Document) -> str:
        """CARE exposes the registered city on its classification endpoint."""
        if not doc.doc_id:
            return ""
        r = self.fetcher.get(
            f"{BASE}/getSearchBreadcrumb",
            params={"companyName": doc.doc_id},
            headers={"X-Requested-With": "XMLHttpRequest"},
            max_age=24 * 3600,
        )
        if not r.ok:
            return ""
        try:
            for block in r.json().get("data") or []:
                if squash_space(block.get("Company", "")) == squash_space(doc.company_name):
                    for c in block.get("MyCompany") or []:
                        if squash_space(c.get("CompanyName", "")) == squash_space(doc.company_name):
                            return squash_space(c.get("City", ""))
        except Exception:
            pass
        return ""

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _restricted_group_link(pdf: PdfDoc, company_name: str) -> str:
        """The member's own press-release URL from a restricted-group update.

        The roster table gives entity names and the PDF's link annotations give
        the URLs, but the two are not joined in the extracted text. Matching is
        done on the URL's own filename instead, which embeds the entity name -
        '..._ACME_JODHPUR_SOLAR_POWER_PRIVATE_LIMITED.pdf'. Scored rather than
        substring-matched so sibling SPVs that share most of their name
        ('Acme Solar Energy (MP)' vs 'Acme Solar Technologies (Gujarat)')
        cannot pick each other's document.
        """
        best, best_score = "", 0.0
        for url in pdf.links:
            if "/upload/CompanyFiles/PR/" not in url:
                continue
            stem = unquote(url.rsplit("/", 1)[-1])
            stem = re.sub(r"^\d+_", "", stem)          # strip the timestamp prefix
            stem = re.sub(r"\.pdf$", "", stem, flags=re.I).replace("_", " ")
            score = name_score(company_name, stem)
            if score > best_score:
                best, best_score = url, score
        # The roster lists close siblings, so demand a strong match, not merely
        # the least-bad one.
        return best if best_score >= 90.0 else ""

    def parse(self, doc: Document) -> list[RatingRecord]:
        r = self.fetcher.get(doc.url)
        if not r.ok or not r.content:
            log.warning("CARE: could not fetch %s", doc.url)
            return []

        pdf = PdfDoc(r.content)
        text = pdf.text
        rating_date = self._document_date(text) or doc.published

        rows = self._facility_rows(pdf)
        if not rows:
            # A pooled-financing 'Restricted Group' credit update covers many
            # SPVs at once and carries no facility table of its own - just a
            # roster of member entities, each linking to its own press release.
            # CARE's search returns this shared document for every member, so
            # without following the link the whole group parses to nothing.
            redirect = self._restricted_group_link(pdf, doc.company_name)
            if redirect and redirect != doc.url:
                log.info("CARE: %s is a restricted-group update; following link for %s",
                         doc.url, doc.company_name)
                rr = self.fetcher.get(redirect)
                if rr.ok and rr.content:
                    pdf = PdfDoc(rr.content)
                    text = pdf.text
                    rating_date = self._document_date(text) or rating_date
                    rows = self._facility_rows(pdf)
                    if rows:
                        doc = replace(doc, url=redirect)

        if not rows:
            log.warning("CARE: no facility table in %s", doc.url)
            return []

        unit = detect_unit(text, default="Rs. Crore")
        records: list[RatingRecord] = []
        for facility, amount_s, rating_s, action_s in rows:
            rating, outlook = split_rating(rating_s, self.name)
            if not rating and not facility:
                continue
            records.append(
                RatingRecord(
                    borrower_name=doc.company_name,
                    rating_date=rating_date,
                    agency=self.name,
                    instrument_category=classify_instrument(facility),
                    instrument_details=squash_space(facility),
                    amount=parse_amount(amount_s),
                    rating=rating,
                    development=extract_action(action_s) or extract_action(rating_s),
                    outlook=outlook,
                    url=doc.url,
                    unit=unit,
                )
            )
        return records

    @staticmethod
    def _document_date(text: str):
        """CARE puts the rating date on the 3rd line, under the company name."""
        for line in text.splitlines()[:12]:
            line = squash_space(line)
            if re.fullmatch(r"[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}", line):
                return parse_date(line)
        return None

    def _facility_rows(self, pdf: PdfDoc) -> list[tuple[str, str, str, str]]:
        """Extract (facility, amount, rating, action) from the summary table."""
        # Preferred: the real table, when pdfplumber finds its grid.
        #
        # The header is matched on 'amount' rather than on the word 'facilities'.
        # CARE heads the summary table 'Facilities/Instruments' for borrowers
        # with bank lines but plain 'Instruments' for pure debt issuers, and
        # requiring 'facilit' skipped the latter entirely - the parse then fell
        # through to the Annexure ('Name of the Instrument ... Rating Assigned'),
        # which also mentions a facility and a rating but leads with a serial
        # number. Altius Telecom came back as 22 rows carrying amounts of
        # 1, 2, 3, 4 and 5 instead of 5 rows totalling 12,950 crore.
        # The annexure sizes its column 'Size of the Issue', so requiring
        # 'amount' keeps the two apart.
        for tbl in pdf.tables:
            header = " ".join(c or "" for c in tbl[0]).lower()
            names_instruments = "facilit" in header or "instrument" in header
            if names_instruments and "rating" in header and "amount" in header:
                out = []
                for row in tbl[1:]:
                    parsed = _classify_cells([squash_space(c) for c in row])
                    if parsed:
                        out.append(parsed)
                if out:
                    return out
        return self._facility_rows_from_text(pdf.text)

    @staticmethod
    def _facility_rows_from_text(text: str) -> list[tuple[str, str, str, str]]:
        """Fallback for PRs whose summary table has no detectable grid.

        PyMuPDF emits one table cell per line, in reading order:
            Facilities/Instruments
            Amount (₹ crore)
            Rating1
            Rating Action
            Long-term bank facilities
            45,000.00
             (Enhanced from 25,000.00)
            CARE AAA; Stable
            Reaffirmed
        """
        lines = [squash_space(l) for l in text.splitlines()]
        try:
            start = next(
                i for i, l in enumerate(lines)
                if re.search(r"facilities\s*/\s*instruments", l, re.I)
            )
        except StopIteration:
            return []
        end = next(
            (i for i, l in enumerate(lines[start + 1:], start + 1)
             if re.search(r"details of instruments|annexure-1|rationale and key", l, re.I)),
            min(start + 60, len(lines)),
        )

        rows: list[tuple[str, str, str, str]] = []
        facility = amount = rating = ""
        for line in (l for l in lines[start + 1:end] if l):
            if _is_header_cell(line) or _is_continuation(line):
                continue
            if _RATING_RE.search(line):
                rating = line
                continue
            if _is_amount(line):
                amount = line
                continue
            if rating:
                # first plain line after a rating is that row's action
                rows.append((facility, amount, rating, line))
                facility = amount = rating = ""
                continue
            facility = line

        if rating and facility:
            rows.append((facility, amount, rating, ""))
        return [r for r in rows if r[0] and not r[0].lower().startswith("total")]


# --------------------------------------------------------------------------
# Column detection
# --------------------------------------------------------------------------

# pdfplumber pads CARE's tables with a varying number of empty columns, so the
# amount/rating/action land at different indices on different rows. Identify
# each cell by what it contains instead of where it sits.
_RATING_RE = re.compile(r"\bCARE\s*(?:[A-D]{1,3}[+-]?|A[1-4][+-]?)\b", re.I)
_AMOUNT_RE = re.compile(r"^[\d,]+\.?\d*$")
_HEADER_RE = re.compile(
    r"^(facilities\s*/\s*instruments|amount\s*\(|rating\s*\d*$|rating\s+action)", re.I
)


def _is_amount(cell: str) -> bool:
    return bool(_AMOUNT_RE.fullmatch(cell.strip()))


def _is_continuation(cell: str) -> bool:
    """'(Enhanced from 25,000.00)' rows restate the previous amount."""
    return cell.strip().startswith("(")


def _is_header_cell(cell: str) -> bool:
    return bool(_HEADER_RE.match(cell.strip()))


def _classify_cells(cells: list[str]) -> Optional[tuple[str, str, str, str]]:
    """Turn one raw table row into (facility, amount, rating, action)."""
    cells = [c for c in cells if c and c.strip()]
    if not cells:
        return None
    if len(cells) == 1 and _is_continuation(cells[0]):
        return None

    rating = next((c for c in cells if _RATING_RE.search(c)), "")
    amount = next((c for c in cells if _is_amount(c)), "")
    rest = [
        c for c in cells
        if c not in (rating, amount) and not _is_continuation(c) and not _is_header_cell(c)
    ]
    if not rest:
        return None

    facility = rest[0]
    action = rest[1] if len(rest) > 1 else ""
    if facility.lower().startswith("total"):
        return None
    return facility, amount, rating, action
