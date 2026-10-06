"""Infomerics Valuation and Rating Ltd (IVR).

The public site is a Next.js app with no usable per-company route, but it is
backed by an unauthenticated Strapi CMS that exposes two whole-collection
endpoints:

  GET cms.infomerics.com/api/companies     -> 4,479 companies
       [{id, documentId, CompanyName, slug, PAN, Address, industry, ...}]
  GET cms.infomerics.com/api/upload/files  -> ~92,700 files (58 MB)
       [{name, url, ext, size, createdAt, ...}]

Neither honours `filters` or `pagination` - any query string is ignored and the
full collection comes back. That sounds wasteful but suits a 500-borrower run:
both indexes are fetched once, cached on disk for a week, and every borrower is
then matched offline with no further requests.

Documents are public on Azure blob storage. Two families matter:
    pr_*  / PR_*   press release            (~49,000 files)
    len_* / Len_*  facility-wise lender details -> the NaBFID source

The company -> document link is NOT exposed by the CMS (`/api/companies/{id}`
returns 500), so documents are matched by filename. Filenames are inconsistent
- 'pr-ShreeRam-Proteins-28june24.pdf', 'PR_Shree_Ram_Resins_31_12_2019.pdf',
'PR_RIL_23.04.2019.pdf' - so matching is fuzzy and deliberately refuses to
guess: a borrower matching several distinct file groups raises
AmbiguousMatchError and is reported for confirmation rather than resolved by
coin toss.

Note: Infomerics rates SMEs and mid-corporates. Large caps (Adani Power,
Reliance, NTT) return no match at all, which is correct, not a failure.
`PAN` and `Address` exist on the company schema but are empty for all 4,479
records, so they cannot fill those output columns.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from typing import Optional

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
from ..pdf_utils import PdfDoc
from .base import AgencyAdapter, AmbiguousMatchError

log = logging.getLogger(__name__)

CMS = "https://cms.infomerics.com"
INDEX_MAX_AGE = 7 * 24 * 3600  # both indexes are large; a week is plenty

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Filenames carry the date in several shapes; try the specific ones first.
_DATE_PATTERNS = [
    re.compile(r"(?P<d>\d{1,2})[._\-](?P<m>\d{1,2})[._\-](?P<y>\d{4})"),          # 15.07.2019
    re.compile(rf"(?P<d>\d{{1,2}})[\-_ ]?(?P<mon>{_MON_RE})[\-_ ]?(?P<y>\d{{2,4}})", re.I),  # 28june24
    re.compile(rf"(?P<mon>{_MON_RE})[\-_ ]?(?P<d>\d{{1,2}})[\-_ ](?P<y>\d{{4}})", re.I),     # mar03-2021
    re.compile(rf"(?P<mon>{_MON_RE})[\-_ ]?(?P<y>\d{{2,4}})\b", re.I),                       # mar23
]

# Infomerics rating symbols: IVR AAA / IVR BB+ / IVR A1+ / IVR D
_IVR_RE = re.compile(r"\bIVR\s*(?:AAA|AA|A|BBB|BB|B|C|D)[+-]?\b|\bIVR\s*A[1-4][+-]?\b", re.I)

_PREFIX_RE = re.compile(r"^(pr|press[_\- ]?release|len|lender|rr|rationale)[_\- ]+", re.I)
_TRAIL_HASH_RE = re.compile(r"[_\-][0-9a-f]{8,}$", re.I)


def _strip_date(stem: str) -> str:
    out = stem
    for pat in _DATE_PATTERNS:
        out = pat.sub(" ", out)
    return out


def _file_date(stem: str) -> Optional[date]:
    """Best-effort date from a filename."""
    for pat in _DATE_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        g = m.groupdict()
        try:
            year = int(g["y"])
            if year < 100:
                year += 2000
            month = int(g["m"]) if g.get("m") else _MONTHS[g["mon"].lower()]
            day = int(g["d"]) if g.get("d") else 1
            if 1 <= month <= 12 and 1 <= day <= 31 and 2000 <= year <= 2100:
                return date(year, month, day)
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _key(text: str) -> str:
    """Compress a company name or filename stem to a comparable key.

    'Shree Ram Proteins Limited'      -> shreeramproteins
    'pr-ShreeRam-Proteins-28june24'   -> shreeramproteins
    """
    stem = _PREFIX_RE.sub("", text or "")
    stem = _TRAIL_HASH_RE.sub("", stem)
    stem = _strip_date(stem)
    stem = re.sub(r"\.pdf$", "", stem, flags=re.I)
    # canonical_name drops Ltd/Pvt/Limited and punctuation; then squeeze.
    return re.sub(r"[^a-z0-9]", "", canonical_name(stem.replace("_", " ").replace("-", " ")))


# Cell text wraps mid-word inside their tables, so the INC marker arrives
# broken in several ways - 'COPERATI NG', 'COOPERA TING', 'CO-OPERATING' - and
# is additionally misspelled with one 'o' in many releases. Mend it before the
# shared action matcher sees it, otherwise the marker survives into the rating
# symbol instead of landing in Development.
_COOP_RE = re.compile(r"C\s*O\s*-?\s*O?\s*P\s*E\s*R\s*A\s*T\s*I\s*N\s*G", re.I)

# The words-in-brackets restatement of the symbol, closing bracket optional
# because the cell is often truncated: '(IVR Double B Plus with Stable Outlook'
_VERBAL_RE = re.compile(r"\(\s*IVR\b[^)]*\)?", re.I)

# Rows of the headline block are named after the rating type rather than an
# instrument, and duplicate what Annexure 1 itemises.
_SUMMARY_ROW_RE = re.compile(r"^(long|short)\s*term(\s*/\s*(long|short)\s*term)?\s*rating\b", re.I)

# Categories that Annexure 1 already itemises facility by facility.
_BANK_CATEGORIES = {
    "Bank Facilities",
    "Long Term Bank Facilities",
    "Short Term Bank Facilities",
    "Long Term / Short Term Bank Facilities",
}


def _mend(text: str) -> str:
    return _COOP_RE.sub("COOPERATING", text) if text else text


class _Doc:
    __slots__ = ("name", "url", "when", "kind")

    def __init__(self, name: str, url: str, when: Optional[date], kind: str):
        self.name, self.url, self.when, self.kind = name, url, when, kind


class InfomericsAdapter(AgencyAdapter):
    name = "Infomerics"
    # Filenames are noisy; demand a high bar before accepting a group.
    file_match_threshold: float = 92.0

    def __init__(self, fetcher):
        super().__init__(fetcher)
        self._companies: Optional[list[str]] = None
        self._by_key: Optional[dict[str, list[_Doc]]] = None

    # -- indexes -----------------------------------------------------------

    def _company_names(self) -> list[str]:
        if self._companies is not None:
            return self._companies
        r = self.fetcher.get(f"{CMS}/api/companies", max_age=INDEX_MAX_AGE,
                             headers={"Accept": "application/json"})
        names: list[str] = []
        if r.ok:
            try:
                for rec in r.json().get("data", []):
                    n = squash_space(rec.get("CompanyName") or "")
                    if n:
                        names.append(n)
            except Exception as e:
                log.warning("Infomerics: company index unreadable: %s", e)
        log.info("Infomerics: %d companies in index", len(names))
        self._companies = names
        return names

    def _file_index(self) -> dict[str, list[_Doc]]:
        """key -> documents, built once from the whole media library."""
        if self._by_key is not None:
            return self._by_key
        r = self.fetcher.get(f"{CMS}/api/upload/files", max_age=INDEX_MAX_AGE,
                             headers={"Accept": "application/json"})
        index: dict[str, list[_Doc]] = defaultdict(list)
        if r.ok:
            try:
                for f in r.json():
                    if str(f.get("ext", "")).lower() != ".pdf":
                        continue
                    name = str(f.get("name") or "")
                    kind = ("lender" if re.match(r"^(len|lender)[_\- ]", name, re.I)
                            else "pr" if re.match(r"^(pr|press)[_\- ]", name, re.I)
                            else "")
                    if not kind:
                        continue
                    k = _key(name)
                    if len(k) < 5:          # too short to identify anything
                        continue
                    index[k].append(_Doc(name, str(f.get("url") or ""),
                                         _file_date(name), kind))
            except Exception as e:
                log.warning("Infomerics: file index unreadable: %s", e)
        log.info("Infomerics: %d filename groups indexed", len(index))
        self._by_key = dict(index)
        return self._by_key

    # -- discovery ---------------------------------------------------------

    def candidate_names(self, company: Company) -> list[str]:
        names = self._company_names()
        terms = {w for t in company.search_terms(self.name)
                 for w in canonical_name(t).split() if len(w) > 2}
        if not terms:
            return []
        return [n for n in names if terms & set(canonical_name(n).split())]

    def _resolve_key(self, company: Company) -> Optional[str]:
        """Find the filename group for this borrower, or refuse to guess."""
        index = self._file_index()
        if not index:
            return None

        for term in company.search_terms(self.name):
            k = _key(term)
            if not k:
                continue
            if k in index:
                return k

            # No exact key: score against every group and require a clear win.
            from rapidfuzz import fuzz
            scored = sorted(
                ((fuzz.ratio(k, cand), cand) for cand in index),
                reverse=True,
            )[:6]
            if not scored or scored[0][0] < self.file_match_threshold:
                continue
            rivals = [c for s, c in scored[1:] if s >= self.file_match_threshold
                      and (scored[0][0] - s) <= self.ambiguity_margin]
            if rivals:
                raise AmbiguousMatchError(
                    term,
                    [(c, float(s)) for s, c in scored if s >= self.file_match_threshold],
                    choices=[(self._label(c), float(s)) for s, c in scored],
                )
            return scored[0][1]
        return None

    def _label(self, key: str) -> str:
        """A readable name for a filename group, for the QA sheet."""
        docs = self._file_index().get(key) or []
        return docs[0].name if docs else key

    def search(self, company: Company) -> list[Document]:
        # Confirm Infomerics rates this borrower at all; this also applies the
        # shared ambiguity guard against their official company list.
        names = self._company_names()
        official = None
        if names:
            matched, score = self.pick_company(company, names)
            if not matched:
                log.debug("Infomerics: %r not in company list (best %.0f)",
                          company.borrower_name, score)
                return []
            official = matched

        key = self._resolve_key(company)
        if not key:
            return []

        docs = [d for d in self._file_index().get(key, []) if d.kind == "pr" and d.url]
        if not docs:
            return []
        lender = next((d.url for d in self._file_index().get(key, [])
                       if d.kind == "lender"), "")
        return [
            Document(
                agency=self.name,
                company_name=official or company.borrower_name,
                url=d.url,
                published=d.when,
                doc_id=d.name,
                lender_url=lender,
            )
            for d in docs
        ]

    # -- parsing -----------------------------------------------------------

    def parse(self, doc: Document) -> list[RatingRecord]:
        r = self.fetcher.get(doc.url)
        if not r.ok or b"%PDF" not in r.content[:2048]:
            log.warning("Infomerics: no PDF at %s", doc.url)
            return []

        pdf = PdfDoc(r.content)
        text = pdf.text
        rating_date = doc.published or self._doc_date(text)
        unit = detect_unit(text[:2000], default="Rs. Crore")

        rows = self._instrument_rows(pdf)
        if not rows:
            log.warning("Infomerics: no instrument rows in %s", doc.url)
            return []

        records: list[RatingRecord] = []
        for name, amount, raw, action in rows:
            # Infomerics restates the symbol in words alongside it:
            # 'IVR BB+/ Stable (IVR Double B Plus with Stable Outlook)'.
            # Drop the bracketed restatement before splitting.
            rating, outlook = split_rating(_VERBAL_RE.sub(" ", raw), self.name)
            records.append(
                self._record(doc, rating_date, classify_instrument(name), name,
                             amount, rating, outlook, action or raw, unit)
            )
        return records

    def _instrument_rows(self, pdf: PdfDoc) -> list[tuple[str, Optional[float], str, str]]:
        """(facility, amount, rating text, action text) from whichever table has them.

        Infomerics has used at least two layouts, and within each the columns
        drift relative to the header row - the headline table puts 'Amount' at
        header index 2 but the value at index 1. So rather than trust column
        positions, each row is scanned for the three things that identify
        themselves: a facility name, a number, and a cell containing 'IVR'.

        Candidate tables, best first:
          1. headline table   Facilities | Amount | Ratings | Rating Action
          2. Annexure 1       Name of Facility ... Size of Facility ... Rating
          3. rating history   Instrument | Type | Amount outstanding | Rating
        """
        annexure: list = []      # itemised bank facilities
        instruments: list = []   # non-bank instruments (NCDs, CP, ...)
        summary: list = []       # 'Long Term Rating / Short Term Rating' block
        history: list = []       # three-year table, last resort

        for tbl in pdf.tables:
            if len(tbl) < 2:
                continue
            header = " ".join(c for c in tbl[0] if c).lower()
            if "ivr" in header:            # a stray fragment, not a header
                continue

            rows = [p for row in tbl[1:] if (p := self._scan_row(row))]
            if not rows:
                continue

            if "size of" in header or "name of facility" in header:
                annexure.extend(rows)
            elif "rating action" in header and (
                "instrument" in header or "facilit" in header
            ):
                # The same shape serves two purposes: a real instrument table
                # (NCDs, CP) and the headline summary whose rows are named
                # 'Long Term Rating' / 'Short Term Rating'. Split them.
                for row in rows:
                    (summary if _SUMMARY_ROW_RE.match(row[0]) else instruments).append(row)
            elif "current rating" in header or "rating history" in header:
                history.extend(rows)
            elif "total bank loan" in header:
                # A bank-facilities-only entity's headline table. Its header row
                # doubles as a data row - "Total Bank Loan Facilities Rated |
                # Rs. X Crore | Regulator" - so pdfplumber never sees a row that
                # is both named and priced together, and _scan_row's amount
                # search legitimately comes up empty for 'Long Term Rating' /
                # 'Short Term Rating'. The word "Rating Action" that would
                # normally flag this shape is a page heading here, not a table
                # cell, so it never reaches `header` at all - this branch is
                # what actually catches it. The one total is applied to each
                # row since Infomerics does not break it down further here.
                total = self._total_amount(pdf.text)
                for nm, _amt, rating, action in rows:
                    low = nm.lower()
                    label = ("Short Term Bank Facilities" if low.startswith("short")
                             else "Long Term Bank Facilities" if low.startswith("long")
                             else nm)
                    summary.append((label, total, rating, action))
            elif ("facilit" in header or "instrument" in header) and "rating" in header:
                summary.extend(rows)

        # A borrower can hold both bank facilities and NCDs, rated separately -
        # taking only one table silently drops half the exposure. But Annexure 1
        # already itemises the *bank* facilities, so once it is present the
        # headline block's bank-facility rows are the same money stated coarsely
        # and must not be added again. Non-bank instruments (NCDs, CP) are kept
        # because the annexure does not cover them.
        merged = list(annexure)
        for row in instruments:
            if annexure and classify_instrument(row[0]) in _BANK_CATEGORIES:
                continue
            merged.append(row)
        if not merged:
            merged = summary or history
        # Deliberately no de-duplication: a borrower legitimately holds several
        # facilities of the same type and size (Suriya has three separate
        # 9.15 crore term loans), and collapsing them would understate exposure.
        # Cross-table double counting is already prevented above.
        return merged

    @staticmethod
    def _scan_row(cells: list[str]) -> Optional[tuple[str, Optional[float], str, str]]:
        """Pull (name, amount, rating, action) out of one row by content, not position."""
        vals = [_mend(squash_space(c or "")) for c in cells]
        if not any(vals):
            return None

        rating = next((v for v in vals if _IVR_RE.search(v)), "")
        if not rating:
            return None

        # Name: first alphabetic cell that is not the rating, a tenure marker,
        # a serial number or an action word.
        name = ""
        for v in vals:
            if not v or v is rating or _IVR_RE.search(v):
                continue
            if re.fullmatch(r"\d+\.?|LT|ST|Long Term|Short Term|Simple|Complex|NA|-{1,2}", v, re.I):
                continue
            if extract_action(v) and len(v) < 24:
                continue
            if re.match(r"^[\d,]+\.?\d*(\s*\(.*\))?$", v):
                continue
            if re.match(r"^(total|sr\.?\s*no|type|amount|rating|name of)\b", v, re.I):
                continue
            if len(v) >= 3:
                name = v
                break
        if not name:
            return None

        amount = None
        for v in vals:
            if v is rating:
                continue
            m = re.match(r"^([\d,]+\.\d{1,2}|[\d,]{2,})\b", v)
            if m and not re.search(r"20\d{2}", m.group(1)):
                amount = parse_amount(m.group(1))
                if amount is not None:
                    break

        action = next((v for v in vals if v is not rating and extract_action(v)
                       and len(v) < 40), "")
        return name, amount, rating, action or rating

    @staticmethod
    def _record(doc, when, category, details, amount, rating, outlook, raw, unit):
        return RatingRecord(
            borrower_name=doc.company_name,
            rating_date=when,
            # A static helper, so it takes the agency from the document it was
            # given rather than from an instance it does not have.
            agency=doc.agency,
            instrument_category=category,
            instrument_details=squash_space(details),
            amount=amount,
            rating=rating,
            development=extract_action(raw),
            outlook=outlook,
            url=doc.url,
            unit=unit,
        )

    @staticmethod
    def _doc_date(text: str) -> Optional[date]:
        """The release date sits on the third line, under the company name."""
        for line in [l.strip() for l in text.splitlines()[:8] if l.strip()]:
            d = parse_date(line)
            if d:
                return d
        return None

    @staticmethod
    def _total_amount(text: str) -> Optional[float]:
        m = re.search(r"Total\s+Bank\s+Loan\s+Facilities\s+Rated\s+Rs\.?\s*([\d,]+\.?\d*)",
                      text, re.I)
        return parse_amount(m.group(1)) if m else None

    # -- lenders -----------------------------------------------------------

    def lenders(self, doc: Document) -> list[LenderLine]:
        if not doc.lender_url:
            return []
        r = self.fetcher.get(doc.lender_url, max_age=6 * 3600)
        if not r.ok or b"%PDF" not in r.content[:2048]:
            return []

        pdf = PdfDoc(r.content)
        out: list[LenderLine] = []
        for tbl in pdf.tables:
            if not tbl:
                continue
            header = [squash_space(c or "") for c in tbl[0]]
            joined = " ".join(header).lower()
            if not re.search(r"lender|name of (the )?bank", joined):
                continue
            unit = detect_unit(joined, default="Rs. Crore")

            def col(*pats: str) -> int:
                for idx, h in enumerate(header):
                    if any(re.search(pt, h, re.I) for pt in pats):
                        return idx
                return -1

            i_name = col(r"name of (the )?(bank|lender)", r"\blender\b", r"\bbank\b")
            i_fac = col(r"facilit", r"instrument", r"type", r"nature")
            i_amt = col(r"sanction", r"amount", r"limit")
            if i_name < 0:
                continue

            for row in tbl[1:]:
                cells = [squash_space(c or "") for c in row]
                if i_name >= len(cells):
                    continue
                nm = cells[i_name].strip()
                if not nm or re.match(r"^(total|grand total|sub[\s-]*total)$", nm, re.I):
                    continue
                if re.fullmatch(r"\d+\.?", nm):
                    continue
                out.append(
                    LenderLine(
                        lender_name=nm,
                        facility=cells[i_fac].strip() if 0 <= i_fac < len(cells) else "",
                        amount=parse_amount(cells[i_amt]) if 0 <= i_amt < len(cells) else None,
                        unit=unit,
                    )
                )
        return out
