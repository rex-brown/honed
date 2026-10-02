"""Pure labeling logic: line mapping, suggestions, judged labels, gold provenance and agreement statistics."""

from __future__ import annotations

import pytest

from builders import comment, thread
from honed.core import agreement, labels, lines, suggestions
from honed.core.types import (
    Addressed,
    AuthorKind,
    CommitInfo,
    GoldProvenance,
    Judgment,
    JudgmentKind,
    LineBasis,
    Outcome,
    Polarity,
    Stance,
    Strength,
    ThreadLabel,
)

HIGH_RISK = frozenset({"security", "concurrency"})

# ---- line mapping --------------------------------------------------------------------------------------------

OLD = "a\nb\nc\nd\ne\n"
NEW = "a\nB\nc\nx\nd\ne\n"


def test_map_forward_follows_replaced_and_split_lines():
    assert lines.map_forward(OLD, NEW, 2, 2) == (2, 2)  # b became B
    assert lines.map_forward(OLD, NEW, 3, 4) == (3, 5)  # c, d with x inserted between them
    assert lines.map_forward(OLD, NEW, 5, 5) == (6, 6)  # e moved down one
    assert lines.map_forward("a\nb\nc\n", "a\nc\n", 2, 2) == (2, 1)  # deleted: an empty range where it was


def test_unchanged_back_only_maps_untouched_lines():
    assert lines.unchanged_back(OLD, NEW, 5, 6) == (4, 5)
    assert lines.unchanged_back(OLD, NEW, 4, 4) is None  # x did not exist in the old version
    assert lines.unchanged_back(OLD, NEW, 2, 3) is None  # B changed


def test_excerpt_marks_flagged_lines_and_clips():
    text = "\n".join(f"l{n}" for n in range(1, 21))
    out = lines.excerpt(text, 10, 11, 2).splitlines()
    assert out[0] == "  8 | l8" and out[2] == ">10 | l10" and out[3] == ">11 | l11" and out[-1] == " 13 | l13"
    assert lines.excerpt(text, 1, 1, 3).splitlines()[0] == ">1 | l1"
    assert "omitted" in lines.excerpt(text, 1, 20, 0, max_lines=6)


# ---- suggestions ---------------------------------------------------------------------------------------------

PATCH = "@@ -9,3 +9,3 @@ def f():\n     a = 1\n-    return x\n+    return x + 1\n     b = 2"


def test_suggestion_blocks_are_parsed():
    body = "Nit:\r\n```suggestion\r\n    return x + 1\r\n```\r\nand\n````suggestion\n\n````"
    assert suggestions.blocks(body) == ["    return x + 1\n", "\n"]
    assert suggestions.suggestion(thread(comment(body="no code here"))) is None


@pytest.mark.parametrize(
    ("suggested", "start", "applied"),
    [
        ("    return x + 1\n", 10, True),
        ("    return  x + 1  \n", 10, False),  # different text
        ("    return y\n", 10, False),
        ("    return x + 1\n", 30, False),  # the change is elsewhere
        ("    a = 1\n    return x + 1\n", 10, True),  # an unchanged line of the suggestion stays as context
    ],
)
def test_applied_in_patch(suggested, start, applied):
    assert suggestions.applied_in_patch(suggested, PATCH, start, start) is applied


def test_an_empty_suggestion_means_delete_the_lines():
    assert suggestions.applied_in_patch("", "@@ -10 +9,0 @@\n-    return x", 10, 10)
    assert not suggestions.applied_in_patch("", PATCH, 10, 10)


def test_a_suggestion_commit_after_the_comment_counts():
    t = thread(comment("rev", body="```suggestion\nx\n```", at="2026-03-01T00:00:00Z"))
    batch = CommitInfo("c1", "2026-03-02T00:00:00Z", "2026-03-02", "Apply suggestions from code review")
    mine = CommitInfo("c2", "2026-03-02T00:00:00Z", "2026-03-02", "Apply suggestion from @rev")
    other = CommitInfo("c3", "2026-03-02T00:00:00Z", "2026-03-02", "Apply suggestion from @someone")
    early = CommitInfo("c4", "2026-02-01T00:00:00Z", "2026-02-01", "Apply suggestions from code review")
    assert suggestions.applied_by_commit(t, [batch]) and suggestions.applied_by_commit(t, [mine])
    assert not suggestions.applied_by_commit(t, [other, early])


# ---- judged labels ---------------------------------------------------------------------------------------------


def lab(outcome: Outcome, kind: AuthorKind = AuthorKind.HUMAN) -> ThreadLabel:
    return ThreadLabel("t1", kind, outcome, outcome is Outcome.FIXED, LineBasis.COMPARE)


def js(**kinds: tuple[str, str | None]) -> dict[JudgmentKind, Judgment]:
    return {JudgmentKind(k): Judgment("o/r", 1, "t1", JudgmentKind(k), v, category=c) for k, (v, c) in kinds.items()}


def judged(outcome, judgments=None, *, reply=False, kind=AuthorKind.HUMAN):
    return labels.judged_label(lab(outcome, kind), judgments or {}, has_reply=reply, high_risk=HIGH_RISK)


