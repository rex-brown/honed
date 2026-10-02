"""Runs idempotent LLM jobs at a fixed concurrency (ARCHITECTURE.md section 8).

A job whose result is already saved is skipped without a call, and a job whose model call is cached costs nothing,
so a re-run resumes where the last one stopped. A `StopRun` (a usage limit, the network, the call cap) stops the run
cleanly: no new job starts, the ones in flight finish, and the report says how far it got and when the limit resets.
Any other error fails just that job.

With a `ResetWait` (`--wait-for-reset`), a stop for a plan window alone doesn't end the run: once the jobs in flight
finish, the runner sleeps until the window resets plus a margin, keeping the heartbeat fresh, then re-arms the guard
and runs the stopped jobs again, one at a time until a live call has re-checked the plan, then at full concurrency.
Any other stop (overage, usage credits, an isolation breach, the call cap, the network) still ends the run at once,
and so does a reset further away than `max_wait_s`.

With a `CallBatch` (the `anthropic` backend's message batches), jobs run in passes around one batch each (`run_jobs`).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import deque
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Protocol

from honed.core.jobs import Job, RunReport
from honed.ports.llm import CallBatch, CallDeferred, PlanWindow, StopRun, UsageLimitReached

log = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> dt.datetime: ...  # timezone-aware

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class ResetWait:
    """Wait out plan-window stops instead of ending the run (`--wait-for-reset`)."""

    window: PlanWindow
    margin_s: float  # sleep until the reset plus this
    heartbeat_s: float  # rewrite the heartbeat this often while waiting
    max_wait_s: float = float("inf")  # a reset further away than this ends the run instead (`[llm.claude_code]`)
    clock: Clock = field(default_factory=SystemClock)


def run_jobs(
    jobs: Sequence[Job], *, concurrency: int, stop_on: tuple[type[BaseException], ...] = (StopRun,),
    wait_for_reset: ResetWait | None = None, batch: CallBatch | None = None,
) -> RunReport:  # fmt: skip
    """Run `jobs` with at most `concurrency` in flight. `stop_on` lists the errors that stop the whole run;
    `wait_for_reset` waits out the plan-window ones instead.

    With a `batch` (`[llm.anthropic] use_batches`), the jobs run in passes: in each pass a call that misses the call
    cache is queued and its job deferred (`CallDeferred`); the queue then goes out as one message batch, and the
    deferred jobs run again, finding their answers, until none is deferred. A job that makes several calls in turn
    (a review: intent, panel, verifier) takes one pass per step. Without a batch, a deferred job (a nested run's,
    inside a batched job) ends the run, and `stop_error` carries the deferral to the job around it."""
    started = time.monotonic()
    report = RunReport(total=len(jobs))
    queue = [job for job in jobs if not job.done()]
    report.skipped = len(jobs) - len(queue)
    while queue:
        if batch is not None:
            batch.collecting(True)
        try:
            deferred = _run(queue, report, concurrency, stop_on, wait_for_reset)
        finally:
            if batch is not None:
                batch.collecting(False)
        if not deferred:
            break
        if batch is None:  # a nested run inside a batched job: the job around it is deferred too
            report.stopped = report.stopped or f"{len(deferred)} job(s) deferred to a message batch"
            report.stop_error = report.stop_error or deferred[0][1]
            report.not_run += len(deferred)
            break
        if batch.pending() == 0:
            for job, error in deferred:
                report.failed[job.id] = f"deferred, but no call was queued for the batch: {error}"
            break
        try:
            summary = batch.flush()
        except stop_on as error:
            log.warning("stopping the run: %s: %s", type(error).__name__, error)
            report.stopped = f"{type(error).__name__}: {error}"
            report.stop_error = error
            report.not_run += len(deferred)
            break
        report.batches += 1
        log.info("message batch %d: %s; running %d deferred job(s) again", report.batches, summary, len(deferred))
        queue = [job for job, _ in deferred]
    report.seconds = time.monotonic() - started
    return report


def _run(queue_in: Sequence[Job], report: RunReport, concurrency: int, stop_on: tuple[type[BaseException], ...],
         wait_for_reset: ResetWait | None) -> list[tuple[Job, CallDeferred]]:  # fmt: skip
    """One pass over `queue_in`, adding to `report`; returns the jobs deferred to a batch (none once the run
    stopped: they count as not run)."""
    queue: deque[Job] = deque(queue_in)
    width = max(1, concurrency)
    in_flight: dict[Future[object], Job] = {}
    stops: list[tuple[Job, BaseException]] = []  # since the last (re)start; their results were not saved
    deferred: list[tuple[Job, CallDeferred]] = []
    probing = False  # after a wait: one job at a time until a live call has re-checked the plan
    too_far = ""
    with ThreadPoolExecutor(max_workers=width) as pool:

        def fill() -> None:
            while not stops and len(in_flight) < (1 if probing else width) and queue:
                job = queue.popleft()
                in_flight[pool.submit(job.run)] = job

        fill()
        while in_flight:
            finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in finished:
                job = in_flight.pop(future)
                error = future.exception()
                if error is None:
                    report.completed += 1
                elif isinstance(error, CallDeferred):
                    deferred.append((job, error))
                elif isinstance(error, stop_on):
                    if not stops:
                        log.warning("stopping the run: %s: %s", type(error).__name__, error)
                    stops.append((job, error))
                else:
                    report.failed[job.id] = f"{type(error).__name__}: {error}"
                    log.warning("%s failed: %s", job.id, report.failed[job.id])
            if probing and wait_for_reset is not None and wait_for_reset.window.rechecked():
                probing = False
            if stops and not in_flight:
                until = _resumable_at(stops, wait_for_reset)
                if until is not None and wait_for_reset is not None:
                    too_far = _too_far(until, wait_for_reset)
                    if too_far:
                        log.warning(too_far)
                        until = None
                if until is None or wait_for_reset is None:
                    break
                _wait_out(until, wait_for_reset)
                report.waits += 1
                queue.extendleft(reversed([job for job, _ in stops]))
                stops.clear()
                probing = True
            fill()
    if stops:
        first = stops[0][1]
        report.stopped = f"{type(first).__name__}: {first}" + (f"; {too_far}" if too_far else "")
        report.stop_error = first
        limits = [e.resets_at for _, e in stops if isinstance(e, UsageLimitReached) and e.resets_at]
        report.resets_at = limits[-1] if limits else None
        report.not_run += len(stops) + len(queue) + len(deferred)
        return []
    report.not_run += len(queue)
    return deferred


def _too_far(until: dt.datetime, waiter: ResetWait) -> str:
    """Why the run won't wait for a reset at `until` (beyond `max_wait_s`), or ""."""
    target = max(until, waiter.clock.now()) + dt.timedelta(seconds=waiter.margin_s)
    wait_s = (target - waiter.clock.now()).total_seconds()
    if wait_s <= waiter.max_wait_s:
        return ""
    return (f"not waiting: the plan window resets at {until.isoformat()}, {wait_s / 3600:.1f}h away, beyond "
            f"max_wait_s ({waiter.max_wait_s / 3600:.1f}h); run again after the reset to resume")  # fmt: skip


