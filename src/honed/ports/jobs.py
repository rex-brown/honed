"""JobRunner: runs idempotent jobs at a fixed concurrency (`learn/jobs.py` `run_jobs`), for services that can't
import `learn` (the review pipeline's parallel panel)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from honed.core.jobs import Job, RunReport


class JobRunner(Protocol):
    def __call__(self, jobs: Sequence[Job], *, concurrency: int) -> RunReport:
        """Run `jobs`; a run-stopping error (a usage limit, the call cap) ends the run and is in `stop_error`."""
        ...
