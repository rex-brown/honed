"""`--wait-for-reset` never waits longer than `[llm.claude_code] max_wait_s`: a reset further away ends the run
cleanly, with the reason in the report. No model calls."""

from __future__ import annotations

from honed import config
from honed.learn.jobs import ResetWait, run_jobs
from reviewkit import ROOT
from test_wait_for_reset import FIVE_HOUR_RESET, MARGIN, Rig, info


def test_a_reset_beyond_max_wait_ends_the_run_without_waiting():
    rig = Rig({"j2": info(five_hour=0.3, seven_day=0.9)})  # the weekly window: days away
    wait = ResetWait(rig.guard, margin_s=120, heartbeat_s=600, max_wait_s=6 * 3600, clock=rig.clock)
    report = run_jobs(rig.jobs(4), concurrency=1, wait_for_reset=wait)
    assert (report.completed, report.waits, report.not_run) == (2, 0, 2) and rig.clock.sleeps == []
    assert "not waiting" in report.stopped and "max_wait_s (6.0h)" in report.stopped
    assert report.resets_at is not None


def test_a_reset_within_max_wait_is_waited_out():
    rig = Rig({"j2": info(five_hour=0.85)})  # an hour away
    wait = ResetWait(rig.guard, margin_s=120, heartbeat_s=600, max_wait_s=6 * 3600, clock=rig.clock)
    report = run_jobs(rig.jobs(3), concurrency=1, wait_for_reset=wait)
    assert (report.completed, report.waits, report.stopped) == (3, 1, None)
    assert rig.clock.now() == FIVE_HOUR_RESET + MARGIN


def test_the_default_max_wait_is_six_hours():
    assert config.load(ROOT / "honed.toml").llm.claude_code.max_wait_s == 6 * 3600
