"""core.scoring against METRICS.md sections 1, 2 and 6."""

from dataclasses import replace

import pytest

from honed.core import scoring
from honed.core.types import (
    EvalResult,
    Finding,
    FindingClass,
    GoldIssue,
    GoldProvenance,
    Match,
    PRKey,
    Severity,
)

# METRICS.md section 8 defaults, passed explicitly: core has no defaults of its own.
PARAMS = scoring.ScoringParams(
    beta=0.5,
    severity_weights={Severity.IMPORTANT: 3.0, Severity.PRE_EXISTING: 1.0, Severity.NIT: 0.5},
    valid_unlabeled_credit=0.5,
    language_weights={"typescript": 0.45, "cpp": 0.25, "python": 0.20, "other": 0.10},
    max_neutral_vu_per_round=3,
    vu_min_evidence=3,
)


def gold(gid: str, severity: Severity, conf: float = 1.0) -> GoldIssue:
    return GoldIssue(gid, "a.ts", 1, 1, severity, GoldProvenance.HUMAN_FIXED, conf)


def finding(fid: str, severity: Severity) -> Finding:
    return Finding(fid, "a.ts", 1, 1, severity, "bug", fid)


def result(
    number: int,
    language: str,
    golds: list[GoldIssue],
    judged: list[tuple[Finding, FindingClass, str | None]],
) -> EvalResult:
    return EvalResult(
        pr=PRKey("o/r", number),
        language=language,
        gold=tuple(golds),
        findings=tuple(f for f, _, _ in judged),
        matches=tuple(Match(f.id, klass, gid) for f, klass, gid in judged),
    )


def worked_example(language: str = "typescript", number: int = 1) -> EvalResult:
    """METRICS.md section 2: gold Important + Nit; findings TP on the Important, a valid Nit, a false Important."""
    return result(
        number,
        language,
        [gold("g1", Severity.IMPORTANT), gold("g2", Severity.NIT)],
        [
            (finding("f1", Severity.IMPORTANT), FindingClass.TP, "g1"),
            (finding("f2", Severity.NIT), FindingClass.VU, None),
            (finding("f3", Severity.IMPORTANT), FindingClass.FP, None),
        ],
    )


def test_worked_example():
    """The valid Nit is neutral (within the cap): it neither earns nor costs."""
    t = scoring.tally(worked_example(), PARAMS)
    assert (t.tp, t.vu, t.fp, t.gold) == (3.0, 0.0, 3.0, 3.5)
    p, r = scoring.precision(t), scoring.recall(t)
    assert p == pytest.approx(0.5)
    assert r == pytest.approx(0.857, abs=1e-3)
    assert scoring.f_beta(p, r, 0.5) == pytest.approx(0.545, abs=1e-3)
    assert scoring.headline([worked_example()], PARAMS) == pytest.approx(0.545, abs=1e-3)
    credits = [s.credit for s in scoring.score_findings(worked_example(), PARAMS)]
    assert credits == [scoring.Credit.TP, scoring.Credit.NEUTRAL, scoring.Credit.FP]


def _vu(fid: str, claimed: Severity, judged: Severity | None, level: int) -> tuple[Finding, Match]:
    return replace(finding(fid, claimed), evidence_level=level), Match(
        fid, FindingClass.VU, None, "", judged_severity=judged
    )


def _vu_result(*pairs: tuple[Finding, Match]) -> EvalResult:
    return EvalResult(PRKey("o/r", 1), "python", (), tuple(f for f, _ in pairs), tuple(m for _, m in pairs))


def test_only_a_traced_valid_unlabeled_important_the_judge_rates_important_earns_credit():
    """METRICS.md section 1: c_vu needs the judge's own Important rating and evidence level 3 or higher."""
    credited = _vu_result(_vu("f1", Severity.IMPORTANT, Severity.IMPORTANT, 3),
                          _vu("f2", Severity.PRE_EXISTING, Severity.IMPORTANT, 3),
                          _vu("f3", Severity.NIT, Severity.IMPORTANT, 3))  # fmt: skip
    t = scoring.tally(credited, PARAMS)
    assert (t.tp, t.vu, t.fp) == (0.0, 1.5, 0.0)  # c_vu x w(Important); the Pre-existing and the Nit are neutral
    assert scoring.precision(t) == 1.0
    untraced = _vu_result(_vu("f1", Severity.IMPORTANT, Severity.IMPORTANT, 2))
    legacy = _vu_result(_vu("f1", Severity.IMPORTANT, None, 3))  # judged before the judge rated severity
    for r in (untraced, legacy):
        t = scoring.tally(r, PARAMS)
        assert (t.vu, t.fp) == (0.0, 0.0) and scoring.score_findings(r, PARAMS)[0].credit is scoring.Credit.NEUTRAL