def _resumable_at(stops: Sequence[tuple[Job, BaseException]], waiter: ResetWait | None) -> dt.datetime | None:
    """When the run may resume: every stop is a usage limit and the guard stopped for a plan window alone."""
    if waiter is None or not all(isinstance(error, UsageLimitReached) for _, error in stops):
        return None
    resets = waiter.window.resumable_at()
    if resets is None:
        return None
    try:
        until = dt.datetime.fromisoformat(resets)
    except ValueError:
        log.warning("unreadable plan reset time %r: not waiting", resets)
        return None
    return until if until.tzinfo else until.replace(tzinfo=dt.UTC)


def _wait_out(resets: dt.datetime, waiter: ResetWait) -> None:
    """Sleep until `resets` plus the margin (at least the margin from now), rewriting the heartbeat as it goes, then
    re-arm the guard."""
    clock = waiter.clock
    target = max(resets, clock.now()) + dt.timedelta(seconds=waiter.margin_s)
    stamp = target.isoformat()
    log.warning("plan window stop: waiting until %s, then resuming", stamp)
    while (left := (target - clock.now()).total_seconds()) > 0:
        waiter.window.wait_until(stamp)
        clock.sleep(min(waiter.heartbeat_s, left))
    waiter.window.resume()
    log.warning("plan window reset: resuming; the next live call re-checks the plan")
