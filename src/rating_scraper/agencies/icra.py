"""ICRA.

Contract (recovered from the site's own JS - all plain HTTP):
  POST /Rating/GetRatingCompanys              data={Term}
        -> [{"id": "12810", "label": "Adani Ports and Special ..."}]
  POST /Rating/GetAllRatingRational?page=N&type=Search
        data={__RequestVerificationToken, CompanyName, FromDate, ToDate,
              RatingCategoryName}
        -> HTML rows: date | sector | title | lender link | pdf link
  GET  /Rating/GetRationalReportFilePdf?Id={id}      -> rationale PDF
  GET  /Rating/BankFacilities?CompanyId=&CompanyName= -> lender-wise HTML table

ShowRationaleReport returns an SPA shell, so the PDF is the parseable form.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..models import Company, Document, LenderLine, RatingRecord
from ..normalize import (
    classify_instrument,
    detect_unit,
    extract_action,
    parse_amount,
    parse_date,
    split_rating,
    squash_space,
)
from ..pdf_utils import PdfDoc
from .base import AgencyAdapter

log = logging.getLogger(__name__)

BASE = "https://www.icra.in"
_ICRA_RATING_RE = re.compile(r"\[ICRA\]\s*[A-D]{1,3}[+-]?|\[ICRA\]\s*A[1-4][+-]?", re.I)


def _is_amount_cell(cell: str) -> bool:
    """A rated-amount cell: a number, or '-' when there is no previous amount."""
    c = cell.strip()
    return c in {"-", "--", "–"} or bool(re.fullmatch(r"[\d,]+\.?\d*", c))


class IcraAdapter(AgencyAdapter):
    name = "ICRA"

    def __init__(self, fetcher):
        super().__init__(fetcher)
        self._token: Optional[str] = None

    # -- session -----------------------------------------------------------

    def _antiforgery_token(self) -> str:
        """ICRA's search POST requires the page's antiforgery token.

        This must be a live request, not a cached one: the token is only valid
        alongside the session cookie the same response sets. Serving it from
        cache yields a stale token and every search silently returns nothing.
        """
        if self._token:
            return self._token
        r = self.fetcher.get(f"{BASE}/Rating/AllRatingRationales", use_cache=False)
        if r.ok:
            soup = BeautifulSoup(r.text, "lxml")
            el = soup.find("input", {"name": "__RequestVerificationToken"})
            if el and el.get("value"):
                self._token = el["value"]
        return self._token or ""

    @property
    def _hdrs(self) -> dict:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/Rating/AllRatingRationales",
        }

    # -- discovery ---------------------------------------------------------

    def _resolve(self, company: Company) -> Optional[tuple[str, str]]:
        """Resolve our borrower to ICRA's (company_id, company_name)."""
        # Warm the session first: the autocomplete POST needs the cookie that
        # the landing page sets.
        self._antiforgery_token()
        for term in company.search_terms(self.name):
            r = self.fetcher.post(
                f"{BASE}/Rating/GetRatingCompanys", data={"Term": term}, headers=self._hdrs
            )
            if not r.ok:
                continue
            try:
                items = r.json()
            except Exception:
                continue
            if not items:
                continue

            # Labels sometimes carry a '#'-separated suffix; keep the name part.
            labels = {}
            for it in items:
                label = str(it.get("label", "")).split("#")[0].strip()
                cid = str(it.get("id", "")).split("#")[-1].strip()
                if label:
                    labels[label] = cid

            matched, score = self.pick_company(company, list(labels))
            if matched:
                return labels[matched], matched
            log.debug("ICRA: no confident match for %r (best %.0f)", term, score)
        return None

    def candidate_names(self, company: Company) -> list[str]:
        self._antiforgery_token()          # warm the session cookie
        names: set[str] = set()
        for term in company.search_terms(self.name):
            r = self.fetcher.post(
                f"{BASE}/Rating/GetRatingCompanys", data={"Term": term}, headers=self._hdrs
            )
            if not r.ok:
                continue
            try:
                items = r.json()
            except Exception:
                continue
            for it in items or []:
                label = str(it.get("label", "")).split("#")[0].strip()
                if label:
                    names.add(label)
            if names:
                break
        return sorted(names)

    def search(self, company: Company) -> list[Document]:
        resolved = self._resolve(company)
        if not resolved:
            return []
        company_id, icra_name = resolved

        r = self.fetcher.post(
            f"{BASE}/Rating/GetAllRatingRational?page=1&type=Search",
            data={
                "__RequestVerificationToken": self._antiforgery_token(),
                "CompanyName": icra_name,
                "FromDate": "",
                "ToDate": "",
                "RatingCategoryName": "",
            },
            headers=self._hdrs,
        )
        if not r.ok:
            return []

        soup = BeautifulSoup(r.text, "lxml")
        lender_url = (
            f"{BASE}/Rating/BankFacilities?CompanyId={company_id}"
            f"&CompanyName={quote(icra_name)}"
        )

        docs: list[Document] = []
        for row in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if not cells:
                continue
            published = parse_date(cells[0])
            rid = None
            for a in row.find_all("a", href=True):
                m = re.search(r"(?:ShowRationaleReport|GetRationalReportFilePdf)\?Id=(\d+)", a["href"])
                if m:
                    rid = m.group(1)
                    break
            if not rid:
                continue
            docs.append(
                Document(
                    agency=self.name,
                    company_name=icra_name,
                    url=f"{BASE}/Rationale/ShowRationaleReport?Id={rid}",
                    published=published,
                    doc_id=rid,
                    lender_url=lender_url,
                )
            )
        return docs

    # -- lenders -----------------------------------------------------------

    def lenders(self, doc: Document) -> list[LenderLine]:
        if not doc.lender_url:
            return []
        r = self.fetcher.get(doc.lender_url, max_age=6 * 3600)
        if not r.ok:
            return []

        soup = BeautifulSoup(r.text, "lxml")
        out: list[LenderLine] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header = " ".join(c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"]))
            if "lender" not in header.lower():
                continue
            unit = detect_unit(header, default="Rs. Crore")
            for row in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
                if len(cells) < 3:
                    continue
                out.append(
                    LenderLine(
                        lender_name=squash_space(cells[0]),
                        facility=squash_space(cells[1]),
                        amount=parse_amount(cells[2]),
                        unit=unit,
                    )
                )
        return out

    # -- parsing -----------------------------------------------------------

    def parse(self, doc: Document) -> list[RatingRecord]:
        pdf_url = f"{BASE}/Rating/GetRationalReportFilePdf?Id={doc.doc_id}"
        r = self.fetcher.get(pdf_url)
        if not r.ok or b"%PDF" not in r.content[:2048]:
            log.warning("ICRA: no PDF for %s", pdf_url)
            return []

        pdf = PdfDoc(r.content)
        rating_date = doc.published or parse_date(pdf.text[:400])
        unit = detect_unit(pdf.text[:1500], default="Rs. Crore")

        records: list[RatingRecord] = []
        for instrument, amount, rating_action in self._summary_rows(pdf):
            rating, outlook = split_rating(rating_action, self.name)
            action = extract_action(rating_action)
            records.append(
                RatingRecord(
                    borrower_name=doc.company_name,
                    rating_date=rating_date,
                    agency=self.name,
                    instrument_category=classify_instrument(instrument),
                    instrument_details=squash_space(instrument),
                    amount=amount,
                    rating=rating,
                    development=action,
                    outlook=outlook,
                    url=doc.url,
                    unit=unit,
                )
            )
        return records

    @staticmethod
    def _summary_rows(pdf: PdfDoc) -> list[tuple[str, Optional[float], str]]:
        """Rows of ICRA's 'Summary of rating action' table.

        Shape:
            Instrument* | Previous rated amount | Current rated amount |
            Rating action | Financial Sector Regulator

        Instrument names wrap onto a following row that has only column 0
        filled; those continuation rows are merged back.
        """
        for tbl in pdf.tables:
            header = " ".join(c or "" for c in tbl[0]).lower()
            if "instrument" not in header or "rating action" not in header:
                continue

            out: list[list] = []
            for row in tbl[1:]:
                cells = [squash_space(c) for c in row if c and squash_space(c)]
                if not cells:
                    continue
                first = cells[0]
                if first.lower().startswith(("total", "(rs.", "regulator")):
                    continue

                rating = next((c for c in cells if _ICRA_RATING_RE.search(c)), "")
                amounts = [parse_amount(c) for c in cells if re.fullmatch(r"[\d,]+\.?\d*", c)]
                amounts = [a for a in amounts if a is not None]

                if not rating and not amounts:
                    # continuation of the previous instrument's wrapped name
                    if out:
                        out[-1][0] = squash_space(f"{out[-1][0]} {first}")
                    continue

                # 'Previous rated amount' then 'Current rated amount' - take current.
                amount = amounts[-1] if amounts else None
                out.append([first, amount, rating])

            rows = [(i, a, r) for i, a, r in out if i and r]
            if rows:
                return rows
        return IcraAdapter._summary_rows_from_text(pdf.text)

    @staticmethod
    def _summary_rows_from_text(text: str) -> list[tuple[str, Optional[float], str]]:
        """Fallback when pdfplumber finds the grid but not the cell contents.

        The text stream lists the table cell-by-cell in reading order:
            Summary of rating action
            Instrument*
            Previous rated amount
            (Rs. crore)
            Current rated amount
            (Rs. crore)
            Rating action
            Long-term borrowing programme
            -
            25,000.00
            [ICRA]AAA (Stable); assigned
            ...
            Total
        """
        lines = [squash_space(l) for l in text.splitlines()]
        try:
            start = next(
                i for i, l in enumerate(lines)
                if re.search(r"summary of rating action", l, re.I)
            )
        except StopIteration:
            return []
        end = next(
            (i for i, l in enumerate(lines[start + 1:], start + 1)
             if re.match(r"^(total\b|\*?instrument details|rationale\b)", l, re.I)),
            min(start + 80, len(lines)),
        )

        rows: list[tuple[str, Optional[float], str]] = []
        pending: list[str] = []
        for line in (l for l in lines[start + 1:end] if l):
            # Header cells, including the fragments left when 'Previous rated
            # amount' wraps onto its own line - otherwise a stray 'amount'
            # gets glued onto the front of the instrument name.
            # Header cells, including the fragments left when 'Previous rated
            # amount' wraps onto its own line - otherwise a stray 'amount'
            # gets glued onto the front of the instrument name. The rating
            # column is headed 'Rating action' on releases that take an action
            # and 'Rating Outstanding' on those that merely restate.
            if re.match(
                r"^(instrument\*?$|previous rated|current rated|\(rs\.|rating action$"
                r"|rating outstanding$|amount$|amount\)$|rated amount$)",
                line,
                re.I,
            ):
                continue
            if _ICRA_RATING_RE.search(line):
                names = [p for p in pending if not _is_amount_cell(p)]
                amounts = [parse_amount(p) for p in pending if _is_amount_cell(p)]
                amounts = [a for a in amounts if a is not None]
                instrument = squash_space(" ".join(names))
                if instrument:
                    rows.append((instrument, amounts[-1] if amounts else None, line))
                pending = []
                continue
            pending.append(line)
        return rows
