"""Judge: the fixed yardstick (ARCHITECTURE.md sections 5 and 6, METRICS.md section 5).

Labeling asks four questions of review threads: did the change address the comment, what is a reply's stance
(and the thread's category), which threads are one gold issue (with severity and category), and is a comment a
valid issue at all (the judge audit). The evaluation asks two more: which findings match which gold issues
(`match`), and whether each unmatched finding is valid anyway (`validity_many`). The judge sees findings under
neutral labels, never the policy, model or panel member that produced them (METRICS.md section 5, blinding).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from honed.core.types import Addressed, Severity, Stance


class JudgeError(ValueError):
    """The judge's answer could not be used (malformed, or inconsistent with the question)."""


@dataclass(frozen=True)
class ThreadComment:
    author: str
    role: str  # reviewer, PR author, bot, other
    body: str


@dataclass(frozen=True)
class ThreadEvidence:
    """What the judge sees of one review thread. Every field that came from the PR is untrusted data."""

    pr: str  # owner/name#number
    title: str
    thread_id: str
    path: str
    lines: tuple[int, int] | None  # flagged lines at the anchor commit; None for a file-level thread
    comments: tuple[ThreadComment, ...]
    anchor_code: str  # the flagged code where the comment was made (a numbered excerpt, or the diff hunk)
    head_code: str | None = None  # the same region at the PR's final head (the addressed check)
    side: str = "RIGHT"


@dataclass(frozen=True)
class AddressedVerdict:
    verdict: Addressed
    reason: str


@dataclass(frozen=True)
class ThreadClass:
    stance: Stance | None  # None when the thread has no reply
    category: str
    reason: str


@dataclass(frozen=True)
class GoldGroup:
    """Threads that raise one issue, with the issue's severity and category."""

    thread_ids: tuple[str, ...]
    severity: Severity
    category: str
    summary: str


@dataclass(frozen=True)
class Validity:
    valid: bool
    reason: str
    severity: Severity | None = None  # the evaluation's question only: the judge's own rating, Important or Nit


@dataclass(frozen=True)
class FindingEvidence:
    """What the judge sees of one review finding: a neutral label, the location, the text and the code. Nothing about
    who or what produced it."""

    label: str  # F1, F2, ... or D1, D2, ...
    path: str
    lines: tuple[int, int]
    text: str  # the comment: title and body
    code: str  # a numbered excerpt at the reviewed commit, flagged lines marked


@dataclass(frozen=True)
class GoldEvidence:
    label: str  # G1, G2, ...
    path: str
    lines: tuple[int, int]  # (0, 0) for a file-level issue
    description: str  # the issue in the judge's words (or the fix PR's, for an escaped defect)
    comment: str  # the human review comment that raised it ("" for an escaped defect)
    code: str


@dataclass(frozen=True)
class MatchVerdict:
    label: str  # a finding's or other note's label
    gold: str | None  # the gold label it reports, or None
    duplicate_of: str | None  # the finding label it repeats (findings only)
    reason: str


class Judge(Protocol):
    @property
    def model(self) -> str: ...

    def addressed(self, evidence: ThreadEvidence) -> AddressedVerdict: ...

    def classify(self, evidence: ThreadEvidence) -> ThreadClass: ...

    def gold_groups(
        self, pr: str, title: str, threads: Sequence[ThreadEvidence], *, sample: int = 0
    ) -> list[GoldGroup]:
        """Every thread in exactly one group."""
        ...

    def validity(self, evidence: ThreadEvidence, *, sample: int = 0) -> Validity:
        """Blind: the judge sees the first comment and the code it was made on, never replies or outcomes."""
        ...

    def match(self, pr: str, title: str, findings: Sequence[FindingEvidence], others: Sequence[FindingEvidence],
              gold: Sequence[GoldEvidence], *, sample: int = 0) -> list[MatchVerdict]:  # fmt: skip
        """Findings to gold issues, one-to-one (a finding may instead repeat another finding); `others` (dismissed
        and noted findings) to gold issues without the one-to-one rule. One verdict per label."""
        ...

    def validity_many(self, pr: str, title: str, findings: Sequence[FindingEvidence], *,
                      sample: int = 0) -> dict[str, Validity]:  # fmt: skip
        """Blind validity of several findings of one PR, each judged on its own, with the judge's own severity
        (Important or Nit; it never sees the severity the reviewer claimed); keyed by label."""
        ...
