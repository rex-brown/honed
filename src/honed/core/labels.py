"""From mechanical outcomes plus judgments to training labels (ARCHITECTURE.md section 5).

- A `fixed` thread is a strong positive only once confirmed: its ```suggestion was applied, or the judge found the
  change addressed the comment (fully or partially). Rejected ones become `changed_unaddressed`: neutral.
- A reply's stance moves an unfixed thread: agree or fixed elsewhere is a medium positive, disagree a medium
  negative. Without one, `resolved_no_change` and `ignored` are weak negatives and `thumbs_down` a strong one.
- A human dismissal of a finding in a high-risk category is the owner's call, not evidence the finding was wrong:
  it is flagged and never a negative label.
- Only review findings (threads opened by a human or AI reviewer) carry a signal; the PR author's own threads and
  automation are neutral.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping

from honed.core.types import (
    Addressed,
    AuthorKind,
    GoldProvenance,
    JudgedLabel,
    Judgment,
    JudgmentKind,
    Outcome,
    Polarity,
    Stance,
    Strength,
    ThreadLabel,
)

REVIEW_KINDS = frozenset({AuthorKind.HUMAN, AuthorKind.AI})
DISMISSED = frozenset({Outcome.THUMBS_DOWN, Outcome.RESOLVED_NO_CHANGE, Outcome.IGNORED})
APPLIED, NOT_APPLIED, NO_SUGGESTION = "applied", "not_applied", "none"


def is_approval_only(thread_authors: Iterable[AuthorKind]) -> bool:
    """No human inline review thread (by the kinds of the threads' openers): the PR belongs to the clean-PR set,
    not the human-review quota."""
    return AuthorKind.HUMAN not in set(thread_authors)


def needs_addressed_check(label: ThreadLabel, applied: bool) -> bool:
    return label.author_kind in REVIEW_KINDS and label.outcome is Outcome.FIXED and not applied


def needs_classification(label: ThreadLabel, has_reply: bool) -> bool:
    """Stance for replied-to threads that aren't fixed; category for dismissed ones (the high-risk check)."""
    if label.author_kind not in REVIEW_KINDS or label.outcome is Outcome.FIXED:
        return False
    return has_reply or label.outcome in DISMISSED


def judged_label(
    label: ThreadLabel, judgments: Mapping[JudgmentKind, Judgment], *, has_reply: bool, high_risk: Collection[str]
) -> JudgedLabel:
    suggestion = judgments.get(JudgmentKind.SUGGESTION)
    applied = suggestion is not None and suggestion.value == APPLIED
    addressed_j = judgments.get(JudgmentKind.ADDRESSED)
    addressed = Addressed(addressed_j.value) if addressed_j else None
    classify = judgments.get(JudgmentKind.CLASSIFY)
    stance = Stance(classify.value) if classify and classify.value else None
    category = classify.category if classify else None
    outcome = label.outcome
    polarity: Polarity | None
    strength: Strength | None = None

    if label.author_kind not in REVIEW_KINDS:
        polarity = Polarity.NEUTRAL
    elif outcome is Outcome.FIXED:
        if applied or addressed in (Addressed.ADDRESSED, Addressed.PARTIALLY):
            polarity, strength = Polarity.POSITIVE, Strength.STRONG
        elif addressed is Addressed.NOT_ADDRESSED:
            outcome, polarity = Outcome.CHANGED_UNADDRESSED, Polarity.NEUTRAL
        else:
            polarity = None  # awaiting the addressed check
    elif needs_classification(label, has_reply) and classify is None:
        polarity = None  # awaiting stance and category
    elif outcome is Outcome.THUMBS_DOWN:
        polarity, strength = Polarity.NEGATIVE, Strength.STRONG
    elif stance in (Stance.AGREE, Stance.FIXED_ELSEWHERE):
        polarity, strength = Polarity.POSITIVE, Strength.MEDIUM
    elif stance is Stance.DISAGREE:
        polarity, strength = Polarity.NEGATIVE, Strength.MEDIUM
    elif outcome in DISMISSED:
        polarity, strength = Polarity.NEGATIVE, Strength.WEAK
    else:
        polarity = Polarity.NEUTRAL

    high_risk_dismissal = polarity is Polarity.NEGATIVE and category in high_risk
    if high_risk_dismissal:
        polarity, strength = Polarity.NEUTRAL, None
    return JudgedLabel(
        thread_id=label.thread_id, author_kind=label.author_kind, outcome=outcome, polarity=polarity,
        strength=strength, applied_suggestion=applied, addressed=addressed, stance=stance, category=category,
        high_risk_dismissal=high_risk_dismissal,
    )  # fmt: skip


def gold_provenance(label: JudgedLabel) -> GoldProvenance | None:
    """Why a human thread is a gold issue (METRICS.md section 8 `gold_conf`), or None when it isn't one."""
    if label.author_kind is not AuthorKind.HUMAN or label.outcome is Outcome.THUMBS_DOWN:
        return None
    if label.applied_suggestion and label.outcome is Outcome.FIXED:
        return GoldProvenance.APPLIED_SUGGESTION
    if label.outcome is Outcome.FIXED and label.addressed in (Addressed.ADDRESSED, Addressed.PARTIALLY):
        return GoldProvenance.HUMAN_FIXED
    if label.outcome is Outcome.OPEN_AT_MERGE and label.stance is Stance.AGREE:
        return GoldProvenance.HUMAN_OPEN_AT_MERGE
    return None


PROVENANCE_RANK = (
    GoldProvenance.APPLIED_SUGGESTION,
    GoldProvenance.HUMAN_FIXED,
    GoldProvenance.HUMAN_OPEN_AT_MERGE,
    GoldProvenance.JUDGE_ONLY,
)


def strongest(provenances: Iterable[GoldProvenance]) -> GoldProvenance:
    """The strongest provenance among a merged issue's threads."""
    return min(provenances, key=PROVENANCE_RANK.index)
