"""Background job runner.

A 500-borrower run takes many minutes, far longer than any browser will wait on
a request. Jobs therefore run on a worker thread and the page polls for status.
Results are written to disk so a download survives a page reload.
"""
from __future__ import annotations

import logging
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from rating_scraper.agencies import ADAPTERS
from rating_scraper.excel_export import write_workbook
from rating_scraper.http_client import Fetcher
from rating_scraper.models import Company
from rating_scraper.pipeline import Pipeline
from rating_scraper.resolutions import ResolutionStore

log = logging.getLogger(__name__)

# Strict mode demands a near-exact name match. The default 88 accepts an issuer
# and its similarly-named SPVs; 96 does not.
STRICT_THRESHOLD = 96.0


@dataclass
class Job:
    id: str
    companies: list[Company]
    agencies: list[str]
    strict: bool = False
    status: str = "queued"          # queued | running | done | failed
    done: int = 0
    total: int = 0
    label: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    output_path: Optional[Path] = None
    summary: dict = field(default_factory=dict)
    qa: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        pct = int(100 * self.done / self.total) if self.total else 0
        return {
            "id": self.id,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "percent": pct,
            "label": self.label,
            "error": self.error,
            "created_at": self.created_at,
            "companies": len(self.companies),
            "agencies": self.agencies,
            "strict": self.strict,
            "summary": self.summary,
            "qa": self.qa,
            "has_result": bool(self.output_path and Path(self.output_path).exists()),
        }


class JobManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.out_dir = self.data_dir / "runs"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.resolutions = ResolutionStore(self.data_dir / "resolutions.db")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]

    def submit(self, companies: list[Company], agencies: list[str], strict: bool = False) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            companies=companies,
            agencies=agencies,
            strict=strict,
            total=len(companies) * len(agencies),
        )
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
        try:
            fetcher = Fetcher(cache_dir=self.cache_dir)
            adapters = []
            for key in job.agencies:
                adapter = ADAPTERS[key](fetcher)
                if job.strict:
                    adapter.match_threshold = STRICT_THRESHOLD
                adapters.append(adapter)

            def progress(done: int, total: int, label: str) -> None:
                job.done, job.total, job.label = done, total, label

            result = Pipeline(
                adapters, resolutions=self.resolutions, progress=progress
            ).run(job.companies)

            out = self.out_dir / f"ratings_{job.id}.xlsx"
            write_workbook(result, out)
            job.output_path = out
            job.summary = result.summary()
            job.qa = [
                {
                    "borrower_name": q.borrower_name,
                    "cin": q.cin,
                    "agency": q.agency,
                    "status": q.status,
                    "detail": q.detail,
                    "candidates": list(q.candidates or []),
                }
                for q in result.qa
            ]
            job.done = job.total
            job.status = "done"
            log.info("job %s finished: %d rows", job.id, job.summary.get("records", 0))
        except Exception as e:
            job.status = "failed"
            job.error = f"{type(e).__name__}: {e}"
            log.error("job %s failed\n%s", job.id, traceback.format_exc())
