"""The replay evaluation end to end, offline: a store with a human-review PR (gold set, context pack) and a clean PR,
the review pipeline and the judge on a scripted model, then scoring, the report, blinding, storage, the sensitivity
analysis and the decision log."""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from builders import comment
from evalkit import BANNED, GOLD, INDEX, NAMING, TEST_LEAK, build_store, evaluate, judge_for, scripted
from honed.adapters.pack_reader import PackReader
from honed.core import scoring
from honed.core.types import AuthorKind, FindingClass, LineBasis, Outcome, PRKey, Severity, ThreadLabel
from honed.learn import eval_report, sensitivity
from honed.learn.evaluate import EvalOptions, Evaluator
from honed.learn.replay import RoundGold, RoundGoldOptions
from reviewkit import HEAD, SERVICE_HEAD, SETTINGS, DictReader, ScriptedLLM, make_pipeline, seed_policy


@pytest.fixture
def rig(tmp_path):
    store = build_store(tmp_path)
    yield store
    store.close()


def test_an_evaluation_end_to_end(rig):
    outcome, llm = evaluate(rig)
    run = outcome.run
    assert run.stopped is None and not run.skipped and len(run.records) == 2
    human, clean = run.records
    classes = {f.title: m.klass for f, m in zip(human.result.findings, human.result.matches, strict=True)}
    assert classes == {INDEX: FindingClass.TP, NAMING: FindingClass.FP}
    assert human.result.matches[0].gold_id == GOLD.id and human.result.gold == (GOLD,)
    assert [m.klass for m in human.other_matches] == [FindingClass.FP]  # the dismissed check finding: not gold
    assert clean.corpus == "approval_only" and clean.result.gold == ()
    assert {m.klass for m in clean.result.matches} == {FindingClass.VU, FindingClass.FP}
    assert human.anchored == len(human.result.findings) and human.anchored_ids == tuple(
        f.id for f in human.result.findings
    )
    vu = next(m for m in clean.result.matches if m.klass is FindingClass.VU)
    assert vu.judged_severity is Severity.IMPORTANT  # the judge's own rating, recorded for scoring
    validity = next(c for c in llm.calls if c.stage == "validity")
    assert not re.search(r"\b(important|nit|pre_existing|severity)\b", validity.user, re.I)  # blind to the claim
    assert rig.get_round_gold(PRKey("o/r", 1), HEAD, 1).issues == (GOLD,)

    # Blinding: the reviewer sees no eval vocabulary; the judge sees neutral labels only.
    for call in llm.calls:
        text = call.system + "\n" + call.user
        if call.stage in ("intent", "verifier") or call.stage.startswith("finder:"):
            assert not BANNED.search(text), (call.stage, BANNED.search(text))
            assert not TEST_LEAK.search(text), call.stage
        else:
            policy = seed_policy()
            for leak in (policy.content_hash[:12], "claude-sonnet", "claude-opus", "raised by", "act_on",
                         "consider", "dismissed", "noted", "check:", "reviewer a", "evidence level"):  # fmt: skip
                assert leak not in text, (call.stage, leak)
    match_call = next(c for c in llm.calls if c.stage == "match")
    assert "=== F1 ===" in match_call.user and "=== D1 ===" in match_call.user and "=== G1 ===" in match_call.user
    assert "items can be empty here" in match_call.user  # the human comment behind the gold issue

    # Scoring and the report.
    params = SETTINGS.metrics.scoring_params()
    settings = eval_report.ReportSettings(params, 200, 1, 0.95, 5, 30)
    report = eval_report.report(run, settings, policy={"hash": "x"})
    metrics = report["metrics"]
    # human: TP 3 (gold 3 x conf 1) + FP 0.5; clean: VU 0.5 x 3 + FP 0.5 -> P = 4.5 / 5.5, R = 1
    assert metrics["languages"]["python"]["precision"] == round(4.5 / 5.5, 4)
    assert metrics["important_recall"] == 1.0 and metrics["clean_pr_alarm_rate"] == 1.0
    diag = report["diagnostics"]
    assert diag["consensus"]["rate"] == 0.5 and diag["verifier"]["false_dismissals"] == 0
    assert diag["evidence_level"]["distribution"] == {3: 2, 2: 2}
    assert diag["by_lesson"]["new-lint-suppression"]["fired"] == 2
    assert diag["cost"]["review_mean_usd"] == 0.08 and diag["context"]["pack_hit_rate"] == 1.0


