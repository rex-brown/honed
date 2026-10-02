"""Evaluation records (ARCHITECTURE.md section 6): what one replay evaluation stores, the decision log's rows, and
escaped-defect gold issues. Pure data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from honed.core.reviews import ReviewResult
from honed.core.types import EvalResult, GoldIssue, Match, PRKey


@dataclass(frozen=True)
class RoundRecord:
    """One PR-round of an evaluation: the review, the judge's verdicts on it, and the scoring input."""

    result: EvalResult  # the posted findings, their matches, and the round's gold issues
    review: ReviewResult  # everything the review produced, dismissed and noted findings included
    corpus: str  # human | approval_only (the clean-PR set)
    head_commit: str = ""
    other_matches: tuple[Match, ...] = ()  # judge matches of dismissed and noted findings (TP: a false dismissal)
    anchored: int = 0  # posted findings whose lines touch a changed hunk
    anchored_ids: tuple[str, ...] = ()  # ... their ids (runs stored before this field have only the count)


@dataclass(frozen=True)
class EvalRun:
    id: str
    policy_hash: str
    split: str
    backend: str  # the LLM backend (claude_code, replay)
    rounds: int  # review rounds replayed per PR, at most
    sample: int  # the reviewer's model sample index (0, or fresh samples for the noise floor)
    created_at: str
    llm_run: str  # the usage ledger's run id
    records: tuple[RoundRecord, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)  # PR-round -> why it was not scored
    prs: tuple[str, ...] = ()  # the PRs asked for (`owner/name#n`)
    stopped: str | None = None  # the run stopped early (a plan limit, the call cap): partial
    judge: str = ""  # the judge's fingerprint (`LLMJudge.fingerprint`, with the model that answered): "" before it

    @property
    def results(self) -> tuple[EvalResult, ...]:
        return tuple(r.result for r in self.records)


@dataclass(frozen=True)
class Decision:
    """A decision-log row (ARCHITECTURE.md section 7; pstack `show-me-your-work`)."""

    id: int | None
    at: str
    hypothesis: str
    change: str
    before: str
    after: str
    delta: str
    gate: str  # the gate results, as JSON text
    verdict: str
    note: str = ""


@dataclass(frozen=True)
class EscapedDefect:
    """A bug a later fix PR repaired, blamed back to the corpus PR that introduced the lines (SZZ)."""

    pr: PRKey  # the corpus PR that introduced the lines
    issue: GoldIssue  # Important, provenance escaped_defect, lines at the PR's reviewed commit
    fix_pr: int
    fix_title: str
    fix_commit: str  # the fix's merge commit; the lines were blamed at its first parent
    introduced_by: str  # the commit blame named
