"""Polite, cached, retrying HTTP layer.

At 500+ borrowers x 6 agencies this module is what keeps the run from
hammering the agency sites and from re-downloading work it already has.
Every response is cached on disk, so re-running `parse` after a parser fix
costs zero network requests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str
    from_cache: bool = False

    @property
    def text(self) -> str:
        enc = "utf-8"
        if "charset=" in self.content_type:
            enc = self.content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return self.content.decode(enc, errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)


class RateLimiter:
    """Minimum delay between requests, tracked per host."""

    def __init__(self, delay: float = 1.5, jitter: float = 0.5):
        self.delay = delay
        self.jitter = jitter
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            last = self._last.get(host, 0.0)
            gap = time.monotonic() - last
            need = self.delay + random.uniform(0, self.jitter) - gap
            if need > 0:
                time.sleep(need)
            self._last[host] = time.monotonic()


class BlobCache:
    """Content-addressed cache: URL -> bytes, with an SQLite index."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "cache.db"
        self._local = threading.local()
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fetches (
                url          TEXT PRIMARY KEY,
                status       INTEGER,
                content_type TEXT,
                sha          TEXT,
                fetched_at   REAL
            )
            """
        )
        self.conn.commit()

    def _blob_path(self, sha: str) -> Path:
        return self.blobs / sha[:2] / f"{sha}.bin"

    def get(self, url: str, max_age: Optional[float] = None) -> Optional[FetchResult]:
        row = self.conn.execute(
            "SELECT status, content_type, sha, fetched_at FROM fetches WHERE url = ?", (url,)
        ).fetchone()
        if not row:
            return None
        status, ctype, sha, fetched_at = row
        if max_age is not None and (time.time() - fetched_at) > max_age:
            return None
        path = self._blob_path(sha)
        if not path.exists():
            return None
        return FetchResult(url, status, path.read_bytes(), ctype or "", from_cache=True)

    def put(self, url: str, status: int, content: bytes, content_type: str) -> None:
        sha = hashlib.sha256(content).hexdigest()
        path = self._blob_path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        self.conn.execute(
            "INSERT OR REPLACE INTO fetches (url, status, content_type, sha, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (url, status, content_type, sha, time.time()),
        )
        self.conn.commit()


class Fetcher:
    """Session wrapper: rate limit + retry + cache."""

    def __init__(
        self,
        cache_dir: Path,
        delay: float = 1.5,
        retries: int = 3,
        timeout: int = 60,
        user_agent: str = DEFAULT_UA,
    ):
        self.cache = BlobCache(cache_dir)
        self.limiter = RateLimiter(delay)
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        use_cache: bool = True,
        max_age: Optional[float] = None,
        headers: Optional[dict] = None,
        **kwargs,
    ) -> FetchResult:
        full = url
        if params:
            req = requests.Request("GET", url, params=params).prepare()
            full = req.url

        if use_cache:
            hit = self.cache.get(full, max_age=max_age)
            if hit is not None:
                log.debug("cache hit %s", full)
                return hit

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self.limiter.wait(full)
            try:
                r = self.session.get(full, timeout=self.timeout, headers=headers, **kwargs)
                ctype = r.headers.get("content-type", "")
                # Retry only on transient server-side failures.
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = 2 ** attempt + random.uniform(0, 1)
                    log.warning("%s -> %s, retry in %.1fs", full, r.status_code, wait)
                    time.sleep(wait)
                    continue
                result = FetchResult(full, r.status_code, r.content, ctype)
                if result.ok and use_cache:
                    self.cache.put(full, r.status_code, r.content, ctype)
                return result
            except requests.RequestException as e:
                last_err = e
                if attempt < self.retries:
                    wait = 2 ** attempt + random.uniform(0, 1)
                    log.warning("%s failed (%s), retry in %.1fs", full, e, wait)
                    time.sleep(wait)

        log.error("giving up on %s: %s", full, last_err)
        return FetchResult(full, 0, b"", "")

    def post(
        self,
        url: str,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        **kwargs,
    ) -> FetchResult:
        """POSTs are never cached - they are form round-trips (ASP.NET, tokens)."""
        for attempt in range(1, self.retries + 1):
            self.limiter.wait(url)
            try:
                r = self.session.post(
                    url, data=data, timeout=self.timeout, headers=headers, **kwargs
                )
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                return FetchResult(url, r.status_code, r.content, r.headers.get("content-type", ""))
            except requests.RequestException as e:
                log.warning("POST %s failed: %s", url, e)
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
        return FetchResult(url, 0, b"", "")
