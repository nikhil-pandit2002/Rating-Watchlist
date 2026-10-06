"""Adapter contract every rating agency implements."""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import unquote

from ..http_client import Fetcher
from ..models import Company, Document, LenderLine, RatingRecord
from ..normalize import (
    NABFID_CANONICAL, canonical_name, is_nabfid, name_score, parse_date, rank_matches,
)

log = logging.getLogger(__name__)


class AmbiguousMatchError(Exception):
    """Several agency entities match the borrower name equally well.

    Raised instead of guessing. Real case: "NTT Global Data Center" scores
    exactly 88.0 against three different SPVs (BLR4, Del2, NAV2) while the
    parent scores 78.6 and is rejected - and the SPVs are rated A+ against the
    parent's AA+. Picking one at random would put a three-notch error into the
    output with nothing to flag it.
    """

    def __init__(
        self,
        term: str,
        ranked: list[tuple[str, float]],
        choices: Optional[list[tuple[str, float]]] = None,
    ):
        self.term = term
        # The entities that actually tied - what makes this ambiguous.
        self.ranked = ranked
        # What to offer a human. Deliberately wider than `ranked`: the correct
        # entity is often the one that scored *below* threshold. "NTT Global
        # Data Center" ties three SPVs at 88 while the parent - the right
        # answer - sits at 78.6, so a threshold-only list cannot be resolved.
        self.choices = choices or ranked
        names = "; ".join(f"{n} ({s:.0f})" for n, s in ranked)
        super().__init__(f"{len(ranked)} candidates matched {term!r} equally well: {names}")


