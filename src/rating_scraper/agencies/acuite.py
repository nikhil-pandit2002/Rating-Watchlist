"""Acuite Ratings & Research.

Two endpoints, both on connect.acuite.in:

  GET  /search/result?query=<term>     -> JSON [{Company_Name, Company_Code}]
  POST /rating-search-data             -> HTML, all rating actions for one entity

The POST is the awkward one. Its CSRF token must travel as an ``X-CSRF-TOKEN``
*header*; supplied as a ``_token`` form field - the usual Laravel convention -
the request still returns 200, but with an unfiltered default payload instead of
an error. Sending the wrong thing therefore looks like "this entity has no
ratings" rather than like a failure, so the header is not optional.

The rating table is grouped by rating action, newest first. A group opens with a
row whose ``<th>`` holds the date and whose first ``<td>`` holds the entity name;
the instrument rows follow until the next such header.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Company, Document, LenderLine, RatingRecord
from ..normalize import (
    classify_instrument,
    detect_unit,
    extract_action,
    parse_amount,
    split_rating,
    squash_space,
)
from .base import AgencyAdapter

log = logging.getLogger(__name__)

HOST = "https://connect.acuite.in"
LANDING = f"{HOST}/liveratings?page=1"
SEARCH = f"{HOST}/search/result"
DATA = f"{HOST}/rating-search-data"

# The listing is a rolling feed of rating actions, so it moves constantly.
INDEX_MAX_AGE = 6 * 3600

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# '5th Jun 26', '13th Mar 25'
_DATE_RE = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s+(\d{2,4})", re.I
)


def _parse_acuite_date(text: str) -> Optional[date]:
    m = _DATE_RE.search(squash_space(text or ""))
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2)[:3].title(), m.group(3)
    year = f"20{year}" if len(year) == 2 else year
    try:
        return datetime.strptime(f"{day} {mon} {year}", "%d %b %Y").date()
    except ValueError:
        return None


class AcuiteAdapter(AgencyAdapter):
    name = "Acuite Ratings"

    def __init__(self, fetcher):
        super().__init__(fetcher)
        self._token: str = ""
        self._rows: dict[str, Optional[BeautifulSoup]] = {}

    # -- session -----------------------------------------------------------
    def _csrf(self) -> str:
        """The landing page's CSRF token, fetched once per adapter."""
        if self._token:
            return self._token
        # Never cached: the token is bound to the session cookie issued with the
        # same response. A cached page pairs an old token with a new cookie, the
        # POST then quietly returns an unfiltered payload, and every entity
        # looks unrated. Once per adapter instance, so the cost is negligible.
        r = self.fetcher.get(LANDING, headers={"User-Agent": _UA}, use_cache=False)
        if r.ok:
            m = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
            if m:
                self._token = m.group(1)
        if not self._token:
            log.warning("Acuite: no CSRF token on the landing page")
        return self._token

    def _headers(self) -> dict:
        return {
            "User-Agent": _UA,
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": self._csrf(),
            "Referer": LANDING,
        }

    # -- discovery ---------------------------------------------------------
    def candidate_names(self, company: Company) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for term in company.search_terms(self.name):
            if len(term) < 3:      # the endpoint ignores shorter queries
                continue
            r = self.fetcher.get(SEARCH, params={"query": term},
                                 headers={"User-Agent": _UA},
                                 max_age=INDEX_MAX_AGE)
            if not r.ok:
                continue
            try:
                payload = r.json()
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list):
                continue
            for item in payload:
                n = squash_space(str((item or {}).get("Company_Name", "")))
                if n and n.lower() not in seen:
                    seen.add(n.lower())
                    names.append(n)
            if names:
                break
        return names

    def _fetch_rows(self, entity: str) -> Optional[BeautifulSoup]:
        """Every rating action for one entity, as parsed HTML.

        Memoised because the shared HTTP cache deliberately never stores POSTs,
        and the pipeline asks for the same entity three times over - search,
        parse and lenders. Without this each borrower would cost three identical
        round-trips of up to a few hundred KB.
        """
        if entity in self._rows:
            return self._rows[entity]
        r = self.fetcher.post(DATA, data={"search_company_name": entity},
                              headers=self._headers())
        soup = (BeautifulSoup(r.text, "html.parser")
                if r.ok and r.content else None)
        self._rows[entity] = soup
        return soup

    def search(self, company: Company) -> list[Document]:
        entity, _ = self.pick_company(company, self.candidate_names(company))
        if not entity:
            return []
        soup = self._fetch_rows(entity)
        if soup is None:
            return []

        docs: list[Document] = []
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            tds = tr.find_all("td")
            if not th or not tds:
                continue
            published = _parse_acuite_date(th.get_text(" ", strip=True))
            name = squash_space(tds[0].get_text(" ", strip=True))
            if not name:
                continue
            url = ""
            a = tr.find("a", href=True)
            if a and "company-details" in a["href"]:
                url = a["href"].replace(" ", "_")
            docs.append(Document(
                agency=self.name,
                company_name=name,
                url=url or LANDING,
                published=published,
                doc_id=url.rsplit("/", 1)[-1] if url else "",
            ))
        return docs

    # -- extraction --------------------------------------------------------
    def parse(self, doc: Document) -> list[RatingRecord]:
        """Instrument rows belonging to this document's rating action.

        One fetch returns every action for the entity, so the table is walked
        and only the block whose date matches this document is kept.
        """
        soup = self._fetch_rows(doc.company_name)
        if soup is None:
            return []

        out: list[RatingRecord] = []
        in_block = False
        for tr in soup.find_all("tr"):
            th = tr.find("th")
            tds = tr.find_all("td")
            if th and tds:
                # A group header: are these the rows we want?
                when = _parse_acuite_date(th.get_text(" ", strip=True))
                in_block = when is not None and when == doc.published
                continue
            if not in_block or len(tds) < 6:
                continue

            cells = [squash_space(td.get_text(" ", strip=True)) for td in tds]
            instrument, term, quantum, rating_raw = cells[1], cells[3], cells[4], cells[5]
            if not instrument and not rating_raw:
                continue

            rating, outlook = split_rating(rating_raw, self.name)
            if not rating:
                continue
            # 'Long-term' / 'Short-term' sits in its own column rather than in
            # the instrument name, so it is folded back in before classifying.
            details = f"{term} {instrument}".strip() if term else instrument
            out.append(RatingRecord(
                agency=self.name,
                rating_date=doc.published,
                instrument_category=classify_instrument(details),
                instrument_details=instrument,
                amount=parse_amount(quantum),
                unit=detect_unit(quantum),
                rating=rating,
                outlook=outlook,
                development=extract_action(rating_raw),
                url=doc.url,
            ))
        return out

    def lenders(self, doc: Document) -> list[LenderLine]:
        """The lender-wise annexure, which is what NaBFID detection reads.

        Not in the search response: the 'Instrument & Lender Details' table
        there is an empty shell, and the populated annexure only appears on the
        per-action detail page. Its header is written with a typographic
        apostrophe that arrives mis-encoded, so the column is located on the
        substring 'lender' rather than an exact caption.
        """
        if not doc.url or "company-details" not in doc.url:
            return []
        r = self.fetcher.get(doc.url, headers={"User-Agent": _UA})
        if not r.ok:
            return []
        soup = BeautifulSoup(r.text, "html.parser")

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            # The annexure writes its header as ordinary <td>, so the header row
            # is found by content rather than by tag.
            headers, header_at = [], -1
            for i, tr in enumerate(rows):
                cells = [squash_space(c.get_text(" ", strip=True)).lower()
                         for c in tr.find_all(["th", "td"])]
                if len(cells) > 2 and any("lender" in c for c in cells):
                    headers, header_at = cells, i
                    break
            if header_at < 0:
                continue
            i_name = next((i for i, h in enumerate(headers) if "lender" in h), None)
            i_amt = next((i for i, h in enumerate(headers) if "quantum" in h), None)
            i_fac = next((i for i, h in enumerate(headers) if "facilit" in h), None)
            if i_name is None or i_amt is None:
                continue
            # The annexure sits inside a wrapper table whose first row flattens
            # every cell into one; only rows shaped like the header are lines.
            width = len(headers)
            lines: list[LenderLine] = []
            for tr in rows[header_at + 1:]:
                tds = tr.find_all(["th", "td"])
                if len(tds) != width:
                    continue
                cells = [squash_space(td.get_text(" ", strip=True)) for td in tds]
                name = cells[i_name]
                if not name or "lender" in name.lower():
                    continue
                lines.append(LenderLine(
                    lender_name=name,
                    facility=cells[i_fac] if i_fac is not None else "",
                    amount=parse_amount(cells[i_amt]),
                    unit=detect_unit(cells[i_amt]) or "Rs. Crore",
                ))
            if lines:
                return lines
        return []
