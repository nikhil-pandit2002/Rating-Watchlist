"""Orchestration: companies x agencies -> rating records.

Concurrency model: one worker thread per agency. Each agency's requests stay
serial and politely throttled, while the six agencies progress in parallel.
That keeps a 500-borrower run practical without hammering any single host.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from threading import Lock
from typing import Optional

from .agencies.base import AgencyAdapter, AmbiguousMatchError
from .models import Company, RatingRecord

log = logging.getLogger(__name__)


@dataclass
class QaRow:
    """One line of the QA sheet: why a company/agency pair produced nothing."""

    borrower_name: str
    cin: str
    agency: str
    status: str
    detail: str = ""
    # For 'ambiguous_match': the competing entity names, so the UI can present
    # them for a one-time confirmation against the CIN.
    candidates: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    records: list[RatingRecord] = field(default_factory=list)
    qa: list[QaRow] = field(default_factory=list)

    def summary(self) -> dict:
        by_agency: dict[str, int] = {}
        for r in self.records:
            by_agency[r.agency] = by_agency.get(r.agency, 0) + 1
        return {
            "records": len(self.records),
            "companies_with_ratings": len({r.borrower_name for r in self.records}),
            "by_agency": by_agency,
            "qa_rows": len(self.qa),
        }


def months_before(anchor: date, months: int) -> date:
    """The same day-of-month `months` earlier, clamped for short months."""
    total = anchor.month - 1 - months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.year and anchor.day or 1, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


class Pipeline:
    # SEBI requires CRAs to review accepted ratings at least annually, so a
    # newest press release older than this has almost certainly lapsed or been
    # withdrawn. Such rows are still emitted - they are the agency's latest -
    # but they are flagged so nobody reads them as current.
    STALE_AFTER_DAYS = 550  # ~18 months

    # Hard cut-off measured from the day the report is taken. A rating older
    # than this is not reported at all - the field is left blank rather than
    # filled with an opinion that has lapsed. This is a business rule, not a
    # technical one: an 18-month-old rating is still 'the agency's latest', but
    # it is not something a credit decision should rest on.
    MAX_RATING_AGE_MONTHS = 15

    def __init__(
        self,
        adapters: list[AgencyAdapter],
        latest_only: bool = True,
        resolutions=None,
        progress=None,
        strict: bool = False,
        max_age_months: Optional[int] = MAX_RATING_AGE_MONTHS,
        as_of: Optional[date] = None,
    ):
        self.adapters = adapters
        self.latest_only = latest_only
        # None disables the cut-off entirely; as_of defaults to the run date, so
        # 'within 15 months' is measured from when the report is taken.
        self.max_age_months = max_age_months
        self.as_of = as_of or date.today()
        self.cutoff = (
            months_before(self.as_of, max_age_months) if max_age_months else None
        )
        # Optional ResolutionStore: supplies CIN pins before the run and
        # records newly confirmed matches after it.
        self.resolutions = resolutions
        # Strict mode: never accept a match a person has not confirmed. An
        # unconfirmed entity produces no rating row and a 'needs_confirmation'
        # QA row carrying the ranked candidates instead.
        #
        # This is what makes the output trustworthy enough to replace manual
        # collection. No similarity threshold can do the same job: the two false
        # matches found in testing - 'IndiGrid Trust' against 'Indigrid
        # Infrastructure Trust', and 'BNP PARIBAS' against 'BNP Paribas India
        # Solutions Private Limited' - both scored 97, above anything a real
        # match could be required to clear. Confirming once per entity per
        # agency is the only way to be certain, and it is a one-off cost against
        # a list that is currently collected entirely by hand.
        self.strict = strict
        # Optional callable(done, total, label) for UI progress.
        self.progress = progress
        self._done = 0
        self._total = 0
        self._lock = Lock()

    def _tick(self, label: str) -> None:
        if not self.progress:
            return
        with self._lock:
            self._done += 1
            done, total = self._done, self._total
        try:
            self.progress(done, total, label)
        except Exception:  # progress reporting must never break a run
            log.debug("progress callback failed", exc_info=True)

    def run(self, companies: list[Company]) -> RunResult:
        result = RunResult()
        if not self.adapters:
            log.error("no agency adapters enabled")
            return result

        # Apply stored confirmations before anything is searched. Keyed by CIN
        # when there is one, otherwise by borrower name, so a company confirmed
        # once is never asked about again.
        if self.resolutions is not None:
            for c in companies:
                stored = (
                    self.resolutions.confirmed(c.cin, c.borrower_name)
                    if self.strict
                    else self.resolutions.get(c.cin, c.borrower_name)
                )
                if stored:
                    c.pinned = {**stored, **(c.pinned or {})}
                urls = getattr(self.resolutions, "urls", None)
                if callable(urls):
                    stored_urls = urls(c.cin, c.borrower_name)
                    if stored_urls:
                        c.pinned_urls = {**stored_urls, **(c.pinned_urls or {})}

        # Safe in every mode now that pick_company scores candidates against the
        # borrower's full name rather than the query that retrieved them: a
        # broader query can surface an entity an agency has misspelled, but it
        # can no longer lower the bar for calling it a match.
        for c in companies:
            c.broad_search = True

        self._done = 0
        self._total = len(companies) * len(self.adapters)

        with ThreadPoolExecutor(max_workers=len(self.adapters)) as pool:
            futures = {
                pool.submit(self._run_agency, adapter, companies): adapter.name
                for adapter in self.adapters
            }
            for fut in as_completed(futures):
                agency = futures[fut]
                try:
                    records, qa = fut.result()
                    result.records.extend(records)
                    result.qa.extend(qa)
                    log.info("%s: %d record(s)", agency, len(records))
                except Exception as e:
                    log.exception("%s: agency run failed", agency)
                    result.qa.append(QaRow("", "", agency, "agency_failed", str(e)))

        result.records.sort(
            key=lambda r: (
                r.borrower_name.lower(),
                r.agency,
                r.instrument_category,
                -(r.amount or 0),
            )
        )
        return result

    def _run_agency(
        self, adapter: AgencyAdapter, companies: list[Company]
    ) -> tuple[list[RatingRecord], list[QaRow]]:
        records: list[RatingRecord] = []
        qa: list[QaRow] = []

        for company in companies:
            try:
                self._one_company(adapter, company, records, qa)
            except Exception as e:  # never let one borrower kill the agency run
                log.exception("%s: unhandled error for %s", adapter.name, company.borrower_name)
                qa.append(
                    QaRow(company.borrower_name, company.cin, adapter.name, "error", str(e))
                )
            finally:
                self._tick(f"{adapter.name}: {company.borrower_name}")

        return records, qa

    def _one_company(
        self,
        adapter: AgencyAdapter,
        company: Company,
        records: list[RatingRecord],
        qa: list[QaRow],
    ) -> None:
        """Collect one borrower's latest rating from one agency."""
        # A pinned document URL replaces discovery outright. It is the only way
        # to reach a rating an agency hosts but keeps out of its own search, and
        # because a person supplied the exact document there is nothing left to
        # match - so it satisfies strict mode as fully as a confirmed entity.
        pinned_url = (company.pinned_urls or {}).get(adapter.name, "")
        if pinned_url:
            doc = adapter.document_from_url(pinned_url, company.borrower_name)
            self._collect(adapter, company, doc, records, qa)
            return

        if self.strict and not company.pin_for(adapter.name):
            # Nothing confirmed for this entity at this agency, so nothing is
            # collected. The candidates go to the review queue; once one is
            # confirmed it is pinned and every later run is fully automatic.
            try:
                choices = adapter.candidates(company)
            except Exception as e:
                log.warning("%s: candidate lookup failed for %s: %s",
                            adapter.name, company.borrower_name, e)
                qa.append(QaRow(company.borrower_name, company.cin, adapter.name,
                                "search_failed", str(e)))
                return
            qa.append(
                QaRow(
                    company.borrower_name,
                    company.cin,
                    adapter.name,
                    "needs_confirmation" if choices else "not_rated",
                    ("awaiting one-time confirmation of the agency entity"
                     if choices else "agency index has no plausible entity"),
                    candidates=[n for n, _ in choices],
                )
            )
            return

        try:
            doc = adapter.latest(company)
        except AmbiguousMatchError as e:
            # Deliberately emit no rating row: the borrower name matches
            # several of the agency's entities equally well, and guessing
            # would silently attach another company's rating. The candidates
            # travel with the QA row so the UI can offer them for a one-time
            # confirmation, after which the CIN pin resolves it for good.
            log.warning(
                "%s: ambiguous match for %s: %s", adapter.name, company.borrower_name, e
            )
            qa.append(
                QaRow(
                    company.borrower_name,
                    company.cin,
                    adapter.name,
                    "ambiguous_match",
                    str(e),
                    candidates=[n for n, _ in e.choices],
                )
            )
            return
        except Exception as e:
            log.warning("%s: search failed for %s: %s", adapter.name, company.borrower_name, e)
            qa.append(QaRow(company.borrower_name, company.cin, adapter.name, "search_failed", str(e)))
            return

        if doc is None:
            qa.append(QaRow(company.borrower_name, company.cin, adapter.name, "not_rated", "no matching company/press release"))
            return

        # An unambiguous match is worth remembering: next run it is pinned and
        # no fuzzy matching happens at all. Skipped under strict mode, where the
        # only pin that counts is one a person made and re-recording it as
        # 'auto' would blur that distinction.
        if not self.strict and self.resolutions is not None and doc.company_name:
            self.resolutions.set(
                company.cin, adapter.name, doc.company_name, company.borrower_name, "auto"
            )
        self._collect(adapter, company, doc, records, qa)

    def _collect(
        self,
        adapter: AgencyAdapter,
        company: Company,
        doc,
        records: list[RatingRecord],
        qa: list[QaRow],
    ) -> None:
        """Parse one located document and turn it into rating rows.

        Shared by ordinary discovery and by the URL-pin path, so a hand-supplied
        document goes through exactly the same age window, NaBFID lookup and
        latest-per-category reduction as one the search found.
        """
        try:
            parsed = adapter.parse(doc)
        except Exception as e:
            log.warning("%s: parse failed for %s: %s", adapter.name, doc.url, e)
            qa.append(QaRow(company.borrower_name, company.cin, adapter.name, "parse_failed", f"{e} :: {doc.url}"))
            return

        if not parsed:
            qa.append(QaRow(company.borrower_name, company.cin, adapter.name, "no_rows", doc.url))
            return

        if doc.published is not None:
            age = (date.today() - doc.published).days
            if age > self.STALE_AFTER_DAYS:
                qa.append(
                    QaRow(
                        company.borrower_name,
                        company.cin,
                        adapter.name,
                        "stale_rating",
                        f"newest press release is {age // 30} months old "
                        f"({doc.published}); likely withdrawn or lapsed - {doc.url}",
                    )
                )

        if self.latest_only:
            parsed = keep_latest_per_category(parsed)

        if self.cutoff is not None:
            fresh, dropped = [], []
            for r in parsed:
                # Fall back to the release date when a row carries none of its
                # own, so an undated line inherits the document's age rather
                # than escaping the cut-off.
                when = r.rating_date or doc.published
                (fresh if when is None or when >= self.cutoff else dropped).append(r)
            if dropped and not fresh:
                newest = max(
                    (r.rating_date for r in dropped if r.rating_date),
                    default=doc.published,
                )
                qa.append(
                    QaRow(
                        company.borrower_name,
                        company.cin,
                        adapter.name,
                        "outside_age_window",
                        f"latest rating is {newest}, older than the "
                        f"{self.max_age_months}-month window "
                        f"(cut-off {self.cutoff}) - reported blank; {doc.url}",
                    )
                )
                return
            parsed = fresh

        try:
            adapter.apply_nabfid(parsed, doc)
        except Exception as e:
            log.warning("%s: NaBFID lookup failed for %s: %s", adapter.name, doc.url, e)

        location = ""
        getter = getattr(adapter, "location", None)
        if callable(getter):
            try:
                location = getter(doc) or ""
            except Exception:
                location = ""

        for rec in parsed:
            # The input sheet is authoritative for identity fields.
            rec.borrower_name = company.borrower_name
            rec.cin = company.cin
            rec.pan = company.pan
            if location and not rec.location:
                rec.location = location

        records.extend(parsed)


def keep_latest_per_category(records: list[RatingRecord]) -> list[RatingRecord]:
    """Keep only rows from the newest rating date within each instrument category.

    A press release restates the full outstanding book, so this is normally a
    no-op; it matters when an agency issues separate releases (bank facilities
    vs NCDs) on different dates and both end up in scope.
    """
    latest: dict[str, Optional[object]] = {}
    for r in records:
        key = r.instrument_category or r.instrument_details
        if r.rating_date is None:
            latest.setdefault(key, None)
            continue
        cur = latest.get(key)
        if cur is None or r.rating_date > cur:
            latest[key] = r.rating_date

    out = []
    for r in records:
        key = r.instrument_category or r.instrument_details
        want = latest.get(key)
        if want is None or r.rating_date == want:
            out.append(r)
    return out
