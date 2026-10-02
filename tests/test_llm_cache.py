"""The call cache, the usage ledger, the replay backend and the job runner. No network."""

from __future__ import annotations

import threading
import time

import pytest

from honed.adapters.cached_llm import CachedLLM
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.replay_llm import ReplayLLM
from honed.learn.jobs import Job, run_jobs
from honed.ports.llm import LLMCall, LLMError, LLMResult, ReplayMiss, Usage, UsageLimitReached


class ScriptedLLM:
    """Answers every call with `answer(call)`; counts calls."""

    def __init__(self, answer=lambda call: {"echo": call.user}) -> None:
        self.answer = answer
        self.calls: list[LLMCall] = []
        self._lock = threading.Lock()

    def complete(self, call: LLMCall) -> LLMResult:
        with self._lock:
            self.calls.append(call)
        data = self.answer(call)
        return LLMResult(text=str(data), data=data, usage=Usage(10, 20, 30, 40, 0.5, 1.0), model=call.model)


@pytest.fixture
def calls(tmp_path):
    store = SqliteCallStore(tmp_path / "db.sqlite")
    yield store
    store.close()


def mk(user="u", *, sample=0, stage="addressed", pr="o/r#1", schema=None) -> LLMCall:
    return LLMCall(model="m", system="s", user=user, schema=schema, sample=sample, stage=stage, pr=pr)


def test_the_cache_key_ignores_accounting_tags_but_not_the_sample():
    assert mk(stage="a", pr="x").key == mk(stage="b", pr=None).key
    assert mk(sample=1).key != mk().key
    assert mk(schema={"b": 1, "a": 2}).key == mk(schema={"a": 2, "b": 1}).key != mk().key
    assert LLMCall("m", "s", "u", effort="high").key != LLMCall("m", "s", "u").key


def test_a_cached_call_is_not_repeated_and_both_are_in_the_ledger(calls):
    inner = ScriptedLLM()
    llm = CachedLLM(inner, calls, run_id="r1")
    first = llm.complete(mk())
    second = llm.complete(mk(stage="audit"))
    assert len(inner.calls) == 1 and first.data == second.data == {"echo": "u"}
    assert not first.cached and second.cached and second.usage.output_tokens == 20
    live, cached = calls.ledger()
    assert (live.cached, live.ok, live.output_tokens, live.cost_usd, live.stage) == (False, True, 20, 0.5, "addressed")
    assert (cached.cached, cached.output_tokens, cached.cost_usd, cached.stage) == (True, 0, 0.0, "audit")


def test_failed_calls_are_recorded_and_not_cached(calls):
    def boom(call):
        raise LLMError("bad")

    llm = CachedLLM(ScriptedLLM(boom), calls, run_id="r1")
    with pytest.raises(LLMError):
        llm.complete(mk())
    (entry,) = calls.ledger()
    assert not entry.ok and entry.error == "bad" and calls.get(mk().key) is None


def test_stop_run_errors_pass_through_unrecorded(calls):
    def limited(call):
        raise UsageLimitReached("five-hour limit", resets_at="2026-09-30T00:00:00+00:00")

    with pytest.raises(UsageLimitReached):
        CachedLLM(ScriptedLLM(limited), calls, run_id="r1").complete(mk())
    assert calls.ledger() == []


def test_replay_answers_only_from_the_cache(calls):
    CachedLLM(ScriptedLLM(), calls, run_id="r1").complete(mk("seen"))
    replay = ReplayLLM(calls)
    assert replay.complete(mk("seen")).data == {"echo": "seen"}
    with pytest.raises(ReplayMiss, match="addressed o/r#1"):
        replay.complete(mk("unseen"))


# ---- job runner --------------------------------------------------------------------------------------------


def test_jobs_skip_done_work_and_resume_after_a_stop(calls):
    saved: dict[str, object] = {}
    budget = {"left": 3}

    def answer(call):
        if budget["left"] == 0:
            raise UsageLimitReached("limit", resets_at="2026-09-30T05:00:00+00:00")
        budget["left"] -= 1
        return {"n": call.user}

    llm = CachedLLM(ScriptedLLM(answer), calls, run_id="r1")

    def job(i: int) -> Job:
        def run():
            saved[f"j{i}"] = llm.complete(mk(str(i))).data

        return Job(f"j{i}", "addressed", run, done=lambda: f"j{i}" in saved)

    jobs = [job(i) for i in range(6)]
    report = run_jobs(jobs, concurrency=1)
    assert report.completed == 3 and report.stopped and report.resets_at == "2026-09-30T05:00:00+00:00"
    assert report.not_run == 3 and len(saved) == 3
    budget["left"] = 10
    again = run_jobs(jobs, concurrency=2)
    assert (again.skipped, again.completed, again.stopped) == (3, 3, None) and len(saved) == 6


def test_a_failing_job_does_not_stop_the_others():
    def bad():
        raise ValueError("unparseable answer")

    report = run_jobs([Job("a", "s", bad), Job("b", "s", lambda: None)], concurrency=2)
    assert report.completed == 1 and report.failed == {"a": "ValueError: unparseable answer"} and not report.stopped


def test_concurrency_is_respected():
    active, peak = [0], [0]
    lock = threading.Lock()

    def work():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1

    report = run_jobs([Job(str(i), "s", work) for i in range(8)], concurrency=2)
    assert report.completed == 8 and peak[0] == 2


def test_usage_tables_split_live_and_cached_calls_by_stage_pr_and_run(calls):
    from honed.learn import usage

    llm = CachedLLM(ScriptedLLM(), calls, run_id="r1")
    llm.complete(mk("a", stage="addressed", pr="o/r#1"))
    llm.complete(mk("b", stage="gold", pr="o/r#2"))
    CachedLLM(ScriptedLLM(), calls, run_id="r2").complete(mk("a", stage="addressed", pr="o/r#1"))
    by_stage, by_pr, by_run = usage.tables(calls.ledger())
    rows = {r[0]: r for r in by_stage.rows}
    assert rows["addressed"][1:4] == ("1", "1", "0") and rows["total"][1] == "2" and rows["total"][7] == "1.00"
    assert {r[0] for r in by_pr.rows} == {"o/r#1", "o/r#2", "total"}
    assert [r[0] for r in by_run.rows] == ["r1", "r2"]
    assert usage.tables(calls.ledger(), "r2")[0].rows[0][1:3] == ("0", "1")