class AgencyAdapter(ABC):
    """One adapter per rating agency.

    The pipeline only ever calls: search -> latest -> parse -> lenders.
    """

    name: str = ""
    # Minimum fuzzy score to accept an agency's company as ours.
    match_threshold: float = 88.0
    # Two candidates within this many points of each other are treated as a
    # tie, not a winner. Sibling SPVs routinely land on identical scores.
    ambiguity_margin: float = 2.0
    # How many entities to offer a human when resolving an ambiguous match.
    max_choices: int = 6

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    # -- discovery ---------------------------------------------------------

    @abstractmethod
    def search(self, company: Company) -> list[Document]:
        """All press releases this agency has for the company (any date)."""

    def latest(self, company: Company) -> Optional[Document]:
        """The most recent press release, or None if the agency has no rating."""
        docs = self.search(company)
        if not docs:
            return None
        dated = [d for d in docs if d.published]
        if not dated:
            return docs[0]
        return max(dated, key=lambda d: d.published)

    # -- extraction --------------------------------------------------------

    def candidate_names(self, company: Company) -> list[str]:
        """Agency entity names that might be this borrower.

        Cheap: hits only the agency's search/index, never a rating document.
        Used by the pre-flight resolve step so a person can confirm which
        company is meant *before* any report is collected.
        """
        return []

    def candidates(self, company: Company) -> list[tuple[str, float]]:
        """Ranked (entity name, score), best first, for confirmation by a human."""
        pin = company.pin_for(self.name)
        names = self._dedupe(self.candidate_names(company))
        if not names:
            return []
        ranked = rank_matches(company.borrower_name, names)[: self.max_choices]
        if pin:
            # A pinned entity always leads, whatever the fuzzy score says.
            ranked = [(pin, 100.0)] + [(n, s) for n, s in ranked
                                       if canonical_name(n) != canonical_name(pin)]
        return ranked

    def document_from_url(self, url: str, company_name: str = "") -> Document:
        """Wrap a hand-supplied rating-document URL so parse() can read it.

        For entities an agency hosts but does not index. The publication date is
        left to the parser wherever possible - every agency prints it inside the
        document, which is more trustworthy than anything inferred from a URL -
        with the filename used only as a fallback for parsers that need a date
        up front.
        """
        stem = unquote(url.rsplit("/", 1)[-1])
        published = None
        m = re.search(r"([A-Za-z]{3,9})[\s_%]+(\d{1,2})[\s_,%]+(\d{4})", stem)
        if m:
            published = parse_date(f"{m.group(1)} {m.group(2)}, {m.group(3)}")
        return Document(
            agency=self.name,
            company_name=company_name,
            url=url,
            published=published,
            doc_id=stem,
            kind="PR",
        )

    @abstractmethod
    def parse(self, doc: Document) -> list[RatingRecord]:
        """One RatingRecord per rated instrument line in the document."""

    def lenders(self, doc: Document) -> list[LenderLine]:
        """Lender-wise bank facility rows. Empty when the agency has none."""
        return []

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _dedupe(candidates: list[str]) -> list[str]:
        """Collapse index entries that name the same entity.

        Agencies list a borrower more than once under spellings that differ
        only in decoration: India Ratings carries both 'ANDHRA PRADESH CAPITAL
        REGION DEVELOPMENT AUTHORITY (APCRDA)' and 'Andhra Pradesh Capital
        Region Development Authority'. Both score 100, which reads as a tie
        between two entities and blocks the match as ambiguous - when in fact
        there is only one entity and no ambiguity at all.

        The longest spelling is kept, since that is the one the agency's own
        document is most likely to carry.
        """
        best: dict[str, str] = {}
        for c in candidates:
            k = canonical_name(c)
            if not k:
                continue
            if k not in best or len(c) > len(best[k]):
                best[k] = c
        return list(best.values())

    def pick_company(self, company: Company, candidates: list[str]) -> tuple[Optional[str], float]:
        """Fuzzy-match our borrower against the agency's spelling.

        Raises AmbiguousMatchError when two or more candidates clear the
        threshold within `ambiguity_margin` of each other: the borrower name is
        too vague to identify one entity, and that must be resolved by a human
        rather than by a coin toss.
        """
        candidates = self._dedupe(candidates)
        # A CIN-pinned entity wins outright: the user has already confirmed
        # which of the agency's companies this borrower is, so no amount of
        # fuzzy similarity should be allowed to override it.
        pin = company.pin_for(self.name)
        if pin:
            for c in candidates:
                if canonical_name(c) == canonical_name(pin):
                    return c, 100.0
            log.debug(
                "%s: pinned entity %r not in this result set (%d candidates)",
                self.name, pin, len(candidates),
            )
            return None, 0.0

        best_score = 0.0
        ambiguity: Optional[AmbiguousMatchError] = None

        # A search term is for *retrieval*; identity is always judged against
        # the borrower's full name. Scoring against the term instead is what
        # made loose queries dangerous: 'Clean Solar' scores 85 against a
        # sibling SPV 'Clean Solar Power (Bhadla)', while the full name
        # 'Clean Solar Power (Baniyana) Private Limited' scores it 58.8 and
        # rejects it outright. Separating the two lets a query be as broad as it
        # needs to be to survive an agency's misspelling, without ever loosening
        # what counts as the same company.
        full_names = [company.borrower_name, *company.aliases]

        def rescore(ranked: list[tuple[str, float]]) -> list[tuple[str, float]]:
            out = [
                (n, max(name_score(f, n) for f in full_names if f))
                for n, _ in ranked
            ]
            return sorted(out, key=lambda x: -x[1])

        for term in company.search_terms(self.name):
            ranked = rescore(rank_matches(term, candidates))
            if not ranked:
                continue
            top_name, top_score = ranked[0]
            best_score = max(best_score, top_score)

            if top_score < self.match_threshold:
                continue

            rivals = [
                (n, s)
                for n, s in ranked[1:]
                if s >= self.match_threshold and (top_score - s) <= self.ambiguity_margin
            ]
            if rivals:
                # Hold the ambiguity rather than raising immediately: a later,
                # more specific alias is exactly how a user disambiguates a
                # vague borrower name, and it deserves the chance to resolve it.
                ambiguity = ambiguity or AmbiguousMatchError(
                    term,
                    [(top_name, top_score), *rivals],
                    choices=ranked[: self.max_choices],
                )
                continue
            return top_name, top_score

        if ambiguity is not None:
            raise ambiguity
        return None, best_score

    def nabfid_line(self, doc: Document) -> Optional[LenderLine]:
        """The NaBFID row from the lender table, if NaBFID is a lender."""
        try:
            for line in self.lenders(doc):
                if is_nabfid(line.lender_name):
                    return line
        except Exception as e:
            log.warning("%s: lender lookup failed for %s: %s", self.name, doc.url, e)
        return None

    def apply_nabfid(self, records: list[RatingRecord], doc: Document) -> None:
        """Stamp NaBFID name / loan amount / unit onto every record.

        The exposure is summed across every NaBFID row in the annexure, not
        taken from the first. A single lender routinely appears on several
        facility lines - Nxt-Infra Trust's annexure carries three NaBFID rows of
        2200, 50 and 300 - and reporting only the first understated the loan by
        350 crore there.
        """
        try:
            lines = [l for l in self.lenders(doc) if is_nabfid(l.lender_name)]
        except Exception as e:
            log.warning("%s: lender lookup failed for %s: %s", self.name, doc.url, e)
            return
        if not lines:
            return
        amounts = [l.amount for l in lines if l.amount is not None]
        total = sum(amounts) if amounts else None
        unit = next((l.unit for l in lines if l.unit), "")
        for r in records:
            r.nabfid_name = NABFID_CANONICAL
            r.loan_amount = total
            if unit:
                r.unit = unit
