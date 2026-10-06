"""Brickwork Ratings (BWR).

PressRelease.aspx is classic ASP.NET WebForms: a GET to harvest __VIEWSTATE /
__EVENTVALIDATION, then a POST carrying them back with the search term. The
results table gives date, entity name, rating action and the PDF link.

Caveat: Brickwork's embedded fonts drop inter-word spaces on extraction
('LongtermUnsecuredbonds'), so everything goes through respace().
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin

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
from ..pdf_utils import PdfDoc, respace
from .base import AgencyAdapter

log = logging.getLogger(__name__)

BASE = "https://www.brickworkratings.com"
SEARCH_URL = f"{BASE}/PressRelease.aspx"

_BWR_RATING_RE = re.compile(r"BWR\s*(?:[A-D]{1,3}[+-]?|A[1-4][+-]?)", re.I)
_TOTAL_RE = re.compile(r"^(grand\s+)?total$|^proposed$|^sub[\s-]*total$", re.I)


def _is_total_row(name: str) -> bool:
    """Total / sub-total / proposed footer rows are not lenders."""
    return bool(_TOTAL_RE.match(name.strip()))
_HIDDEN_FIELDS = [
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__VIEWSTATEENCRYPTED",
    "__EVENTVALIDATION",
]


class BrickworkAdapter(AgencyAdapter):
    name = "Brickwork Ratings"

    def search(self, company: Company) -> list[Document]:
        for term in company.search_terms(self.name):
            docs = self._search_once(term, company)
            if docs:
                return docs
        return []

    def candidate_names(self, company: Company) -> list[str]:
        names: set[str] = set()
        for term in company.search_terms(self.name):
            for _published, org, _action, _href in self._raw_results(term):
                if org:
                    names.add(org)
            if names:
                break
        return sorted(names)

    def _raw_results(self, term: str) -> list[tuple]:
        """Run the WebForms search and return its result rows."""
        # 1) GET the form to harvest the WebForms state tokens.
        landing = self.fetcher.get(SEARCH_URL, use_cache=False)
        if not landing.ok:
            return []
        soup = BeautifulSoup(landing.text, "lxml")

        def hidden(name: str) -> str:
            el = soup.find("input", {"name": name})
            return el.get("value", "") if el else ""

        radio = soup.find("input", {"type": "radio", "checked": True})
        radio_value = radio.get("value", "2") if radio else "2"

        data = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
            "__SCROLLPOSITIONX": "0",
            "__SCROLLPOSITIONY": "0",
            "ctl00$ContentPlaceHolder1$rblSearch": radio_value,
            "ctl00$ContentPlaceHolder1$CompanytxtSearch": term,
            "ctl00$ContentPlaceHolder1$btnSearch": "Search",
        }
        for f in _HIDDEN_FIELDS:
            data[f] = hidden(f)

        # 2) POST it back with the search term.
        r = self.fetcher.post(SEARCH_URL, data=data, headers={"Referer": SEARCH_URL})
        if not r.ok or "No results found" in r.text:
            return []
        return self._result_rows(r.text)

    def _search_once(self, term: str, company: Company) -> list[Document]:
        results = self._raw_results(term)
        if not results:
            return []

        names = sorted({name for _, name, _, _ in results})
        matched, score = self.pick_company(company, names)
        if not matched:
            log.debug("Brickwork: no confident match for %r (best %.0f)", term, score)
            return []

        return [
            Document(
                agency=self.name,
                company_name=matched,
                url=urljoin(f"{BASE}/", href),
                published=published,
                doc_id=href,
                action_hint=action,
            )
            for published, name, action, href in results
            if name == matched and href
        ]

    @staticmethod
    def _result_rows(html: str) -> list[tuple[Optional[object], str, str, str]]:
        """(date, organization, rating action, pdf href) from the results table."""
        soup = BeautifulSoup(html, "lxml")
        out = []
        seen = set()
        for row in soup.find_all("tr"):
            cells = [squash_space(c.get_text(" ", strip=True)) for c in row.find_all("td")]
            # The results table repeats a flattened summary row; keep the
            # clean 3-column rows only.
            if len(cells) != 3:
                continue
            date_s, org, action = cells
            if date_s.lower() == "date":
                continue
            published = parse_date(date_s)
            if published is None:
                continue
            href = ""
            for a in row.find_all("a", href=True):
                if ".pdf" in a["href"].lower():
                    href = a["href"]
                    break
            key = (published, org, href)
            if key in seen:
                continue
            seen.add(key)
            out.append((published, org, action, href))
        return out

    # -- parsing -----------------------------------------------------------

    def parse(self, doc: Document) -> list[RatingRecord]:
        r = self.fetcher.get(doc.url)
        if not r.ok or b"%PDF" not in r.content[:2048]:
            log.warning("Brickwork: no PDF at %s", doc.url)
            return []

        pdf = PdfDoc(r.content)
        text = pdf.text
        rating_date = doc.published or parse_date(text[:300])
        unit = detect_unit(text[:1500], default="Rs. Crore")

        records: list[RatingRecord] = []
        for instrument, amount, rating_raw, tenure in self._instrument_rows(pdf):
            rating, outlook = split_rating(rating_raw, self.name)
            # BWR keeps tenure in its own column, so fold it into the category.
            records.append(
                RatingRecord(
                    borrower_name=doc.company_name,
                    rating_date=rating_date,
                    agency=self.name,
                    instrument_category=classify_instrument(f"{tenure} {instrument}".strip()),
                    instrument_details=squash_space(instrument),
                    amount=amount,
                    rating=rating,
                    # The rated-instrument table does not always name the
                    # action; fall back to the listing, then the filename
                    # ('...-NCD-Withdrawal-21Jun2018.pdf').
                    development=(
                        extract_action(rating_raw)
                        or extract_action(doc.action_hint)
                        or extract_action(doc.url.rsplit("/", 1)[-1].replace("-", " "))
                    ),
                    outlook=outlook,
                    url=doc.url,
                    unit=unit,
                )
            )
        return records

    @staticmethod
    def _instrument_rows(pdf: PdfDoc) -> list[tuple[str, Optional[float], str, str]]:
        """Rows of BWR's headline instrument table.

        Shape (two header rows, Previous/Present pairs):
            Facilities/Instruments | Amount(Rs Crs) Prev | Present | Tenure |
            Rating Prev | Rating Present
        The 'Present' column is the current one, so take the last of each pair.
        The header says 'Facilities**' on some releases and 'Instruments@' on
        others, so accept either.
        """
        tenure_re = re.compile(r"^(long|short)[\s-]*term$", re.I)

        for tbl in pdf.tables:
            header = " ".join(c or "" for c in tbl[0]).lower()
            if "rating" not in header:
                continue
            if "instrument" not in header and "facilit" not in header:
                continue

            out: list[tuple[str, Optional[float], str, str]] = []
            for row in tbl[1:]:
                cells = [respace(c) for c in row if c and respace(c)]
                if not cells:
                    continue
                if cells[0].lower().startswith(("total", "previous", "present", "date")):
                    continue

                ratings = [c for c in cells if _BWR_RATING_RE.search(c)]
                if not ratings:
                    continue
                amounts = [
                    parse_amount(c) for c in cells
                    if re.fullmatch(r"[\d,]+\.?\d*", c.strip())
                ]
                amounts = [a for a in amounts if a is not None]
                tenure = next((c for c in cells if tenure_re.match(c.strip())), "")

                names = [
                    c for c in cells
                    if c not in ratings
                    and not re.fullmatch(r"[\d,]+\.?\d*", c.strip())
                    and not tenure_re.match(c.strip())
                ]
                instrument = squash_space(names[0]) if names else ""
                if not instrument:
                    continue
                out.append((instrument, amounts[-1] if amounts else None, ratings[-1], tenure))
            if out:
                return out
        return []

    # -- lenders -----------------------------------------------------------

    def lenders(self, doc: Document) -> list[LenderLine]:
        """BWR has no lender endpoint; scan the PDF for a lender annexure.

        Columns are located by header text and read positionally. Both matter:
        BWR's annexure leads with a 'Sr. No.' column, and it carries several
        numeric columns - sanctioned, availed, outstanding, and ROI %. Taking
        cells[0] yields the serial number instead of the bank, and taking the
        last numeric cell yields the interest rate instead of the loan.
        """
        r = self.fetcher.get(doc.url)
        if not r.ok or b"%PDF" not in r.content[:2048]:
            return []

        pdf = PdfDoc(r.content)
        out: list[LenderLine] = []
        for tbl in pdf.tables:
            if not tbl:
                continue
            header_cells = [respace(c or "") for c in tbl[0]]
            cols = self._lender_columns(header_cells)
            if cols is None:
                continue
            i_name, i_fac, i_amt = cols

            unit_src = header_cells[i_amt] if 0 <= i_amt < len(header_cells) else ""
            unit = detect_unit(unit_src or " ".join(header_cells), default="Rs. Crore")

            for row in tbl[1:]:
                # Positions preserved - do NOT drop empty cells, they align
                # the row with the header.
                cells = [respace(c or "") for c in row]
                if i_name >= len(cells):
                    continue
                name = cells[i_name].strip()
                if not name or _is_total_row(name):
                    continue
                # A bare serial number in the name column means the mapping
                # slipped; skip rather than emit a bogus lender.
                if re.fullmatch(r"\d+\.?", name):
                    continue

                amount = None
                if 0 <= i_amt < len(cells):
                    amount = parse_amount(cells[i_amt])

                out.append(
                    LenderLine(
                        lender_name=name,
                        facility=cells[i_fac].strip() if 0 <= i_fac < len(cells) else "",
                        amount=amount,
                        unit=unit,
                    )
                )
        return out

    @staticmethod
    def _lender_columns(header: list[str]) -> Optional[tuple[int, int, int]]:
        """Locate (lender name, facility, amount) columns in a lender annexure."""
        low = [h.lower() for h in header]

        def find(*patterns: str) -> int:
            for i, h in enumerate(low):
                if any(re.search(p, h) for p in patterns):
                    return i
            return -1

        i_name = find(r"name of (the )?(bank|lender|financial)", r"\blender\b", r"\bbank\b")
        if i_name < 0:
            return None
        i_fac = find(r"type of (loan|facilit)", r"facilit", r"instrument", r"nature of")
        # Prefer the sanctioned/rated amount: it is what the other agencies
        # report, and it is not the outstanding balance or the interest rate.
        i_amt = find(r"sanction")
        if i_amt < 0:
            i_amt = find(r"rated amount", r"\bamount\b", r"limit")
        return i_name, i_fac, i_amt
