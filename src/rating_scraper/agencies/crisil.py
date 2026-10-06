"""CRISIL Ratings.

The ratings list is an Adobe AEM component with a plain JSON backing endpoint.
It supports server-side filtering, so unlike Infomerics there is no index to
download - one request resolves one borrower:

  GET .../ratingresultlisting.results.json
        ?cmd=CR&start=0&limit=50&filters={"company_name":"Adani Power Limited"}
    -> {"numFound": 4,
        "docs": "{\"CODE\":[{companyName, companyCode, instrumentName, rating,
                            outlook, industryName, ratingFileBasePath,
                            prDocument}]}"}          # docs is a JSON *string*

  GET .../ratingresultlisting.suggest.CR.company_name.json
    -> [{"value": "20 Microns Limited"}, ...]        # 18,496 names, for matching

The rationale itself is HTML rather than PDF, and carries the lender annexure
inline ('Facility | Amount (Rs.Crore) | Name of Lender | Rating'), so this is
the only agency needing neither PDF extraction nor a separate lender request.

Access note: robots.txt permits these paths for a generic user agent - the '*'
group only disallows /content/dam/crisil/... asset folders. CRISIL does ban a
list of *named* crawlers (Scrapy, GPTBot, CCBot and similar), so this adapter
neither impersonates a search engine nor bypasses anything: the reCAPTCHA on the
site belongs to other forms and is not involved in these endpoints. Requests go
through the shared throttle like every other agency.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..models import Company, Document, LenderLine, RatingRecord
from ..normalize import (
    canonical_name,
    classify_instrument,
    detect_unit,
    extract_action,
    parse_amount,
    parse_date,
    split_rating,
    squash_space,
)
from .base import AgencyAdapter

log = logging.getLogger(__name__)

HOST = "https://www.crisilratings.com"
LIST = (
    HOST + "/content/crisilratings/en/home/our-business/ratings/credit-ratings-list"
    "/_jcr_content/wrapper_100_par/columncontrol_copy/container-100-1/ratingresultlisting"
)
NAME_INDEX_MAX_AGE = 7 * 24 * 3600

# 'Rs.10000 Crore Non Convertible Debentures Crisil AAA/Stable (Assigned)'
_SUMMARY_RE = re.compile(
    r"Rs\.?\s*([\d,]+(?:\.\d+)?)\s*Crore\s*"
    r"([A-Za-z][A-Za-z ()/&.,-]{2,60}?)\s*"
    r"(CRISIL\s*(?:AAA|AA|A|BBB|BB|B|C|D)[+-]?(?:\s*/\s*(?:Stable|Positive|Negative|Developing))?"
    r"|CRISIL\s*A[1-4][+-]?)"
    r"\s*\(([^)]{3,60})\)",
    re.I,
)


class CrisilAdapter(AgencyAdapter):
    name = "CRISIL Ratings"

    def __init__(self, fetcher):
        super().__init__(fetcher)
        self._names: Optional[list[str]] = None

    @property
    def _hdrs(self) -> dict:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{HOST}/en/home/our-business/ratings/credit-ratings-list.html",
        }

    # -- discovery ---------------------------------------------------------

    def _company_names(self) -> list[str]:
        """CRISIL's own spelling of every rated company (~18,500)."""
        if self._names is not None:
            return self._names
        r = self.fetcher.get(f"{LIST}.suggest.CR.company_name.json",
                             max_age=NAME_INDEX_MAX_AGE, headers=self._hdrs)
        names: list[str] = []
        if r.ok:
            try:
                for item in r.json():
                    v = squash_space(item.get("value") if isinstance(item, dict) else item)
                    if v:
                        names.append(v)
            except Exception as e:
                log.warning("CRISIL: name index unreadable: %s", e)
        log.info("CRISIL: %d company names in index", len(names))
        self._names = names
        return names

    def candidate_names(self, company: Company) -> list[str]:
        # CRISIL's index is the full 18,500-name list, so narrow it to names
        # sharing a word with the borrower before ranking.
        names = self._company_names()
        terms = {w for t in company.search_terms(self.name)
                 for w in canonical_name(t).split() if len(w) > 2}
        if not terms:
            return []
        return [n for n in names if terms & set(canonical_name(n).split())]

    def _rows_for(self, company_name: str) -> list[dict]:
        """Rated-instrument rows for one exact CRISIL company name."""
        r = self.fetcher.get(
            f"{LIST}.results.json",
            params={
                "cmd": "CR",
                "start": 0,
                "limit": 200,
                # Must be the scalar form; the list form silently returns nothing.
                "filters": json.dumps({"company_name": company_name}),
            },
            headers=self._hdrs,
            max_age=6 * 3600,
        )
        if not r.ok:
            return []
        try:
            payload = r.json()
        except Exception:
            return []

        docs = payload.get("docs")
        if isinstance(docs, str):          # docs arrives as an encoded string
            try:
                docs = json.loads(docs)
            except Exception:
                return []
        if not isinstance(docs, dict):
            return []
        return [row for group in docs.values() for row in group if isinstance(row, dict)]

    DOC_BASE = "/mnt/winshare/Ratings/RatingList/RatingDocs/"

    def _rationale_rows(self, term: str, limit: int = 20) -> list[dict]:
        """Rating rationales for a name, from the RR dataset.

        A second, larger dataset than the credit-ratings list this adapter
        started with. 'cmd=CR' is the list of currently rated instruments and it
        simply does not contain some rated entities: Citius Transnet, IRB
        Infrastructure Trust, Mumbai Urja Marg, Nashik Municipal Corporation,
        Pimpri Chinchwad Municipal Corporation and Nxt-Infra Trust all return
        zero rows there, and none appear in the 18,496-name suggest index either,
        while their rationales are published and current.

        'cmd=RR' has every one of them. It also answers to a partial name and is
        case-insensitive, so it finds entities whose spelling differs from ours -
        which is why what it returns is still put through pick_company rather
        than trusted.
        """
        r = self.fetcher.get(
            f"{LIST}.results.json",
            params={
                "cmd": "RR",
                "start": 0,
                "limit": limit,
                "filters": json.dumps({"company_name": term}),
            },
            headers=self._hdrs,
            max_age=6 * 3600,
        )
        if not r.ok:
            return []
        try:
            docs = r.json().get("docs")
        except Exception:
            return []
        # Unlike cmd=CR, this one returns a plain list of rows.
        return [d for d in docs if isinstance(d, dict)] if isinstance(docs, list) else []

    def _rationale_documents(self, company: Company) -> list[Document]:
        rows: dict[str, dict] = {}
        for term in company.search_terms(self.name):
            for row in self._rationale_rows(term):
                fn = squash_space(str(row.get("ratingFileName") or ""))
                if fn:
                    rows.setdefault(fn, row)
            if rows:
                break
        if not rows:
            return []

        # The endpoint matches loosely, so the entity still has to be confirmed
        # as ours before any of its documents are used.
        by_name: dict[str, list[dict]] = {}
        for row in rows.values():
            by_name.setdefault(squash_space(str(row.get("companyName") or "")), []).append(row)
        matched, _ = self.pick_company(company, [n for n in by_name if n])
        if not matched:
            return []

        out = []
        for row in by_name.get(matched, []):
            fn = squash_space(str(row.get("ratingFileName") or ""))
            out.append(Document(
                agency=self.name,
                company_name=matched,
                url=HOST + self.DOC_BASE + quote(fn),
                published=parse_date(squash_space(str(row.get("ratingDate") or ""))),
                doc_id=fn,
            ))
        return out

    def search(self, company: Company) -> list[Document]:
        names = self._company_names()
        matched, score = (None, 0.0)
        if names:
            matched, score = self.pick_company(company, names)
        if not matched:
            log.debug("CRISIL: not in the credit-ratings index for %s (best %.0f); "
                      "trying rating rationales", company.borrower_name, score)
            return self._rationale_documents(company)

        rows = self._rows_for(matched)
        if not rows:
            return self._rationale_documents(company)

        # Several instruments share one press release; emit one Document each.
        seen: dict[str, Document] = {}
        for row in rows:
            pr = str(row.get("prDocument") or "").strip()
            if not pr or pr in seen:
                continue
            base = str(row.get("ratingFileBasePath") or "/mnt/winshare/Ratings/RatingList/RatingDocs/")
            seen[pr] = Document(
                agency=self.name,
                company_name=squash_space(str(row.get("companyName") or matched)),
                url=HOST + base + quote(pr),
                published=self._date_from_name(pr),
                doc_id=pr,
            )
        return list(seen.values())

    @staticmethod
    def _date_from_name(pr_document: str) -> Optional[object]:
        """'AdaniPowerLimited_March 30_ 2026_RR_382417.html' -> 2026-03-30.

        Only a provisional date for ordering; parse() re-reads it from the
        document body, which is authoritative.
        """
        m = re.search(r"_([A-Za-z]+)\s+(\d{1,2})_\s*(\d{4})_", pr_document)
        return parse_date(f"{m.group(1)} {m.group(2)}, {m.group(3)}") if m else None

    # How far back to look when the newest document yields no rating.
    _LOOKBACK = 4

    def latest(self, company: Company) -> Optional[Document]:
        """The newest document that actually carries a rating.

        CRISIL publishes Credit Bulletins ('X:Update on X') between rating
        actions, and one is often the newest document an entity has. They are
        not empty - two of the three on this watchlist quote the rating in
        prose - but they carry no rating-action table, so whether one parses
        cannot be decided from its heading. Skipping them by title was tried
        and was wrong: it discarded documents that do carry ratings.

        So nothing is skipped on its title. The newest document is used when it
        yields rows, and otherwise the next one back is tried, which is what
        happens for Andhra Pradesh Capital Region Development Authority - its
        only document is a bulletin with no rating anywhere in it.
        """
        docs = self.search(company)
        if not docs:
            return None
        dated = sorted((d for d in docs if d.published),
                       key=lambda d: d.published, reverse=True)
        if not dated:
            return docs[0]

        for doc in dated[: self._LOOKBACK]:
            try:
                if self.parse(doc):
                    return doc
            except Exception as e:  # noqa: BLE001
                log.debug("CRISIL: %s did not parse (%s); trying the previous one",
                          doc.url, e)
        return dated[0]

    # -- parsing -----------------------------------------------------------

    def _soup(self, doc: Document) -> Optional[BeautifulSoup]:
        r = self.fetcher.get(doc.url, headers={"Referer": f"{HOST}/"})
        if not r.ok or len(r.content) < 500:
            log.warning("CRISIL: could not fetch %s", doc.url)
            return None
        return BeautifulSoup(r.text, "lxml")

    def parse(self, doc: Document) -> list[RatingRecord]:
        soup = self._soup(doc)
        text = soup.get_text("\n", strip=True) if soup else ""

        rating_date = self._document_date(text) or doc.published
        unit = detect_unit(text[:3000], default="Rs. Crore") if text else "Rs. Crore"

        # The listing JSON is the reliable backbone: it names every rated
        # instrument with its rating and outlook already separated. The
        # rationale HTML adds the amount and the rating action, which the JSON
        # does not carry - but its wording varies, so it enriches rather than
        # drives. Parsing HTML alone yielded one row where there were four.
        enrich = self._html_details(text)
        # The Rating Action block states one total for ALL bank facilities
        # ('Total Bank Loan Facilities Rated Rs.58000 Crore') and never a figure
        # per facility. The lender annexure does carry them, and its 'Facility'
        # column uses the same names as the listing JSON, so summing it by
        # facility recovers the per-instrument amount from CRISIL's own numbers.
        by_facility = self._facility_amounts(soup) if soup else {}
        # Keyed on the rating grade alone: the annexure writes 'Crisil
        # AAA/Stable' while the listing splits the outlook into its own field,
        # so the two only line up once both are reduced the same way.
        by_facility_rating: dict[tuple[str, str], float] = {}
        for fac, rat, amt in (self._annexure_rating_rows(soup) if soup else []):
            if amt is None:
                continue
            grade, _ = split_rating(re.sub(r"\([^)]*\)", "", rat), self.name)
            if grade:
                by_facility_rating[(fac.lower(), grade.lower())] = amt
        default_action = self._tenure_action(text)
        rows = self._rows_for(doc.company_name)

        records: list[RatingRecord] = []
        for row in rows:
            instrument = squash_space(str(row.get("instrumentName") or ""))
            if not instrument:
                continue
            rating = squash_space(str(row.get("rating") or ""))
            outlook = squash_space(str(row.get("outlook") or ""))
            amount, action, _html_rating = enrich.get(instrument.lower(), (None, "", ""))
            if amount is None:
                # Prefer the amount for this facility *at this rating*. One
                # facility can be split across two ratings - Cube Highways'
                # bank guarantees are 100 crore at Crisil A1+ and 150 at Crisil
                # AAA/Stable - and a facility-name total gives both rows 250,
                # overstating each and doubling the borrower's book.
                amount = by_facility_rating.get((instrument.lower(), rating.lower()))
            if amount is None:
                amount = by_facility.get(instrument.lower())
            # Bank facilities are not named individually in the Rating Action
            # block - it states one action against 'Long Term Rating' covering
            # them all - so fall back to that rather than leave Development blank.
            action = action or default_action
            records.append(
                RatingRecord(
                    borrower_name=doc.company_name,
                    rating_date=rating_date,
                    agency=self.name,
                    instrument_category=classify_instrument(instrument),
                    instrument_details=instrument,
                    amount=amount,
                    rating=rating,
                    development=action,
                    outlook=outlook,
                    url=doc.url,
                    unit=unit,
                )
            )

        if not records:
            # No JSON rows (rare): fall back to whatever the HTML yields.
            for instrument, (amount, action, rating_raw) in enrich.items():
                rating, outlook = split_rating(rating_raw, self.name)
                records.append(
                    RatingRecord(
                        borrower_name=doc.company_name,
                        rating_date=rating_date,
                        agency=self.name,
                        instrument_category=classify_instrument(instrument),
                        instrument_details=instrument.title(),
                        amount=amount,
                        rating=rating,
                        outlook=outlook,
                        development=action,
                        url=doc.url,
                        unit=unit,
                    )
                )
        # Fold in any rating the document's own Rating Action block carries that
        # the instrument list did not. Matched on the rating value, so a line
        # already reported is never duplicated - only a genuinely absent one,
        # such as the long-term rating CRISIL's list omits for some borrowers,
        # is added.
        action_rows, action_total = self._rating_action_rows(soup)
        withdrawals = self._withdrawal_rows(soup) if not action_rows and soup else []
        # Last resort, and the one that makes Credit Bulletins readable: the
        # annexure lists every facility with its own rating and amount.
        if not records and not action_rows and not withdrawals and soup:
            for facility, raw, amount in self._annexure_rating_rows(soup):
                rating, outlook = split_rating(re.sub(r"\([^)]*\)", "", raw), self.name)
                if not rating:
                    continue
                records.append(
                    RatingRecord(
                        borrower_name=doc.company_name,
                        rating_date=rating_date,
                        agency=self.name,
                        instrument_category=classify_instrument(facility),
                        instrument_details=squash_space(facility),
                        amount=amount,
                        rating=rating,
                        development=extract_action(raw),
                        outlook=outlook,
                        url=doc.url,
                        unit=unit,
                    )
                )
        if withdrawals:
            action_rows = [(label, rating) for label, rating, _ in withdrawals]
            action_total = next((amt for _, _, amt in withdrawals if amt is not None), None)
        if action_rows:
            have = {squash_space(r.rating).lower() for r in records if r.rating}
            first = True
            for label, raw in action_rows:
                # The action is parenthesised after the rating - 'Crisil
                # AAA/Stable ( Converted from Provisional Rating to Final
                # Rating )'. Left in place it is read as part of the rating
                # itself; extract_action still gets the original text.
                rating, outlook = split_rating(re.sub(r"\([^)]*\)", "", raw), self.name)
                if not rating:
                    # 'Withdrawn' and 'Suspended' are outcomes, not grades, so
                    # split_rating returns nothing for them. They are still the
                    # agency's answer and are reported as such - the other
                    # adapters already do, and dropping them makes a withdrawn
                    # entity indistinguishable from an unrated one.
                    bare = squash_space(re.sub(r"\([^)]*\)", "", raw))
                    if re.match(r"^(withdrawn|suspended)\b", bare, re.I):
                        rating = bare
                if not rating or rating.lower() in have:
                    continue
                have.add(rating.lower())
                records.append(
                    RatingRecord(
                        borrower_name=doc.company_name,
                        rating_date=rating_date,
                        agency=self.name,
                        instrument_category=classify_instrument(label),
                        instrument_details=squash_space(label),
                        # 'Total Bank Loan Facilities Rated' is one pooled figure
                        # covering every facility, so it is carried on the first
                        # line only rather than repeated onto each.
                        amount=action_total if first else None,
                        rating=rating,
                        development=extract_action(raw),
                        outlook=outlook,
                        url=doc.url,
                        unit=unit,
                    )
                )
                first = False

        if not records:
            log.warning("CRISIL: no instrument rows for %s", doc.url)
        return records

    def _annexure_rating_rows(
        self, soup: BeautifulSoup
    ) -> list[tuple[str, str, Optional[float]]]:
        """Ratings taken from the lender annexure: [(facility, rating, amount)].

        The route into Credit Bulletins. A bulletin carries no rating-action
        block, so on its heading alone it looks like a document with no rating -
        but it does publish the full annexure, facility by facility, with each
        facility's rating beside it:

            Facility     Amount (Rs.Crore)  Name of Lender        Rating
            Term Loan    350                Axis Bank Limited     Crisil AAA/Stable
            Term Loan    2850               ICICI Bank Limited    Crisil AAA/Stable

        Rows are grouped by facility *and* rating, because one facility can
        carry two - Cube Highways' bank guarantees are split between Crisil A1+
        and Crisil AAA/Stable - and collapsing them would invent a single rating
        that the document does not give. Amounts are summed within each group,
        which is the same arithmetic _facility_amounts already applies.

        This reads a table, never prose. Mining the surrounding narrative for
        rating strings was the obvious alternative and is what turned Nashik
        Municipal Corporation into 55 rows of paragraphs.
        """
        groups: dict[tuple[str, str], Optional[float]] = {}
        order: list[tuple[str, str]] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header = [squash_space(c.get_text(" ", strip=True))
                      for c in rows[0].find_all(["td", "th"])]
            # CRISIL nests the annexure inside a wrapper table, and flattening
            # leaves the wrapper's first row holding every cell of the real one
            # - 14 cells including a verbatim 'Name of Lender'. Matching on that
            # picked the wrapper, whose first column is the entire annexure as
            # one string, and Cube Highways came back as two Bank Guarantee rows
            # of 250 instead of four facilities. A genuine header is short and
            # narrow, so that is what is required.
            if not (2 <= len(header) <= 8) or any(len(h) > 60 for h in header):
                continue
            if not any(re.fullmatch(r"name of lender", h, re.I) for h in header):
                continue

            def col(*pats: str) -> int:
                for i, h in enumerate(header):
                    if any(re.search(p, h, re.I) for p in pats):
                        return i
                return -1

            i_fac, i_amt, i_rat = col(r"facilit", r"instrument"), col(r"amount"), col(r"^rating$")
            if i_fac < 0 or i_rat < 0:
                continue

            for row in rows[1:]:
                cells = [squash_space(c.get_text(" ", strip=True))
                         for c in row.find_all(["td", "th"])]
                if max(i_fac, i_rat) >= len(cells):
                    continue
                facility, rating = cells[i_fac], cells[i_rat]
                if not facility or facility.lower() == "total":
                    continue
                if not self._is_rating_cell(rating):
                    continue
                amount = (parse_amount(cells[i_amt])
                          if 0 <= i_amt < len(cells) else None)
                key = (facility, rating)
                if key not in groups:
                    groups[key] = amount
                    order.append(key)
                elif amount is not None:
                    groups[key] = (groups[key] or 0.0) + amount
            if order:
                break
        return [(fac, rat, groups[(fac, rat)]) for fac, rat in order]

    def _facility_amounts(self, soup: BeautifulSoup) -> dict[str, float]:
        """Rated amount per facility, summed from the lender annexure.

        Includes the unallocated rows: a proposed facility is rated and carries
        an amount, but its lender reads 'Not Applicable' because none is
        committed yet. They are not lenders, so lenders() drops them - but the
        money is real and belongs in the Amount column.
        """
        totals: dict[str, float] = {}
        for line in self._lender_rows(soup, include_unallocated=True):
            facility = (line.facility or "").strip().lower()
            if facility and line.amount is not None:
                totals[facility] = totals.get(facility, 0.0) + line.amount
        return totals

    @staticmethod
    def _tenure_action(text: str) -> str:
        """The action stated against 'Long/Short Term Rating' in the header block.

            Long Term Rating
            Crisil AA/Stable (Reaffirmed)
        """
        m = re.search(
            r"(?:Long|Short)\s+Term\s+Rating\s*\n?\s*CRISIL[^\n(]*\(([^)]{3,40})\)",
            text[:3000], re.I,
        )
        return extract_action(m.group(1)) if m else ""

    # 'Long Term Rating', 'Short Term Rating', 'Corporate Credit Rating'
    _ACTION_LABEL_RE = re.compile(
        r"^\s*(long[- ]term|short[- ]term|corporate\s+c\s*redit|issuer)\b", re.I)
    _ACTION_TOTAL_RE = re.compile(
        r"total\s+bank\s+loan\s+facilities\s+rated", re.I)

    # 'Rs.7605 Crore Bond &' - the amount is inside the instrument cell in the
    # withdrawal layout, where there is no separate total row.
    _INSTRUMENT_AMOUNT_RE = re.compile(
        r"^\s*Rs\.?\s*([\d,]+(?:\.\d+)?)\s*Crore\s*(.*)$", re.I)

    # A cell holding a rating and nothing else. Every row taken from a summary
    # table is checked against this: the rationale wraps its prose sections in
    # tables too, and without the guard 'About the Company Tamil Nadu
    # Electricity Board, set up in July 1957 ...' was accepted as an instrument
    # and Nashik Municipal Corporation grew from 2 rows to 55.
    _RATING_VALUE_RE = re.compile(
        r"^(crisil\s*(aaa|aa|a|bbb|bb|b|c|d)[+-]?(\s*/\s*[a-z]+)?"
        r"|crisil\s*a[1-4][+-]?(\s*/\s*[a-z]+)?"
        r"|withdrawn|suspended|not\s+applicable)\b", re.I)

    @classmethod
    def _is_rating_cell(cls, value: str) -> bool:
        # The action is parenthesised after the rating and can be long -
        # 'Crisil AAA/Stable ( Converted from Provisional Rating to Final
        # Rating )' is 62 characters - so it is removed before the length is
        # judged. What must be short is the rating itself.
        bare = squash_space(re.sub(r"\([^)]*\)", "", value or "")).strip()
        return len(bare) <= 40 and bool(cls._RATING_VALUE_RE.match(bare))

    def _withdrawal_rows(self, soup) -> list[tuple[str, str, Optional[float]]]:
        """The 'Name Of Instrument / Rating Outstanding' layout.

        Used by withdrawal notices, which carry no 'Total Bank Loan Facilities
        Rated' row and put the amount inside the instrument cell:

            Name Of Instrument     Rating Outstanding with Outlook   Regulator
            Rs.7605 Crore Bond &   Withdrawn                         SEBI

        A withdrawal is a real outcome and worth reporting - the alternative is
        an entity that silently looks unrated.
        """
        out: list[tuple[str, str, Optional[float]]] = []
        for table in soup.find_all("table"):
            head = table.get_text(" ", strip=True)
            if "Name Of Instrument" not in head or "Rating Outstanding" not in head:
                continue
            for tr in table.find_all("tr"):
                cells = [squash_space(c.get_text(" ", strip=True))
                         for c in tr.find_all(["th", "td"])]
                if not 2 <= len(cells) <= 3:
                    continue
                instrument, rating = cells[0], cells[1]
                if not instrument or len(instrument) > 90:
                    continue
                if instrument.lower().startswith("name of"):
                    continue
                if not self._is_rating_cell(rating):
                    continue
                m = self._INSTRUMENT_AMOUNT_RE.match(instrument)
                amount = parse_amount(m.group(1)) if m else None
                label = squash_space((m.group(2) if m else instrument).strip(" &"))
                out.append((label or instrument, rating, amount))
            if out:
                return out
        return []

    def _rating_action_rows(self, soup) -> tuple[list[tuple[str, str]], Optional[float]]:
        """The document's own 'Rating Action' summary: [(label, rating)], total.

        This block is what a person reads at the top of the rationale, and it is
        the only place some ratings appear at all. CRISIL's instrument list
        returns a single 'Bank Guarantee / Crisil A3+' line for Bhusawal Waste
        Water Management while the document itself states 'Long Term Rating
        Crisil BBB/Stable (Reaffirmed)' alongside it - so relying on the list
        alone reported a short-term rating for a borrower whose headline rating
        is long-term.

        It is a table, not prose, which is why the text-level pattern misses it:
        the amount sits in one cell and the rating two rows below.
        """
        if soup is None:
            return [], None
        for table in soup.find_all("table"):
            body = table.get_text(" ", strip=True)
            if not self._ACTION_TOTAL_RE.search(body):
                continue
            total = None
            out: list[tuple[str, str]] = []
            for tr in table.find_all("tr"):
                cells = [squash_space(c.get_text(" ", strip=True))
                         for c in tr.find_all(["th", "td"])]
                # The wrapper row flattens the whole block into a single <tr>,
                # so very wide rows are skipped. Real rows are a label and a
                # value, sometimes followed by a 'Regulator Of Instrument'
                # column - present for Citius Transnet, absent for Bhusawal.
                if not 2 <= len(cells) <= 3:
                    continue
                label, value = cells[0], cells[1]
                if total is None and self._ACTION_TOTAL_RE.search(label):
                    total = parse_amount(value)
                elif self._ACTION_LABEL_RE.match(label) and self._is_rating_cell(value):
                    out.append((label, value))
            if out:
                return out, total
        return [], None

    @staticmethod
    def _html_details(text: str) -> dict[str, tuple[Optional[float], str, str]]:
        """instrument (lowercased) -> (amount, rating action, rating) from the rationale.

        The rating is carried even though the listing JSON normally supplies it:
        for entities CRISIL hosts but does not index, the JSON returns nothing
        and the rationale is the only source. Dropping it there left rows with a
        date, an amount and no rating at all.
        """
        out: dict[str, tuple[Optional[float], str, str]] = {}
        for amount_s, instrument, rating, action in _SUMMARY_RE.findall(text):
            key = squash_space(instrument).lower()
            if key and key not in out:
                out[key] = (parse_amount(amount_s), extract_action(action),
                            squash_space(rating))
        return out

    @staticmethod
    def _document_date(text: str):
        """The rationale is dated in its opening lines."""
        m = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},\s*\d{4}\b",
            text[:4000],
        )
        return parse_date(m.group(0)) if m else None

    # -- lenders -----------------------------------------------------------

    def lenders(self, doc: Document) -> list[LenderLine]:
        """The lender annexure is inline in the rationale, no extra request."""
        soup = self._soup(doc)
        return self._lender_rows(soup) if soup is not None else []

    @staticmethod
    def _lender_rows(soup: BeautifulSoup, include_unallocated: bool = False) -> list[LenderLine]:
        out: list[LenderLine] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header = [squash_space(c.get_text(" ", strip=True))
                      for c in rows[0].find_all(["td", "th"])]
            # Require a header *cell* that is the lender column, not merely a
            # table whose text mentions it. CRISIL nests tables, so a substring
            # test matches the outer wrapper too and yields rows like
            # 'Annexure - Details of Bank Lenders & Facilities' as lender names.
            #
            # The exact-cell test is still not enough. Flattening the wrapper
            # leaves its first row holding every cell of the inner table, an
            # exact 'Name of Lender' among them, so the wrapper matched anyway:
            # Cube Highways parsed 20 lender rows instead of 8 and reported
            # NaBFID at 2,943 crore against a true 2,843. A real header is a
            # handful of short cells.
            if not (2 <= len(header) <= 8) or any(len(h) > 60 for h in header):
                continue
            if not any(re.fullmatch(r"name of lender", h, re.I) for h in header):
                continue
            joined = " ".join(header).lower()
            unit = detect_unit(joined, default="Rs. Crore")

            def col(*pats: str) -> int:
                for i, h in enumerate(header):
                    if any(re.search(p, h, re.I) for p in pats):
                        return i
                return -1

            i_name = col(r"name of lender", r"\blender\b")
            i_fac = col(r"facilit", r"instrument")
            i_amt = col(r"amount", r"rs\.?\s*crore")
            if i_name < 0:
                continue

            for row in rows[1:]:
                cells = [squash_space(c.get_text(" ", strip=True))
                         for c in row.find_all(["td", "th"])]
                if i_name >= len(cells):
                    continue
                nm = cells[i_name]
                if not nm or nm.lower() == "total":
                    continue
                # 'Not Applicable' marks a proposed, unallocated facility - no
                # lender committed yet, so it is not a lender row.
                if nm.lower() in {"not applicable", "na", "n/a", "-"} and not include_unallocated:
                    continue
                out.append(
                    LenderLine(
                        lender_name=nm,
                        facility=cells[i_fac] if 0 <= i_fac < len(cells) else "",
                        amount=parse_amount(cells[i_amt]) if 0 <= i_amt < len(cells) else None,
                        unit=unit,
                    )
                )
        return out
