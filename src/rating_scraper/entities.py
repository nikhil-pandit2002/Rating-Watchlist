"""The watchlist of entities whose ratings are collected on every run.

Distinct from ResolutionStore, which records *which agency entity* a watchlist
row resolves to. This is simply the list itself: the trusts, InvITs, municipal
bodies and foundations that Saverisk does not cover and that were previously
looked up by hand.

The list is additive and persistent. An entity added through the frontend is
picked up by the next run without anyone re-uploading a sheet, which is the
whole point - the collection is recurring and the list only ever grows.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .normalize import canonical_name, is_non_corporate

# Kept deliberately coarse. The type drives reporting and sanity checks, not
# matching behaviour - normalize.is_non_corporate() reads the name itself, so a
# mistyped category can never change which document is fetched.
ENTITY_TYPES = ("Trust", "InvIT", "Municipal Body", "Foundation", "Society", "Other")


def guess_entity_type(name: str) -> str:
    """Best-effort category from the name, so the user rarely has to pick one."""
    n = (name or "").lower()
    # Most Indian InvITs are registered as '<name> Infrastructure Trust' rather
    # than spelling out 'Infrastructure Investment Trust' - IndiGrid, Maple,
    # Altius Telecom, TVS and Highways all follow that shorter form.
    if re.search(r"\binvit\b|\breit\b|infra(structure)?\s+(investment\s+)?trust", n):
        return "InvIT"
    # 'Greater Chennai Corporation' and 'Brihanmumbai ... Corporation' are civic
    # bodies that never carry the word 'municipal' in their registered name.
    if re.search(r"nagar\s+nigam|municipal|corporation\s+of|panchayat|parishad"
                 r"|development\s+authority|metropolitan|city\s+council"
                 r"|greater\s+\w+\s+corporation|city\s+corporation"
                 r"|capital\s+region", n):
        return "Municipal Body"
    if re.search(r"\bfoundation\b", n):
        return "Foundation"
    if re.search(r"\bsociety\b|\bsangh\b|\bsamiti\b", n):
        return "Society"
    if re.search(r"\btrust\b|\bfund\b", n):
        return "Trust"
    return "Other"


def entity_slug(name: str) -> str:
    """Stable dedup key so the same entity cannot be added twice."""
    return re.sub(r"[^a-z0-9]", "", canonical_name(name or ""))


@dataclass
class Entity:
    id: int
    name: str
    entity_type: str
    notes: str
    active: bool
    added_at: float
    updated_at: float

    @property
    def is_non_corporate(self) -> bool:
        return is_non_corporate(self.name)


class EntityStore:
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
            CREATE TABLE IF NOT EXISTS entities (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL UNIQUE,
                name        TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'Other',
                notes       TEXT NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                added_at    REAL,
                updated_at  REAL
            )
            """
        )
        self.conn.commit()

    # -- writes ---------------------------------------------------------
    def add(self, name: str, entity_type: str = "", notes: str = "") -> tuple[Optional[Entity], str]:
        """Add one entity. Returns (entity, status) where status is
        'added' | 'exists' | 'reactivated' | 'invalid'."""
        name = " ".join((name or "").split())
        slug = entity_slug(name)
        if not slug:
            return None, "invalid"

        etype = entity_type if entity_type in ENTITY_TYPES else guess_entity_type(name)
        now = time.time()
        row = self.conn.execute("SELECT * FROM entities WHERE slug = ?", (slug,)).fetchone()
        if row:
            # Re-adding a deactivated entity should bring it back rather than
            # fail, which is what a user actually means by adding it again.
            if not row["active"]:
                self.conn.execute(
                    "UPDATE entities SET active = 1, updated_at = ? WHERE id = ?", (now, row["id"])
                )
                self.conn.commit()
                return self.get(row["id"]), "reactivated"
            return self._to_entity(row), "exists"

        cur = self.conn.execute(
            "INSERT INTO entities (slug, name, entity_type, notes, active, added_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (slug, name, etype, notes or "", now, now),
        )
        self.conn.commit()
        return self.get(cur.lastrowid), "added"

    def add_many(self, names: list[str]) -> dict[str, list[str]]:
        """Bulk add. Returns names grouped by what happened to each."""
        out: dict[str, list[str]] = {"added": [], "exists": [], "reactivated": [], "invalid": []}
        for n in names:
            _, status = self.add(n)
            out[status].append(n)
        return out

    def update(self, entity_id: int, **fields) -> Optional[Entity]:
        allowed = {"name", "entity_type", "notes", "active"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "name":
                v = " ".join(str(v).split())
                if not entity_slug(v):
                    continue
                sets.append("slug = ?")
                vals.append(entity_slug(v))
            if k == "active":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return self.get(entity_id)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(entity_id)
        self.conn.execute(f"UPDATE entities SET {', '.join(sets)} WHERE id = ?", vals)
        self.conn.commit()
        return self.get(entity_id)

    def set_active(self, entity_id: int, active: bool) -> Optional[Entity]:
        return self.update(entity_id, active=active)

    def delete(self, entity_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # -- reads ----------------------------------------------------------
    @staticmethod
    def _to_entity(row: sqlite3.Row) -> Entity:
        return Entity(
            id=row["id"],
            name=row["name"],
            entity_type=row["entity_type"],
            notes=row["notes"],
            active=bool(row["active"]),
            added_at=row["added_at"] or 0.0,
            updated_at=row["updated_at"] or 0.0,
        )

    def get(self, entity_id: int) -> Optional[Entity]:
        row = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return self._to_entity(row) if row else None

    def find(self, name: str) -> Optional[Entity]:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE slug = ?", (entity_slug(name),)
        ).fetchone()
        return self._to_entity(row) if row else None

    def list(self, active_only: bool = False) -> list[Entity]:
        sql = "SELECT * FROM entities"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._to_entity(r) for r in self.conn.execute(sql).fetchall()]

    def count(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT entity_type, COUNT(*) n FROM entities WHERE active = 1 GROUP BY entity_type"
        ).fetchall()
        return {r["entity_type"]: r["n"] for r in rows}
