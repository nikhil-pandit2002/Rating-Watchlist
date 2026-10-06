"""PDF text/table/link extraction.

Two engines on purpose: pdfplumber gives good table geometry, PyMuPDF gives
better text runs and is the only one that reads link annotations (which is how
CARE hides its lender-wise URL).
"""
from __future__ import annotations

import io
import logging
import re
from functools import lru_cache
from typing import Optional

# PyMuPDF was renamed: 'pymupdf' since 1.24, 'fitz' before that. Importing the
# old name still works but prints a deprecation warning at startup, in red,
# which reads as a failure to anyone who has just installed the tool. Prefer the
# new name and fall back only for older builds.
try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz
import pdfplumber

log = logging.getLogger(__name__)


class PdfDoc:
    """A parsed PDF: text, tables and hyperlinks."""

    def __init__(self, data: bytes):
        self.data = data
        self._text: Optional[str] = None
        self._tables: Optional[list[list[list[str]]]] = None
        self._links: Optional[list[str]] = None

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = self._extract_text()
        return self._text

    def _extract_text(self) -> str:
        # PyMuPDF first - it preserves inter-word spacing more reliably.
        try:
            with fitz.open(stream=self.data, filetype="pdf") as doc:
                out = "\n".join(page.get_text("text") for page in doc)
            if out.strip():
                return out
        except Exception as e:
            log.debug("pymupdf text failed: %s", e)
        try:
            with pdfplumber.open(io.BytesIO(self.data)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            log.warning("pdf text extraction failed: %s", e)
            return ""

    @property
    def tables(self) -> list[list[list[str]]]:
        if self._tables is None:
            self._tables = self._extract_tables()
        return self._tables

    def _extract_tables(self) -> list[list[list[str]]]:
        out: list[list[list[str]]] = []
        try:
            with pdfplumber.open(io.BytesIO(self.data)) as pdf:
                for page in pdf.pages:
                    for tbl in page.extract_tables() or []:
                        cleaned = [
                            [respace(c) if c else "" for c in row]
                            for row in tbl
                            if any(c for c in row)
                        ]
                        if cleaned:
                            out.append(cleaned)
        except Exception as e:
            log.warning("pdf table extraction failed: %s", e)
        return out

    @property
    def links(self) -> list[str]:
        """External URLs from link annotations (order = page order)."""
        if self._links is None:
            urls: list[str] = []
            try:
                with fitz.open(stream=self.data, filetype="pdf") as doc:
                    for page in doc:
                        for lk in page.get_links():
                            uri = lk.get("uri")
                            if uri and uri not in urls:
                                urls.append(uri)
            except Exception as e:
                log.debug("pdf link extraction failed: %s", e)
            self._links = urls
        return self._links

    def find_link(self, pattern: str) -> Optional[str]:
        rx = re.compile(pattern, re.I)
        for u in self.links:
            if rx.search(u):
                return u
        return None


# --------------------------------------------------------------------------
# Whitespace repair
# --------------------------------------------------------------------------

# Some agencies (Brickwork especially) embed fonts whose extraction drops the
# spaces between words: "LongtermUnsecuredbonds". Full dictionary re-splitting
# is overkill; these targeted rules recover the cases that matter for parsing.
_RESPACE_RULES = [
    (r"(?<=[a-z])(?=[A-Z])", " "),               # longTerm    -> long Term
    (r"(?<=[A-Za-z])(?=\d)", " "),               # Rs2000      -> Rs 2000
    (r"(?<=\d)(?=[A-Za-z])", " "),               # 2000Crs     -> 2000 Crs
]


def respace(text: str) -> str:
    """Repair missing spaces and collapse newlines inside a table cell."""
    if not text:
        return ""
    t = str(text).replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # Only intervene when the text looks space-starved.
    words = t.split(" ")
    long_runs = [w for w in words if len(w) > 14 and w.isalpha()]
    if long_runs:
        for pattern, repl in _RESPACE_RULES:
            t = re.sub(pattern, repl, t)
        t = re.sub(r"\s+", " ", t).strip()
    return t


def rows_matching(tables: list[list[list[str]]], *keywords: str) -> list[list[list[str]]]:
    """Return the tables whose header row mentions all given keywords."""
    out = []
    for tbl in tables:
        if not tbl:
            continue
        header = " ".join(c or "" for c in tbl[0]).lower()
        if all(k.lower() in header for k in keywords):
            out.append(tbl)
    return out
