"""`--wait-for-reset`: the job runner waits out a plan-window stop on a fake clock, keeps the heartbeat fresh, re-checks
the plan with one call, and stops at once for anything a reset doesn't clear. No model calls."""

from __future__ import annotations

import datetime as dt
import threading

import pytest

import claude_events as ev
from honed.adapters.plan_guard import PlanGuard
from honed.learn.jobs import Job, ResetWait, run_jobs
from honed.ports.llm import UsageLimitReached

FIVE_HOUR_RESET = dt.datetime.fromtimestamp(1790733000, dt.UTC)  # `claude_events.rate_limit` windows
WEEKLY_RESET = dt.datetime.fromtimestamp(1791248400, dt.UTC)
MARGIN = dt.timedelta(seconds=120)


class FakeClock:
    def __init__(self, now: dt.datetime) -> None:
        self.t = now
        self.sleeps: list[float] = []

    def now(self) -> dt.datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += dt.timedelta(seconds=seconds)


def info(**rate) -> dict:
    return ev.rate_limit(**rate)["rate_limit_info"]


class Rig:
    """A real PlanGuard and a stand-in backend: each job makes one guarded call that reports a scripted reading."""

    def __init__(self, readings: dict[str, dict | None] | None = None, *, cap: int = 50,
                 together: set[str] = frozenset()) -> None:  # fmt: skip
        self.beats: list[dict] = []
        self.barrier = threading.Barrier(len(together), timeout=5) if together else None
        self.together = together  # jobs that must be in flight at the same time
        self.guard = PlanGuard(cap=cap, stop_at_utilization=0.8, stop_at_weekly_utilization=0.85, require_signal=True,
                               write_status=self.beats.append)  # fmt: skip
        self.clock = FakeClock(FIVE_HOUR_RESET - dt.timedelta(hours=1))
        self.readings = readings or {}
        self.events: list[str] = []
        self.done: set[str] = set()
        self._lock = threading.Lock()

    def jobs(self, n: int) -> list[Job]:
        def job(name: str) -> Job:
            def run() -> None:
                self.guard.before_call()
                with self._lock:
                    self.events.append(f"start {name}")
                reading = self.readings.pop(name, info())  # each job's reading, once; later calls are fine
                self.guard.observe(reading)
                if name in self.together:
                    self.barrier.wait()  # BrokenBarrierError (a failed job) unless they run concurrently
                with self._lock:
                    self.events.append(f"end {name}")
                    self.done.add(name)

            return Job(name, "addressed", run, done=lambda: name in self.done)

        return [job(f"j{i}") for i in range(1, n + 1)]

    def wait(self) -> ResetWait:
        return ResetWait(self.guard, margin_s=120, heartbeat_s=600, clock=self.clock)


def test_a_window_stop_is_waited_out_then_the_run_resumes():
    rig = Rig({"j2": info(five_hour=0.85)})
    report = run_jobs(rig.jobs(5), concurrency=1, wait_for_reset=rig.wait())
    assert (report.completed, report.stopped, report.not_run, report.waits) == (5, None, 0, 1)
    assert rig.done == {"j1", "j2", "j3", "j4", "j5"}  # j2's answer was kept; j3 ran after the wait
    target = FIVE_HOUR_RESET + MARGIN
    assert rig.clock.now() == target and max(rig.clock.sleeps) <= 600  # slept in heartbeat-sized steps
    waiting = [b for b in rig.beats if b["waiting_until"]]
    assert len(waiting) == len(rig.clock.sleeps) >= 6  # the heartbeat is rewritten before every step
    assert {b["waiting_until"] for b in waiting} == {target.isoformat()}
    assert waiting[0]["stopped"] == "plan utilization 0.85 >= 0.80 (five_hour)"
    assert rig.beats[-1]["waiting_until"] is None and rig.beats[-1]["stopped"] is None
    assert set(rig.beats[-1]) >= {"updated_at", "calls", "cap", "rate_limit_status", "warning", "utilization",
                                  "window_utilizations", "resets_at", "overage", "stopped"}  # fmt: skip


def test_a_weekly_stop_resumes_at_the_weekly_reset():
    rig = Rig({"j2": info(five_hour=0.3, seven_day=0.86)})
    report = run_jobs(rig.jobs(3), concurrency=1, wait_for_reset=rig.wait())
    assert (report.completed, report.waits, report.stopped) == (3, 1, None)
    assert rig.clock.now() == WEEKLY_RESET + MARGIN
    waiting = [b for b in rig.beats if b["waiting_until"]]
    assert waiting[0]["stopped"] == "plan utilization 0.86 >= 0.85 (seven_day)"
    assert waiting[0]["resets_at"] == WEEKLY_RESET.isoformat()
    assert waiting[0]["window_utilizations"] == {"five_hour": 0.3, "seven_day": 0.86}


def test_an_allowed_warning_does_not_stop_the_run():
    rig = Rig({"j2": info(status="allowed_warning", seven_day=0.63), "j3": info(seven_day=0.82)})
    report = run_jobs(rig.jobs(4), concurrency=1, wait_for_reset=rig.wait())
    assert (report.completed, report.waits, report.stopped, report.not_run) == (4, 0, None, 0)
    assert rig.clock.sleeps == [] and not any(b["stopped"] for b in rig.beats)
    assert [b["warning"] for b in rig.beats if b["rate_limit_status"] == "allowed_warning"] == [True]


