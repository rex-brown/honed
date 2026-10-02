"""The cross-family audit on scripted judges: Fable's questions are rebuilt from a stored run exactly as Fable saw them,
put to another judge, and scored for agreement, Cohen's kappa and the matcher's F1."""

from __future__ import annotations

import pytest

from evalkit import build_store, evaluate, judge_for, scripted
from honed.adapters.pack_reader import PackReader
from honed.learn.crossfamily import AuditOptions, CrossFamilyAudit, collect, select_matching, select_validity


@pytest.fixture
def stored(tmp_path):
    store = build_store(tmp_path)
    fable = scripted()
    outcome, _ = evaluate(store, llm=fable)
    store.save_eval_run(outcome.run)
    yield store, outcome.run, fable
    store.close()


OPTIONS = AuditOptions(validity_items=40, match_rounds=20, excerpt_lines=2, max_findings_judged=20)


def test_questions_are_rebuilt_exactly_and_answers_compared(stored):
    store, run, fable = stored
    asked = collect([run, run], store, lambda pack: PackReader(pack, store.get_blob), OPTIONS)
    assert len(asked) == 2  # a run given twice asks each question once
    human, clean = sorted(asked, key=lambda a: a.unit)
    # Labels in file order: F1 is the rename Nit (line 10), F2 the IndexError (line 11), D1 the dismissed check.
    assert human.validity == {"F1": False} and human.matches == {"F1": None, "F2": "G1", "D1": None}
    assert clean.validity == {"F1": False, "F2": True} and not any(clean.matches.values())  # no gold: no match asked

    # The other judge: rules every finding valid, and matches only the IndexError finding.
    other = scripted(valid=lambda text: True, match=lambda text, gold: "IndexError" in text)
    report = CrossFamilyAudit(judge_for(other), alarm_kappa=0.5, min_f1=0.9).run(
        select_validity(asked, 40), select_matching(asked, 20)
    )
    asked_before = {c.user for c in fable.calls if c.stage in ("validity", "match")}
    assert {c.user for c in other.calls} <= asked_before  # byte-identical questions
    assert report.validity_n == 3 and dict(report.confusion) == {"invalid->valid": 2, "valid->valid": 1}
    assert report.agreement == pytest.approx(1 / 3) and report.kappa == 0.0 and report.alarm
    assert report.match_rounds == 1 and (report.pairs_fable, report.pairs_local, report.pairs_both) == (1, 1, 1)
    assert report.f1 == 1.0 and report.matcher_passes and not report.failures
    assert len(report.disagreements) == 2


def test_rounds_with_an_invalid_verdict_are_sampled_first(stored):
    store, run, _ = stored
    asked = collect([run], store, lambda pack: PackReader(pack, store.get_blob), OPTIONS)
    first = select_validity(asked, 1)
    assert len(first) == 1 and not all(first[0].validity.values())


def test_a_judge_failure_is_reported_not_counted(stored):
    store, run, _ = stored
    asked = collect([run], store, lambda pack: PackReader(pack, store.get_blob), OPTIONS)

    class Broken:
        model = "local:broken"

        def validity_many(self, *a, **k):
            from honed.ports.judge import JudgeError

            raise JudgeError("no verdict for ['F1']")

        def match(self, *a, **k):
            return []

    report = CrossFamilyAudit(Broken(), alarm_kappa=0.5, min_f1=0.9).run(select_validity(asked, 40), [])  # type: ignore[arg-type]
    assert report.validity_n == 0 and report.kappa is None and report.alarm is None
    assert set(report.failures) == {f"validity:{a.unit}" for a in asked if a.validity}


def test_the_alarm_counts_only_once_the_local_judge_agrees_with_people():
    """METRICS.md section 5: below `local_min_human_accuracy` on the human labels, a low kappa is report-only."""
    from honed.learn.crossfamily import CrossFamilyReport

    report = CrossFamilyReport("local:x", kappa=0.07, alarm_kappa=0.5, min_human_accuracy=0.75)
    assert report.alarm and not report.alarm_counts
    assert report.alarm_status() == "alarm level, report-only: no human labels yet"
    report.human_n, report.human_accuracy = 40, 0.6
    assert report.alarm_status().startswith("alarm level, report-only: the local judge's accuracy on 40 human")
    report.human_accuracy = 0.8
    assert report.alarm_counts and report.alarm_status() == "ALARM"
    assert CrossFamilyReport("local:x", kappa=0.7, alarm_kappa=0.5).alarm_status() == "ok"
    assert CrossFamilyReport("local:x").alarm_status() == "undefined"
