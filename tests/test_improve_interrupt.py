"""An interrupt (SIGINT, Ctrl-C) during `honed improve`: the candidate in flight is recorded as incomplete, the
round's bookkeeping (stored records, decision-log rows) is finished, and the loop stops. No model calls."""

from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from evalkit import build_store
from honed.adapters.policy_dir import PolicyDirectory
from honed.core.improve import Outcome
from honed.learn.improve import INTERRUPTED, ImproveLoop
from reviewkit import ROOT
from test_improve import LoopLLM, _options, _round_answers, _sensitivity_rows, _services


@pytest.fixture
def rig(tmp_path):
    store = build_store(tmp_path, humans=("ann", "bob"))
    directory = PolicyDirectory(tmp_path / "policy")
    shutil.copytree(ROOT / "policy", directory.path)
    for row in _sensitivity_rows(separates=True, min_gain=0.01):
        store.add_decision(replace(row, id=None))
    yield store, directory
    store.close()


def test_an_interrupt_during_a_candidates_evaluation_records_it_and_finishes_the_round(rig):
    store, directory = rig
    services = _services(store, directory, LoopLLM(_round_answers()))
    incumbent = services.codec.parse(directory.files()).content_hash
    evaluate = services.evaluate

    def interrupted(policy, split, subset=None):
        if policy.content_hash != incumbent:
            raise KeyboardInterrupt  # Ctrl-C while the candidate is evaluated
        return evaluate(policy, split, subset)

    services.evaluate = interrupted
    report = ImproveLoop(services, _options(rounds=2)).run()
    (round_,) = report.rounds  # the loop stopped after the interrupted round
    assert INTERRUPTED in round_.stopped and INTERRUPTED in report.stop_reason
    invalid, in_flight = round_.attempts  # the third proposal was never taken up
    assert invalid.record.outcome is Outcome.INVALID
    assert in_flight.record.outcome is Outcome.INCOMPLETE and in_flight.record.note == f"evaluation {INTERRUPTED}"
    assert [c.outcome for c in store.candidates()] == [Outcome.INVALID, Outcome.INCOMPLETE]
    assert [d.verdict for d in store.decisions()[2:]] == ["invalid", "incomplete"]
    assert not round_.promoted


def test_an_interrupt_while_proposing_records_the_proposals_that_came_back(rig, monkeypatch):
    store, directory = rig

    def interrupted_runner(jobs, **kw):  # SIGINT reaches the main thread, waiting on the proposers
        for job in jobs[:2]:
            job.run()  # two proposals came back before Ctrl-C
        raise KeyboardInterrupt

    monkeypatch.setattr("honed.learn.improve.run_jobs", interrupted_runner)
    (round_,) = ImproveLoop(_services(store, directory, LoopLLM(_round_answers())), _options()).run().rounds
    assert round_.stopped.startswith("proposing: ") and INTERRUPTED in round_.stopped
    assert {a.record.outcome for a in round_.attempts} == {Outcome.INCOMPLETE}
    assert len(store.candidates()) == len(round_.attempts) == 2