def test_a_valid_important_the_judge_rates_a_nit_is_an_fp_at_nit_weight():
    """Severity inflation: claimed Important, the judge rates a Nit; it costs what a false Nit costs, whatever its
    evidence level, and doesn't use up the round's neutral Nits."""
    r = _vu_result(_vu("f1", Severity.IMPORTANT, Severity.NIT, 3), *(_vu(f"n{i}", Severity.NIT, Severity.NIT, 1)
                                                                    for i in range(3)))  # fmt: skip
    t = scoring.tally(r, PARAMS)
    assert (t.vu, t.fp) == (0.0, 0.5)
    first, *nits = scoring.score_findings(r, PARAMS)
    assert first.inflated and not first.over_cap and first.weight == 0.5
    assert all(s.credit is scoring.Credit.NEUTRAL for s in nits)


def _valid_nits(number: int, n: int, round_: int = 1) -> EvalResult:
    findings = [(finding(f"f{number}-{round_}-{i}", Severity.NIT), FindingClass.VU, None) for i in range(n)]
    return replace(result(number, "python", [gold("g", Severity.NIT)], findings), round=round_)


def test_valid_unlabeled_nits_beyond_the_cap_cost_like_false_positives():
    at_cap, over = _valid_nits(1, 3), _valid_nits(2, 5)
    assert scoring.tally(at_cap, PARAMS).fp == 0.0
    t = scoring.tally(over, PARAMS)
    assert (t.vu, t.fp) == (0.0, 1.0)  # 5 - 3 = 2 Nits beyond the cap, 0.5 each
    scored = scoring.score_findings(over, PARAMS)
    assert [s.over_cap for s in scored] == [False, False, False, True, True]  # in posted order
    assert scoring.precision(t) == 0.0  # neutral Nits are out of the denominator; the two beyond it are not
    strict = replace(PARAMS, max_neutral_vu_per_round=0)
    assert scoring.tally(at_cap, strict).fp == 1.5


def test_the_cap_counts_per_pr_round_and_skips_other_classes():
    rounds = [_valid_nits(1, 3, round_=1), _valid_nits(1, 3, round_=2)]  # one PR, two rounds, 3 valid Nits each
    assert sum(scoring.tally(r, PARAMS).fp for r in rounds) == 0.0
    mixed = result(
        3,
        "python",
        [gold("g", Severity.NIT)],
        [
            (finding("t", Severity.NIT), FindingClass.TP, "g"),
            (finding("p", Severity.PRE_EXISTING), FindingClass.VU, None),
            *[(finding(f"v{i}", Severity.NIT), FindingClass.VU, None) for i in range(4)],
        ],
    )
    t = scoring.tally(mixed, PARAMS)
    assert (t.tp, t.fp) == (0.5, 0.5)  # the TP and the Pre-existing don't use up the cap; the 4th valid Nit does


def test_a_kept_subset_keeps_each_findings_place_relative_to_the_cap():
    over = _valid_nits(1, 5)
    last = over.findings[-1].id
    t = scoring.tally(over, PARAMS, keep=lambda f: f.id == last)
    assert (t.fp, t.gold) == (0.5, 0.5)  # still beyond the cap; gold sums cover the whole round


def test_true_positive_earns_the_gold_weight_times_its_confidence():
    r = result(
        1, "python", [gold("g", Severity.PRE_EXISTING, conf=0.8)], [(finding("f", Severity.NIT), FindingClass.TP, "g")]
    )
    t = scoring.tally(r, PARAMS)
    assert t.tp == pytest.approx(0.8)  # the gold's weight (1.0) x conf, not the claimed Nit's 0.5
    assert t.gold == pytest.approx(0.8)


def test_false_positive_and_duplicate_cost_the_claimed_severity():
    r = result(
        1,
        "python",
        [gold("g", Severity.NIT)],
        [
            (finding("f1", Severity.NIT), FindingClass.TP, "g"),
            (finding("f2", Severity.IMPORTANT), FindingClass.DUP, None),
            (finding("f3", Severity.PRE_EXISTING), FindingClass.FP, None),
        ],
    )
    assert scoring.tally(r, PARAMS).fp == 4.0


def test_a_second_tp_on_the_same_gold_issue_is_a_duplicate():
    r = result(
        1,
        "python",
        [gold("g", Severity.IMPORTANT)],
        [
            (finding("f1", Severity.IMPORTANT), FindingClass.TP, "g"),
            (finding("f2", Severity.NIT), FindingClass.TP, "g"),
        ],
    )
    t = scoring.tally(r, PARAMS)
    assert (t.tp, t.fp) == (3.0, 0.5)