def test_fixed_needs_confirmation():
    assert judged(Outcome.FIXED).polarity is None  # pending the addressed check
    ok = judged(Outcome.FIXED, js(addressed=("partially", None)))
    assert (ok.polarity, ok.strength, ok.addressed) == (Polarity.POSITIVE, Strength.STRONG, Addressed.PARTIALLY)
    rejected = judged(Outcome.FIXED, js(addressed=("not_addressed", None)))
    assert (rejected.outcome, rejected.polarity) == (Outcome.CHANGED_UNADDRESSED, Polarity.NEUTRAL)
    applied = judged(Outcome.FIXED, js(suggestion=("applied", None)))
    assert applied.applied_suggestion and applied.polarity is Polarity.POSITIVE


def test_stances_move_unfixed_threads():
    assert judged(Outcome.RESOLVED_NO_CHANGE).polarity is None  # dismissed: awaits a category
    weak = judged(Outcome.RESOLVED_NO_CHANGE, js(classify=("", "style")))
    assert (weak.polarity, weak.strength) == (Polarity.NEGATIVE, Strength.WEAK)
    agree = judged(Outcome.OPEN_AT_MERGE, js(classify=("agree", "correctness")), reply=True)
    assert (agree.polarity, agree.strength, agree.stance) == (Polarity.POSITIVE, Strength.MEDIUM, Stance.AGREE)
    disagree = judged(Outcome.RESOLVED_NO_CHANGE, js(classify=("disagree", "design")), reply=True)
    assert (disagree.polarity, disagree.strength) == (Polarity.NEGATIVE, Strength.MEDIUM)
    question = judged(Outcome.OPEN_AT_MERGE, js(classify=("question", "design")), reply=True)
    assert question.polarity is Polarity.NEUTRAL


def test_high_risk_dismissals_are_flagged_never_negative():
    for outcome in (Outcome.RESOLVED_NO_CHANGE, Outcome.THUMBS_DOWN, Outcome.IGNORED):
        dismissed = judged(outcome, js(classify=("", "security")))
        assert dismissed.high_risk_dismissal and dismissed.polarity is Polarity.NEUTRAL
    ai = judged(Outcome.RESOLVED_NO_CHANGE, js(classify=("disagree", "concurrency")), reply=True, kind=AuthorKind.AI)
    assert ai.high_risk_dismissal and ai.polarity is Polarity.NEUTRAL
    low = judged(Outcome.THUMBS_DOWN, js(classify=("", "style")))
    assert not low.high_risk_dismissal and (low.polarity, low.strength) == (Polarity.NEGATIVE, Strength.STRONG)


def test_the_pr_authors_own_threads_carry_no_signal():
    assert judged(Outcome.FIXED, kind=AuthorKind.PR_AUTHOR).polarity is Polarity.NEUTRAL
    assert not labels.needs_addressed_check(lab(Outcome.FIXED, AuthorKind.PR_AUTHOR), applied=False)


def test_gold_provenance():
    assert labels.gold_provenance(judged(Outcome.FIXED, js(suggestion=("applied", None)))) is (
        GoldProvenance.APPLIED_SUGGESTION)  # fmt: skip
    assert labels.gold_provenance(judged(Outcome.FIXED, js(addressed=("addressed", None)))) is (
        GoldProvenance.HUMAN_FIXED)  # fmt: skip
    agree = judged(Outcome.OPEN_AT_MERGE, js(classify=("agree", "tests")), reply=True)
    assert labels.gold_provenance(agree) is GoldProvenance.HUMAN_OPEN_AT_MERGE
    assert labels.gold_provenance(judged(Outcome.FIXED, js(addressed=("not_addressed", None)))) is None
    ai = judged(Outcome.FIXED, js(addressed=("addressed", None)), kind=AuthorKind.AI)
    assert labels.gold_provenance(ai) is None  # gold issues come from human review
    assert labels.strongest([GoldProvenance.HUMAN_OPEN_AT_MERGE, GoldProvenance.APPLIED_SUGGESTION]) is (
        GoldProvenance.APPLIED_SUGGESTION)  # fmt: skip


def test_approval_only_means_no_human_inline_thread():
    assert labels.is_approval_only([AuthorKind.PR_AUTHOR, AuthorKind.AI, AuthorKind.BOT])
    assert labels.is_approval_only([])
    assert not labels.is_approval_only([AuthorKind.PR_AUTHOR, AuthorKind.HUMAN])


# ---- agreement -------------------------------------------------------------------------------------------------


def test_cohen_kappa():
    pairs = [(True, True)] * 20 + [(True, False)] * 5 + [(False, True)] * 10 + [(False, False)] * 15
    assert agreement.accuracy(pairs) == 0.7
    assert agreement.cohen_kappa(pairs) == pytest.approx(0.4)
    assert agreement.cohen_kappa([(True, True)] * 5) is None  # one class on both sides: undefined
    assert agreement.cohen_kappa([(True, True)] * 4 + [(True, False)]) == 0.0
    assert agreement.cohen_kappa([]) is None and agreement.accuracy([]) is None


def test_consistency():
    unanimous, pairwise = agreement.consistency([[True, True, True], [True, False, True], [False, False, False]])
    assert unanimous == pytest.approx(2 / 3) and pairwise == pytest.approx(7 / 9)
    assert agreement.consistency([[True]]) == (None, None)