def test_after_the_wait_one_call_rechecks_the_plan_before_full_concurrency():
    rig = Rig(together={"j2", "j3"})
    rig.guard.observe(info(status="rejected"))  # tripped before the run's first job
    report = run_jobs(rig.jobs(4), concurrency=2, wait_for_reset=rig.wait())
    assert (report.completed, report.waits, report.stopped, report.failed) == (4, 1, None, {})
    assert rig.events[:2] == ["start j1", "end j1"]  # the probe runs alone, then j2 and j3 run at once
    assert rig.clock.now() == FIVE_HOUR_RESET + MARGIN


def test_a_probe_that_trips_again_waits_again_for_the_later_reset():
    rig = Rig({"j2": info(five_hour=0.85), "j3": info(five_hour=0.2, seven_day=0.9)})
    report = run_jobs(rig.jobs(4), concurrency=1, wait_for_reset=rig.wait())
    assert (report.completed, report.waits, report.stopped) == (4, 2, None)
    assert rig.clock.now() == WEEKLY_RESET + MARGIN


def test_a_limit_error_with_a_reset_time_is_waited_out():
    rig = Rig()
    rig.guard.stop("usage or rate limit: You've hit your limit", resets_at="2026-09-30T01:50:00Z", window=True)
    report = run_jobs(rig.jobs(2), concurrency=1, wait_for_reset=rig.wait())
    assert (report.completed, report.waits) == (2, 1) and rig.clock.now() == FIVE_HOUR_RESET + MARGIN


def test_a_reset_time_in_the_past_still_waits_the_margin():
    rig = Rig()
    rig.guard.stop("usage or rate limit", resets_at=(rig.clock.now() - dt.timedelta(hours=2)).isoformat(),
                   window=True)  # fmt: skip
    start = rig.clock.now()
    report = run_jobs(rig.jobs(1), concurrency=1, wait_for_reset=rig.wait())
    assert report.completed == 1 and rig.clock.now() - start == MARGIN


@pytest.mark.parametrize(
    ("trip", "reason"),
    [
        (lambda g: g.observe(info(overage_status="allowed")), "overage is available"),
        (lambda g: g.observe(info(using_overage=True)), "overage"),
        (lambda g: g.observe(info(), fallback_credit={"used": True}), "usage-credit"),
        (lambda g: g.observe(info(five_hour=0.85, overage_status="allowed")), "overage is available"),
        (lambda g: (g.observe(info(five_hour=0.85)), g.observe(info(overage_status="allowed"))), "then overage"),
        (lambda g: g.observe(info(status="blocked")), "plan status 'blocked'"),
        (lambda g: g.observe(info(status="allowed_warning", seven_day=0.9, overage_status="allowed")), "overage"),
        (lambda g: g.observe(None), "no plan-usage signal"),
        (lambda g: g.stop("isolation breach: tools ['Bash']"), "isolation breach"),
        (lambda g: g.stop("network unavailable: ENOTFOUND"), "network"),
        (lambda g: g.stop("usage or rate limit", window=True), "usage or rate limit"),  # no reset time: can't wait
    ],
)
def test_stops_a_reset_does_not_clear_end_the_run_without_waiting(trip, reason):
    rig = Rig()
    trip(rig.guard)
    assert rig.guard.resumable_at() is None
    report = run_jobs(rig.jobs(3), concurrency=2, wait_for_reset=rig.wait())
    assert report.stopped and reason in report.stopped and report.waits == 0 and report.not_run == 3
    assert rig.clock.sleeps == [] and not any(b["waiting_until"] for b in rig.beats)


def test_the_call_cap_ends_the_run_without_waiting():
    rig = Rig(cap=2)
    report = run_jobs(rig.jobs(4), concurrency=1, wait_for_reset=rig.wait())
    assert report.completed == 2 and "CallCapReached" in report.stopped and report.waits == 0
    assert rig.clock.sleeps == []


def test_without_the_flag_a_window_stop_ends_the_run():
    rig = Rig({"j1": info(five_hour=0.85)})
    report = run_jobs(rig.jobs(3), concurrency=1)
    assert report.completed == 1 and report.not_run == 2 and report.waits == 0
    assert report.resets_at == FIVE_HOUR_RESET.isoformat() and rig.clock.sleeps == []


class ReaderGone(Exception):
    pass


def test_a_stop_from_outside_the_guard_ends_the_run_even_when_the_guard_could_wait():
    rig = Rig()
    rig.guard.observe(info(five_hour=0.85))
    assert rig.guard.resumable_at() is not None

    def limited() -> None:
        rig.guard.before_call()

    def gone() -> None:
        raise ReaderGone("clone missing")

    jobs = [Job("x", "addressed", limited), Job("y", "addressed", gone)]
    report = run_jobs(jobs, concurrency=2, stop_on=(UsageLimitReached, ReaderGone), wait_for_reset=rig.wait())
    assert report.stopped and report.waits == 0 and report.not_run == 2 and rig.clock.sleeps == []