def test_every_finding_needs_a_verdict():
    r = EvalResult(PRKey("o/r", 1), "python", (), (finding("f", Severity.NIT),), ())
    with pytest.raises(ValueError, match="no match verdict"):
        scoring.tally(r, PARAMS)


def test_clean_pr_with_empty_gold():
    clean = result(
        2,
        "typescript",
        [],
        [
            (finding("f1", Severity.NIT), FindingClass.VU, None),
            (finding("f2", Severity.IMPORTANT), FindingClass.FP, None),
        ],
    )
    t = scoring.tally(clean, PARAMS)
    assert (t.tp, t.gold) == (0.0, 0.0)
    assert scoring.recall(t) == 1.0  # nothing to miss
    assert scoring.precision(t) == 0.0  # the valid Nit is neutral; the false Important costs 3
    assert scoring.clean_pr_alarm_rate([clean, worked_example()]) == 1.0  # the only clean PR got an Important
    quiet = result(3, "typescript", [], [(finding("f", Severity.NIT), FindingClass.FP, None)])
    assert scoring.clean_pr_alarm_rate([clean, quiet]) == 0.5
    assert scoring.clean_pr_alarm_rate([worked_example()]) == 0.0


def test_no_findings_means_precision_one_and_recall_zero():
    silent = result(1, "python", [gold("g", Severity.IMPORTANT)], [])
    t = scoring.tally(silent, PARAMS)
    assert (scoring.precision(t), scoring.recall(t)) == (1.0, 0.0)
    assert scoring.headline([silent], PARAMS) == 0.0


def test_pooling_is_micro_averaged_within_a_language():
    a = worked_example(number=1)
    b = result(2, "typescript", [gold("g", Severity.IMPORTANT)], [])
    scores = scoring.language_scores([a, b], PARAMS)
    assert scores["typescript"].prs == 2
    assert scores["typescript"].recall == pytest.approx(3 / 6.5)  # pooled sums, not a mean of per-PR recalls


def test_headline_weights_languages_and_renormalizes_over_those_present():
    perfect = result(1, "cpp", [gold("g", Severity.NIT)], [(finding("f", Severity.NIT), FindingClass.TP, "g")])
    missed = result(2, "python", [gold("g", Severity.NIT)], [])
    s = scoring.headline([perfect, missed], PARAMS)
    assert s == pytest.approx(0.25 / (0.25 + 0.20))  # F_cpp = 1, F_python = 0


def test_important_recall_counts_only_important_gold():
    r = worked_example()
    assert scoring.important_recall([r], PARAMS) == 1.0
    miss = result(
        2,
        "python",
        [gold("g", Severity.IMPORTANT), gold("n", Severity.NIT)],
        [(finding("f", Severity.NIT), FindingClass.TP, "n")],
    )
    assert scoring.important_recall([miss], PARAMS) == 0.0
    assert scoring.important_recall([r, miss], PARAMS) == 0.5


def test_unknown_language_is_an_error():
    with pytest.raises(ValueError, match="no weight"):
        scoring.headline([worked_example(language="cobol")], PARAMS)


# ---- paired bootstrap ----------------------------------------------------------------------------------------


def _eval_set(better: bool) -> list[EvalResult]:
    results = []
    for i, language in enumerate(["typescript"] * 6 + ["cpp"] * 4 + ["python"] * 4):
        caught = better or i % 2 == 0
        judged = [(finding(f"f{i}", Severity.IMPORTANT), FindingClass.TP, "g")] if caught else []
        results.append(result(i, language, [gold("g", Severity.IMPORTANT)], judged))
    return results


def test_bootstrap_is_deterministic_for_a_seed():
    cand, inc = _eval_set(True), _eval_set(False)
    run = lambda seed: scoring.paired_bootstrap(cand, inc, PARAMS, resamples=200, seed=seed, ci_level=0.95)  # noqa: E731
    assert run(7) == run(7)
    assert run(7) != run(8)


def test_bootstrap_of_identical_policies_is_zero():
    same = _eval_set(False)
    ci = scoring.paired_bootstrap(same, same, PARAMS, resamples=200, seed=1, ci_level=0.95)
    assert (ci.delta, ci.low, ci.high) == (0.0, 0.0, 0.0)


def test_bootstrap_detects_a_real_gain():
    ci = scoring.paired_bootstrap(_eval_set(True), _eval_set(False), PARAMS, resamples=500, seed=3, ci_level=0.95)
    assert ci.delta > 0
    assert 0 < ci.low <= ci.delta <= ci.high
    assert ci.resamples == 500


def test_bootstrap_needs_the_same_prs():
    with pytest.raises(ValueError, match="same PRs"):
        scoring.paired_bootstrap(_eval_set(True), _eval_set(False)[1:], PARAMS, resamples=10, seed=1, ci_level=0.95)
