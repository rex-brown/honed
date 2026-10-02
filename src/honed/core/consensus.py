"""People's verdicts on the blind audit sample, combined (METRICS.md section 5). Pure functions.

Each labeler returns one file of verdicts (valid, not valid or unsure). An item's human answer is the majority of the
usable verdicts (valid or not valid; "unsure" is left out) of at least `MIN_LABELERS` labelers. An item only one
labeler answered is reported apart and flagged, never counted; an even split has no answer. How far the labelers agree
with each other is Fleiss' kappa with three or more labelers, Cohen's kappa with two.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from honed.core import agreement

MIN_LABELERS = 2  # usable verdicts an item needs before its majority is the human answer


class HumanVerdict(StrEnum):
    VALID = "valid"
    NOT_VALID = "not_valid"
    UNSURE = "unsure"  # left out of every answer and every agreement statistic


@dataclass(frozen=True)
class HumanLabel:
    """One person's verdict on one review comment of the blind sample (`yardstick/human_labels/sample.json`)."""

    thread_id: str
    labeler: str  # the labeler's GitHub login
    verdict: HumanVerdict
    note: str = ""


class AnswerStatus(StrEnum):
    MAJORITY = "majority"  # at least MIN_LABELERS usable verdicts, one side holding more than half: counted
    SINGLE = "single"  # one usable verdict: reported apart and flagged, not counted
    TIE = "tie"  # usable verdicts split evenly: no answer
    UNSURE = "unsure"  # only "unsure" verdicts: no answer


@dataclass(frozen=True)
class ItemAnswer:
    """The people's answer on one item of the sample."""

    thread_id: str
    valid: int
    not_valid: int
    unsure: int

    @property
    def usable(self) -> int:
        return self.valid + self.not_valid

    @property
    def status(self) -> AnswerStatus:
        if self.usable == 0:
            return AnswerStatus.UNSURE
        if self.usable < MIN_LABELERS:
            return AnswerStatus.SINGLE
        return AnswerStatus.TIE if self.valid == self.not_valid else AnswerStatus.MAJORITY

    @property
    def answer(self) -> bool | None:
        """True (valid) or False (not valid) for a majority or a single verdict; None without one."""
        if self.status in (AnswerStatus.TIE, AnswerStatus.UNSURE):
            return None
        return self.valid > self.not_valid


def latest(labels: Iterable[HumanLabel]) -> list[HumanLabel]:
    """One label per labeler and item: a later label of the same item by the same labeler replaces an earlier one."""
    by_key = {(lab.labeler.lower(), lab.thread_id): lab for lab in labels}
    return list(by_key.values())


def answers(labels: Iterable[HumanLabel]) -> list[ItemAnswer]:
    """Every labeled item's answer, ordered by thread id."""
    counts: dict[str, Counter[HumanVerdict]] = defaultdict(Counter)
    for lab in latest(labels):
        counts[lab.thread_id][lab.verdict] += 1
    return [
        ItemAnswer(tid, c[HumanVerdict.VALID], c[HumanVerdict.NOT_VALID], c[HumanVerdict.UNSURE])
        for tid, c in sorted(counts.items())
    ]


@dataclass(frozen=True)
class InterAnnotator:
    """How far the labelers agree with each other."""

    method: str  # "fleiss" (three or more labelers), "cohen" (two), "" (fewer: nothing to compare)
    kappa: float | None
    labelers: int
    items: int  # items the statistic covers: two or more usable verdicts (Fleiss), both labelers' (Cohen)


def inter_annotator(labels: Sequence[HumanLabel]) -> InterAnnotator:
    """Fleiss' kappa with three or more labelers, Cohen's kappa with two; "unsure" verdicts are left out."""
    kept = latest(labels)
    labelers = sorted({lab.labeler.lower() for lab in kept})
    usable = [lab for lab in kept if lab.verdict is not HumanVerdict.UNSURE]
    if len(labelers) >= 3:
        per_item: dict[str, Counter[HumanVerdict]] = defaultdict(Counter)
        for lab in usable:
            per_item[lab.thread_id][lab.verdict] += 1
        tables = [c for c in per_item.values() if sum(c.values()) >= 2]
        return InterAnnotator("fleiss", agreement.fleiss_kappa(tables), len(labelers), len(tables))
    if len(labelers) == 2:
        first, second = labelers
        a = {lab.thread_id: lab.verdict for lab in usable if lab.labeler.lower() == first}
        b = {lab.thread_id: lab.verdict for lab in usable if lab.labeler.lower() == second}
        pairs = [(a[tid], b[tid]) for tid in sorted(a.keys() & b.keys())]
        return InterAnnotator("cohen", agreement.cohen_kappa(pairs), 2, len(pairs))
    return InterAnnotator("", None, len(labelers), 0)
