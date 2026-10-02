"""What a review takes and returns (ARCHITECTURE.md section 4). Pure data.

A `ReviewRequest` is everything the reviewer may know about a pull request at the commit it reviews: nothing from
later commits or later discussion, so a replayed round looks exactly like a live review. A `ReviewResult` holds every
finding with the verifier's bucket (posted findings are "act on" and "consider"), plus what the review cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from honed.core.types import FilePatch, Finding


@dataclass(frozen=True)
class EarlierComment:
    author: str
    role: str  # reviewer, PR author, bot
    body: str
    created_at: str


@dataclass(frozen=True)
class EarlierThread:
    """A review thread from an earlier round of the same PR, with the comments made before this round began."""

    path: str
    lines: tuple[int, int] | None  # at the commit the thread was made on
    comments: tuple[EarlierComment, ...]


@dataclass(frozen=True)
class PriorFinding:
    """One of this reviewer's findings from an earlier review of the same PR (re-review mode)."""

    finding: Finding
    dismissed: bool  # a human dismissed it (resolved without a change, 👎, or a disagreeing reply)


@dataclass(frozen=True)
class ReviewRequest:
    repo: str
    number: int | None  # None for a bare diff
    title: str
    body: str
    author: str
    language: str  # a `metrics.language_weights` key
    base_commit: str  # the merge base the diff starts from
    head_commit: str  # the commit under review
    files: tuple[FilePatch, ...]  # merge base -> head
    created_at: str = ""  # the PR's creation time: earlier threads on the same files come from before it
    commit_messages: tuple[str, ...] = ()  # headlines of the commits up to the head, oldest first
    round: int = 1
    earlier_threads: tuple[EarlierThread, ...] = ()
    prior_findings: tuple[PriorFinding, ...] = ()  # non-empty: re-review mode

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}" if self.number is not None else self.repo

    @property
    def rereview(self) -> bool:
        return bool(self.prior_findings)


@dataclass(frozen=True)
class StageUsage:
    """One stage's model calls in one review. Cost and seconds are what the calls took when they were made live,
    also when this run was served from the cache (the review's list-price cost, METRICS.md section 3)."""

    stage: str
    calls: int
    cost_usd: float
    seconds: float
    cached: int = 0


@dataclass(frozen=True)
class LintViolation:
    finding_id: str
    rule: str
    excerpt: str


@dataclass(frozen=True)
class ContextStats:
    reads: int = 0  # file reads of files that exist at the commit read
    served: int = 0  # ... that the code source answered (the pack hit rate, offline)
    lines: int = 0  # lines of diff and code in the context
    truncated: bool = False  # the context budget cut something


@dataclass(frozen=True)
class ReviewResult:
    policy_hash: str
    ref: str
    head_commit: str
    intent: str
    findings: tuple[Finding, ...]  # every finding after dedup, each with its bucket
    noted: int  # "noted" findings: counted, not posted
    act_on_flagged: bool  # more "act on" findings than the policy's act_on_flag
    lint: tuple[LintViolation, ...] = ()
    usage: tuple[StageUsage, ...] = ()
    latency_s: float = 0.0  # intent + the slowest panel member + checks + verifier, as the calls took live
    context: ContextStats = ContextStats()
    failures: tuple[str, ...] = ()  # panel members whose answer could not be used
    malformed: int = 0  # proposed findings dropped because they didn't parse (unknown category, bad lines)
    proposed: int = 0  # proposed findings that parsed (panel members' and checks'), before dedup; 0 in older runs
    rereview: bool = False

    @property
    def posted(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.posted)

    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)
