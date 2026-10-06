"""CIN -> agency entity mappings.

No rating agency publishes a CIN anywhere in its search results or its rating
documents (verified across CARE and ICRA: zero occurrences). So a CIN cannot be
matched against agency data directly. What it can do is act as the permanent
identity key: once someone confirms "CIN X is ICRA's entity Y", that decision is
recorded here and from then on it overrides fuzzy name matching entirely.

This is what makes CIN take preference. It also permanently retires ambiguity
for that borrower - the case where "NTT Global Data Center" tied against three
different SPVs and silently resolved to the wrong one.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .normalize import canonical_name

# Key prefix for borrowers held without a CIN.
NAME_PREFIX = "NAME:"


def normalise_cin(cin: str) -> str:
    """CINs are case-insensitive here and often arrive with stray spaces."""
    return "".join((cin or "").split()).upper()


def entity_key(cin: str, borrower_name: str = "") -> str:
    """The identity a confirmation is stored against.

    A CIN when there is one - it is stable and unambiguous. Otherwise the
    borrower's own name, normalised, so that confirming a company once is
    remembered even when no CIN was supplied. Without this a name-only search
    would ask the same question on every run.
    """
    cin = normalise_cin(cin)
    if cin:
        return cin
    slug = re.sub(r"[^a-z0-9]", "", canonical_name(borrower_name or ""))
    return f"{NAME_PREFIX}{slug}" if slug else ""


def is_name_key(key: str) -> bool:
    return (key or "").startswith(NAME_PREFIX)


class ResolutionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.path, timeout=30)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resolutions (
                cin           TEXT NOT NULL,
                agency        TEXT NOT NULL,
                entity_name   TEXT NOT NULL,
                borrower_name TEXT DEFAULT '',
                confirmed_by  TEXT DEFAULT 'auto',
                updated_at    REAL,
                PRIMARY KEY (cin, agency)
            )
            """
        )
        # A rating document an agency hosts but does not expose through its own
        # search. CRISIL publishes 'Nxt-Infra Trust ... Crisil AAA/Stable' at a
        # stable URL while returning zero rows for every spelling of the name in
        # both its suggest index and its results endpoint - four such entities
        # have turned up so far. The document id cannot be derived, so no crawl
        # can reach it; recording the URL once is the only route to the rating.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS url_pins (
                cin           TEXT NOT NULL,
                agency        TEXT NOT NULL,
                url           TEXT NOT NULL,
                borrower_name TEXT DEFAULT '',
                note          TEXT DEFAULT '',
                updated_at    REAL,
                PRIMARY KEY (cin, agency)
            )
            """
        )
        self.conn.commit()

    # -- document URL pins ----------------------------------------------
    def set_url(self, cin: str, agency: str, url: str,
                borrower_name: str = "", note: str = "") -> bool:
        key = entity_key(cin, borrower_name)
        url = (url or "").strip()
        if not (key and agency and url.lower().startswith("http")):
            return False
        self.conn.execute(
            "INSERT OR REPLACE INTO url_pins "
            "(cin, agency, url, borrower_name, note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, agency, url, borrower_name, note, time.time()),
        )
        self.conn.commit()
        return True

    def urls(self, cin: str, borrower_name: str = "") -> dict[str, str]:
        """Pinned document URLs for one borrower, as {agency: url}."""
        key = entity_key(cin, borrower_name)
        if not key:
            return {}
        rows = self.conn.execute(
            "SELECT agency, url FROM url_pins WHERE cin = ?", (key,)
        ).fetchall()
        return {r["agency"]: r["url"] for r in rows}

    def delete_url(self, cin: str, agency: Optional[str] = None) -> int:
        cin = cin if is_name_key(cin) else normalise_cin(cin)
        if agency:
            cur = self.conn.execute(
                "DELETE FROM url_pins WHERE cin = ? AND agency = ?", (cin, agency))
        else:
            cur = self.conn.execute("DELETE FROM url_pins WHERE cin = ?", (cin,))
        self.conn.commit()
        return cur.rowcount

    def all_urls(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM url_pins ORDER BY borrower_name, agency").fetchall()
        return [dict(r) for r in rows]

    def get(self, cin: str, borrower_name: str = "") -> dict[str, str]:
        """All agency pins for one borrower, as {agency: entity_name}."""
        key = entity_key(cin, borrower_name)
        if not key:
            return {}
        rows = self.conn.execute(
            "SELECT agency, entity_name FROM resolutions WHERE cin = ?", (key,)
        ).fetchall()
        return {r["agency"]: r["entity_name"] for r in rows}

    def confirmed(self, cin: str, borrower_name: str = "") -> dict[str, str]:
        """Only the pins a person actually confirmed, as {agency: entity_name}.

        Strict mode collects against these alone. An 'auto' pin records what
        fuzzy matching believed last time, which is precisely the judgement that
        must not be trusted when the output replaces a manual process.
        """
        key = entity_key(cin, borrower_name)
        if not key:
            return {}
        rows = self.conn.execute(
            "SELECT agency, entity_name FROM resolutions "
            "WHERE cin = ? AND confirmed_by = 'user'",
            (key,),
        ).fetchall()
        return {r["agency"]: r["entity_name"] for r in rows}

    def set(
        self,
        cin: str,
        agency: str,
        entity_name: str,
        borrower_name: str = "",
        confirmed_by: str = "auto",
    ) -> None:
        key = entity_key(cin, borrower_name)
        if not (key and agency and entity_name):
            return
        # A human confirmation must never be silently downgraded by a later
        # automatic one.
        existing = self.conn.execute(
            "SELECT confirmed_by FROM resolutions WHERE cin = ? AND agency = ?", (key, agency)
        ).fetchone()
        if existing and existing["confirmed_by"] == "user" and confirmed_by != "user":
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO resolutions "
            "(cin, agency, entity_name, borrower_name, confirmed_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, agency, entity_name, borrower_name, confirmed_by, time.time()),
        )
        self.conn.commit()

    def delete(self, cin: str, agency: Optional[str] = None) -> int:
        # Name keys are stored lowercase; only a real CIN gets upper-cased.
        cin = cin if is_name_key(cin) else normalise_cin(cin)
        if agency:
            cur = self.conn.execute(
                "DELETE FROM resolutions WHERE cin = ? AND agency = ?", (cin, agency)
            )
        else:
            cur = self.conn.execute("DELETE FROM resolutions WHERE cin = ?", (cin,))
        self.conn.commit()
        return cur.rowcount

    def all(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM resolutions ORDER BY borrower_name, agency"
        ).fetchall()
        return [dict(r) for r in rows]