def test_runs_are_stored_by_key_and_compared_paired(rig):
    first, _ = evaluate(rig)
    rig.save_eval_run(first.run)
    loaded = rig.latest_eval_run(first.run.policy_hash, "dev", "scripted", 1, 0)
    assert loaded == first.run and rig.latest_eval_run(first.run.policy_hash, "dev", "scripted", 2, 0) is None
    rig.set_incumbent("dev", "scripted", 1, first.run.id)
    assert rig.incumbent("dev", "scripted", 1) == first.run.id

    again, _ = evaluate(rig, sample=1)
    weak_policy = sensitivity.weaken(seed_policy())
    weak, weak_llm = evaluate(rig, policy=weak_policy)
    assert "verifier" not in {c.stage for c in weak_llm.calls}
    params = SETTINGS.metrics.scoring_params()
    settings = eval_report.ReportSettings(params, 200, 1, 0.95, 5, 30)
    third, _ = evaluate(rig, sample=2)
    result = sensitivity.analyze([first.run, again.run, third.run], weak.run, settings, floor=0.01, multiplier=2.0)
    noise = result["noise_floor"]
    assert [p["samples"] for p in noise["pairs"]] == [[0, 1], [0, 2], [1, 2]]
    assert {p["delta_S"] for p in noise["pairs"]} == {0} and result["min_gain"] == 0.01 and noise["samples"] == 3
    rows = sensitivity.decisions([first.run, again.run, third.run], weak.run, result)
    for row in rows:
        rig.add_decision(row)
    logged = rig.decisions()
    assert [d.id for d in logged] == [1, 2] and logged[0].verdict == "noise floor measured"
    assert json.loads(logged[0].gate) == {"sigma": 0.0, "min_gain": 0.01, "samples": 3}
    assert logged[1].hypothesis.startswith("The eval separates")


def test_a_later_round_without_a_pack_is_skipped(rig):
    item = rig.get_pr(PRKey("o/r", 1))
    later = replace(item.pr.threads[0], id="t2", comments=(comment("rev", commit="c" * 40, line=11, cid="t2-0",
                                                                    at="2026-03-03T00:00:00Z"),))  # fmt: skip
    labels = (*item.labels, ThreadLabel("t2", AuthorKind.HUMAN, Outcome.FIXED, True, LineBasis.COMPARE))
    rig.upsert_pr(replace(item, pr=replace(item.pr, threads=(*item.pr.threads, later)), labels=labels))
    llm = ScriptedLLM({"a": [], "b": []})
    judge = judge_for(llm)
    files = {("app/service.py", HEAD): SERVICE_HEAD, ("app/service.py", "c" * 40): SERVICE_HEAD}
    gold = RoundGold(rig, judge, lambda repo: DictReader(files), RoundGoldOptions(2, 3, SETTINGS.metrics.gold_conf,
                                                                                   False))  # fmt: skip
    options = EvalOptions("dev", 2, 0, "scripted", 1, 2, 20)
    run = Evaluator(rig, rig, make_pipeline(llm, None, rig), judge, gold, lambda p: PackReader(p, rig.get_blob),
                    options).run([PRKey("o/r", 1)]).run  # fmt: skip
    assert [r.result.round for r in run.records] == [1]
    assert "no context pack" in run.skipped["o/r#1@2"]
    # t1's issue stays in round 1; t2 was raised in round 2 on code round 1 already had, so it counts in round 1.
    assert rig.get_round_gold(PRKey("o/r", 1), "c" * 40, 2).issues == ()


def test_an_empty_run_reports_every_headline_key(rig):
    params = SETTINGS.metrics.scoring_params()
    full = eval_report.headline(evaluate(rig)[0].run.results, params)
    assert set(eval_report.headline([], params)) == set(full)


def test_a_valid_important_the_judge_rates_a_nit_costs_precision(rig):
    """Severity inflation (METRICS.md section 1): the clean PR's valid IndexError finding, claimed Important, rated a
    Nit by the judge, costs 0.5 instead of earning 1.5."""
    outcome, _ = evaluate(rig, llm=scripted(severity=lambda text: "nit"))
    clean = outcome.run.records[1]
    vu = next(m for m in clean.result.matches if m.klass is FindingClass.VU)
    assert vu.judged_severity is Severity.NIT
    t = scoring.tally(clean.result, SETTINGS.metrics.scoring_params())
    assert (t.vu, t.fp) == (0.0, 1.0)  # the inflated Important at Nit weight, plus the invalid Nit


def test_a_stored_run_is_reused_only_under_the_same_judge(tmp_path, capsys):
    """ARCHITECTURE.md section 6, storage: a run judged by another judge (model or prompts) is re-judged, its reviews
    served by whatever cache the stack has, never silently reused."""
    from honed import config
    from honed.cli.reviews import evaluate_policy

    settings = config.relocate_data(SETTINGS, tmp_path)
    store = build_store(tmp_path)
    llm = scripted()

    class Wiring:  # the parts of `LLMWiring` an evaluation uses
        backend, run_id, judge = "scripted", "run-x", "fable:aaaa"
        llm = judge_llm = gold_llm = None

        def judge_id(self, judge) -> str:
            return self.judge

        def status_line(self) -> str:
            return "scripted"

    wiring = Wiring()
    wiring.llm = wiring.judge_llm = wiring.gold_llm = llm
    keys = [PRKey("o/r", 1), PRKey("o/r", 2)]
    policy = seed_policy()
    try:
        first = evaluate_policy(settings, store, wiring, policy, keys, split="dev", rounds=1, sample=0)
        assert first.judge == "fable:aaaa" and first.stopped is None
        again = evaluate_policy(settings, store, wiring, policy, keys, split="dev", rounds=1, sample=0)
        assert again.id == first.id and "reusing stored run" in capsys.readouterr().out
        wiring.judge = "fable:bbbb"  # the validity prompt changed, say
        rejudged = evaluate_policy(settings, store, wiring, policy, keys, split="dev", rounds=1, sample=0)
        assert rejudged.id != first.id and rejudged.judge == "fable:bbbb"
        assert "not reusing stored run" in capsys.readouterr().out
    finally:
        store.close()
