"""Jobs for the job runner (`learn/jobs.py`), which the review pipeline also uses through `ports.jobs`. Pure data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


def _never() -> bool:
    return False


@dataclass(frozen=True)
class Job:
    id: str
    stage: str
    run: Callable[[], object]  # makes the job's call(s) and saves the result; safe to repeat
    done: Callable[[], bool] = _never  # True when the result is already saved: skip without calling
    pr: str | None = None


@dataclass
class RunReport:
    total: int = 0
    skipped: int = 0  # already done before this run
    completed: int = 0
    failed: dict[str, str] = field(default_factory=dict)  # job id -> error
    not_run: int = 0  # left over when the run stopped
    stopped: str | None = None
    resets_at: str | None = None
    waits: int = 0  # plan-window resets waited out
    batches: int = 0  # message batches the run sent (`[llm.anthropic] use_batches`)
    seconds: float = 0.0
    stop_error: BaseException | None = None  # the first error that stopped the run, to re-raise from a nested run

    @property
    def finished(self) -> bool:
        return self.stopped is None and not self.failed

    def summary(self) -> str:
        text = (f"{self.total} jobs: {self.skipped} already done, {self.completed} completed,"
                f" {len(self.failed)} failed, {self.not_run} not run ({self.seconds:.0f}s)")  # fmt: skip
        if self.waits:
            text += f"; waited out {self.waits} plan-window reset(s)"
        if self.batches:
            text += f"; {self.batches} message batch(es)"
        if self.stopped:
            text += f"; STOPPED: {self.stopped}" + (f"; resets at {self.resets_at}" if self.resets_at else "")
        return text

    def __add__(self, other: RunReport) -> RunReport:
        return RunReport(
            total=self.total + other.total,
            skipped=self.skipped + other.skipped,
            completed=self.completed + other.completed,
            failed={**self.failed, **other.failed},
            not_run=self.not_run + other.not_run,
            stopped=self.stopped or other.stopped,
            resets_at=self.resets_at or other.resets_at,
            waits=self.waits + other.waits,
            batches=self.batches + other.batches,
            seconds=self.seconds + other.seconds,
            stop_error=self.stop_error or other.stop_error,
        )
