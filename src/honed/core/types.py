"""Domain types: frozen dataclasses and enums. Pure data, no I/O.

Harvest types mirror what the code host reports (`PullRequest`, `Thread`, `Comment`, ...); labels derived from
them (`ThreadLabel`, `HarvestedPR`) are kept separate so raw data can be relabeled. Review and evaluation types
(`Finding`, `GoldIssue`, `Match`, `EvalResult`) follow METRICS.md sections 1-2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# --------------------------------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------------------------------


class Outcome(StrEnum):
    """What happened to a review thread (ARCHITECTURE.md section 5); `core.outcomes` decides, first match wins."""

    THUMBS_DOWN = "thumbs_down"
    FIXED = "fixed"
    RESOLVED_NO_CHANGE = "resolved_no_change"
    OPEN_AT_MERGE = "open_at_merge"
    IGNORED = "ignored"
    # Judged, never mechanical: the flagged lines changed, but the judge found the change did not address the
    # comment. Neither positive nor negative.
    CHANGED_UNADDRESSED = "changed_unaddressed"


class LineBasis(StrEnum):
    """How a thread's "lines changed" was decided."""

    COMPARE = "compare"  # flagged lines checked against compare patches
    IS_OUTDATED = "is_outdated"  # fallback: GitHub's isOutdated flag (unreliable after force-pushes)


class AuthorKind(StrEnum):
    """Who wrote something. `PR_AUTHOR` applies to review threads the PR's own author opened."""

    HUMAN = "human"
    AI = "ai"  # AI reviewers and coding agents
    BOT = "bot"  # other automation
    PR_AUTHOR = "pr_author"


class Corpus(StrEnum):
    HUMAN = "human"  # human-review corpus: PRs with at least one human inline thread; every thread is kept
    AI_FEEDBACK = "ai_feedback"  # AI-feedback corpus: only AI-bot threads are kept
    # The clean-PR set: a human reviewed, but opened no inline thread. Harvested from the human-review listing,
    # outside its quota (ARCHITECTURE.md section 11).
    APPROVAL_ONLY = "approval_only"


class PRSource(StrEnum):
    """Where a stored PR came from."""

    CORPUS = "corpus"  # harvested from the training corpus (`honed harvest`)
    BENCHMARK = "benchmark"  # a public benchmark's PR (`honed import-benchmark`): test split only, never trained on


class SampledAs(StrEnum):
    """How a harvested PR was sampled (ARCHITECTURE.md section 11); the evaluation reports by it."""

    GENERAL = "general"  # the repo's landed PRs, as listed
    TARGETED = "targeted"  # bug-targeted: a human requested changes in a review


class Severity(StrEnum):
    IMPORTANT = "important"
    PRE_EXISTING = "pre_existing"
    NIT = "nit"


class FindingClass(StrEnum):
    """METRICS.md section 1."""

    TP = "tp"
    VU = "vu"  # valid, unlabeled
    FP = "fp"
    DUP = "dup"


class GoldProvenance(StrEnum):
    """Where a gold issue came from; each maps to a `metrics.gold_conf` value."""

    HUMAN_FIXED = "human_fixed"
    APPLIED_SUGGESTION = "applied_suggestion"
    HUMAN_OPEN_AT_MERGE = "human_open_at_merge"
    ESCAPED_DEFECT = "escaped_defect"  # lines a later bug-fix PR rewrote, blamed back to this PR
    JUDGE_ONLY = "judge_only"
    BENCHMARK = "benchmark"  # a public benchmark's expert-verified golden comment (Martian, AACR-Bench)


class Stance(StrEnum):
    """A reply's stance toward the review comment it answers, as the judge classifies it."""

    AGREE = "agree"
    DISAGREE = "disagree"
    FIXED_ELSEWHERE = "fixed_elsewhere"
    QUESTION = "question"
    OTHER = "other"


class Addressed(StrEnum):
    """Whether the change to a `fixed` thread's flagged lines addressed the comment (the judge's addressed check)."""

    ADDRESSED = "addressed"
    PARTIALLY = "partially"
    NOT_ADDRESSED = "not_addressed"


class JudgmentKind(StrEnum):
    SUGGESTION = "suggestion"  # mechanical: was the thread's ```suggestion applied
    ADDRESSED = "addressed"  # judge: did the change address the comment
    CLASSIFY = "classify"  # judge: reply stance and category


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class Strength(StrEnum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


class LessonKind(StrEnum):
    PROMPT = "prompt"  # needs judgment: injected into a prompt
    CHECK = "check"  # a declarative rule run by a fixed engine (regex over added lines, pattern, threshold)


class LessonConfidence(StrEnum):
    """Lesson lifecycle (METRICS.md section 4): candidate -> recurring -> strong, or retired."""

    CANDIDATE = "candidate"
    RECURRING = "recurring"
    STRONG = "strong"
    RETIRED = "retired"


class Bucket(StrEnum):
    """The verifier's verdict on a candidate finding (ARCHITECTURE.md section 4)."""

    ACT_ON = "act_on"
    CONSIDER = "consider"
    NOTED = "noted"
    DISMISSED = "dismissed"


class CompareSource(StrEnum):
    GITHUB = "github"  # the host's compare patches
    CONTENT_DIFF = "content_diff"  # diffed locally from file contents (the base commit was force-pushed away)


class PackRole(StrEnum):
    CHANGED = "changed"  # a file the PR changed, at base or head
    REFERENCING = "referencing"  # references a symbol the PR changed
    TEST = "test"  # a test near the change
    GUIDANCE = "guidance"  # the repo's review guidance (REVIEW.md, CLAUDE.md) at the merge base


# --------------------------------------------------------------------------------------------------------------
# Code-host data
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PRKey:
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class DateWindow:
    """Inclusive ISO dates, `start..end`."""

    start: str
    end: str

    def __str__(self) -> str:
        return f"{self.start}..{self.end}"

    def contains(self, timestamp: str) -> bool:
        return self.start <= timestamp[:10] <= self.end


@dataclass(frozen=True)
class Actor:
    login: str
    typename: str = "User"  # the host's account type: User, Bot, Mannequin, Organization


@dataclass(frozen=True)
class Reaction:
    content: str  # THUMBS_UP, THUMBS_DOWN, LAUGH, HOORAY, CONFUSED, HEART, ROCKET, EYES
    user: str | None


@dataclass(frozen=True)
class Comment:
    id: str
    author: Actor | None
    body: str
    created_at: str
    diff_hunk: str = ""
    commit: str | None = None  # the commit the comment points at now
    original_commit: str | None = None  # the commit the comment was made on
    line: int | None = None
    original_line: int | None = None
    start_line: int | None = None
    original_start_line: int | None = None
    reactions: tuple[Reaction, ...] = ()
    reaction_count: int = 0  # the host's total, which may exceed len(reactions)


@dataclass(frozen=True)
class Thread:
    id: str
    path: str
    comments: tuple[Comment, ...]
    is_resolved: bool = False
    is_outdated: bool = False
    diff_side: str = "RIGHT"  # RIGHT: lines of the new file; LEFT: lines of the old file
    subject_type: str = "LINE"  # LINE or FILE
    line: int | None = None
    original_line: int | None = None
    start_line: int | None = None
    original_start_line: int | None = None
    resolved_by: str | None = None
    comment_count: int = 0  # the host's total, which may exceed len(comments)

    @property
    def first(self) -> Comment | None:
        return self.comments[0] if self.comments else None

    @property
    def created_at(self) -> str | None:
        return self.first.created_at if self.first else None

    @property
    def anchor_commit(self) -> str | None:
        """The commit the thread's first comment was made on."""
        return self.first.original_commit if self.first else None

    @property
    def flagged_lines(self) -> tuple[int, int] | None:
        """(start, end) of the flagged lines in the anchor commit's version of the file, or None (file-level)."""
        if self.subject_type == "FILE":
            return None
        first = self.first
        end = self.original_line if self.original_line is not None else (first.original_line if first else None)
        if end is None:
            return None
        start = self.original_start_line
        if start is None and first is not None:
            start = first.original_start_line
        return (min(start, end), end) if start is not None else (end, end)


@dataclass(frozen=True)
class Review:
    id: str
    author: Actor | None
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING
    submitted_at: str | None
    commit: str | None


@dataclass(frozen=True)
class CommitInfo:
    oid: str
    committed_date: str
    authored_date: str
    message_headline: str = ""  # harvested since phase 2; "" for PRs harvested before


@dataclass(frozen=True)
class FilePatch:
    path: str
    status: str  # added, removed, modified, renamed, copied, changed, unchanged
    patch: str | None  # unified-diff hunks; None when the host omitted them (binary or too large)
    previous_path: str | None = None


@dataclass(frozen=True)
class Compare:
    """File changes from `base` to `head`."""

    base: str
    head: str
    status: str  # ahead, behind, diverged, identical
    files: tuple[FilePatch, ...] = ()
    merge_base: str | None = None
    complete: bool = True  # False when the host capped the file list
    source: CompareSource = CompareSource.GITHUB

    @property
    def is_direct(self) -> bool:
        """True when the patches are the direct base-to-head diff. A host compare diffs from the merge base, which
        equals `base` only when `base` is an ancestor of `head` (status ahead or identical)."""
        return self.source is CompareSource.CONTENT_DIFF or self.status in ("ahead", "identical")

    def file(self, path: str) -> FilePatch | None:
        """The patch for `path`, following a rename away from it."""
        return next((f for f in self.files if f.path == path or f.previous_path == path), None)


@dataclass(frozen=True)
class RepoInfo:
    name: str
    default_branch: str | None
    language: str | None
    license: str | None = None  # SPDX id, as the host detects it ("NOASSERTION" when it can't tell)


@dataclass(frozen=True)
class PRSummary:
    """A landed PR as listed by the host's search, before its detail is fetched."""

    repo: str
    number: int
    title: str
    author: Actor | None
    created_at: str
    base_ref: str
    thread_count: int = 0
    review_authors: tuple[Actor | None, ...] = ()
    closed_by_commit: bool = False
    changes_requested_by: tuple[Actor | None, ...] = ()  # authors of CHANGES_REQUESTED reviews

    @property
    def key(self) -> PRKey:
        return PRKey(self.repo, self.number)


@dataclass(frozen=True)
class PRPage:
    items: tuple[PRSummary, ...]
    end_cursor: str | None
    has_next: bool


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    author: Actor | None
    created_at: str
    landed_at: str | None
    base_ref: str
    base_oid: str
    head_oid: str
    url: str = ""
    body: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    force_pushes: int = 0
    threads: tuple[Thread, ...] = ()
    reviews: tuple[Review, ...] = ()
    commits: tuple[CommitInfo, ...] = ()
    commit_count: int = 0  # the host's total, which may exceed len(commits)
    compares: tuple[Compare, ...] = ()  # each thread anchor commit -> final head, restricted to thread paths
    reviewed_diff: Compare | None = None  # merge base -> reviewed commit, all files

    @property
    def key(self) -> PRKey:
        return PRKey(self.repo, self.number)


# --------------------------------------------------------------------------------------------------------------
# Labels and harvest records
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadLabel:
    thread_id: str
    author_kind: AuthorKind
    outcome: Outcome
    lines_changed: bool  # whether a commit after the thread's anchor changed its flagged lines
    line_basis: LineBasis


@dataclass(frozen=True)
class HarvestedPR:
    pr: PullRequest
    language: str  # a `metrics.language_weights` key
    corpus: Corpus
    author_kind: AuthorKind
    reviewed_commit: str  # head of the first review round
    labels: tuple[ThreadLabel, ...] = ()
    sampled_as: SampledAs = SampledAs.GENERAL
    source: PRSource = PRSource.CORPUS
    split: str | None = None  # a fixed split (a benchmark's test, a bundle's assignment); None: time-ordered

    @property
    def key(self) -> PRKey:
        return self.pr.key


@dataclass(frozen=True)
class Cursor:
    """Resumable position in one repo's PR listing: the page start and how many of its items are processed."""

    after: str | None = None
    offset: int = 0
    exhausted: bool = False


@dataclass(frozen=True)
class PRFact:
    repo: str
    number: int
    language: str
    corpus: Corpus
    author_kind: AuthorKind
    created_at: str
    threads: int
    sampled_as: SampledAs = SampledAs.GENERAL
    source: PRSource = PRSource.CORPUS
    split: str | None = None  # a fixed split; None: assigned by time order


@dataclass(frozen=True)
class PriorThread:
    """A review thread on a file, for looking up earlier review of the same code."""

    repo: str
    number: int
    thread_id: str
    path: str
    created_at: str
    author_kind: AuthorKind
    outcome: Outcome


@dataclass(frozen=True)
class ThreadFact:
    repo: str
    number: int
    language: str
    corpus: Corpus
    author_kind: AuthorKind
    outcome: Outcome
    lines_changed: bool
    line_basis: LineBasis


# --------------------------------------------------------------------------------------------------------------
# Code reading and context packs
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PackBudget:
    """Caps on one context pack (`[harvest.pack]`)."""

    max_files: int
    max_bytes: int
    max_file_bytes: int
    max_symbols: int
    max_files_per_symbol: int
    max_referencing: int
    max_tests: int
    grep_full_tree_max_files: int
    grep_scope_max_files: int
    tree_listing: bool


@dataclass(frozen=True)
class GrepHit:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class PackFile:
    path: str
    commit: str
    role: PackRole
    reason: str  # why the file is in the pack
    blob: str  # content digest in the blob store
    size: int


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: str


@dataclass(frozen=True)
class ContextPack:
    """The code a review of one PR may read, saved at the reviewed commit (ARCHITECTURE.md section 8)."""

    repo: str
    number: int
    base_commit: str  # merge base of the reviewed commit
    head_commit: str  # the reviewed commit: head of the first review round
    files: tuple[PackFile, ...]
    changed_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()  # searched for referencing files
    common_symbols: tuple[str, ...] = ()  # found in too many files to rank on, so dropped
    grep_scope: tuple[str, ...] = ()  # pathspecs searched for referencing files; () means the whole tree
    skipped: tuple[SkippedFile, ...] = ()
    tree_blob: str | None = None  # newline-separated file list at head, when stored
    build_seconds: float = 0.0
    diff: Compare | None = None  # merge base -> head, for a later review round (round 1 uses the PR's reviewed diff)

    @property
    def key(self) -> PRKey:
        return PRKey(self.repo, self.number)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


# --------------------------------------------------------------------------------------------------------------
# Review, evaluation and policy
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    id: str
    path: str
    start_line: int
    end_line: int
    severity: Severity  # the claimed severity
    category: str  # correctness, security, design, tests, comments, performance, ...
    title: str
    body: str = ""
    raised_by: tuple[str, ...] = ()  # panel members or checks that raised it; 2+ is consensus
    evidence_level: int = 1  # the verifier's: 1 asserted, 2 cites the line, 3 traces the path, 4 ran code, 5 reproduced
    checked: str = ""  # what the verifier confirmed in the code itself, the basis of the evidence level
    bucket: Bucket | None = None  # set by the verifier
    bucket_reason: str = ""  # the verifier's one-line reason
    confidence: float = 1.0
    lessons_cited: tuple[str, ...] = ()
    intent_id: str | None = None  # the PR intent paragraph the finders judged against
    policy_hash: str = ""
    trace: str = ""  # the finder's evidence: the failing path, step by step

    @property
    def posted(self) -> bool:
        """Posted as a review comment: "act on" and "consider" findings (ARCHITECTURE.md section 4, render)."""
        return self.bucket in (Bucket.ACT_ON, Bucket.CONSIDER)

    @property
    def consensus(self) -> bool:
        """Raised independently by two or more panel members (checks don't count)."""
        return sum(not r.startswith("check:") for r in self.raised_by) >= 2


@dataclass(frozen=True)
class GoldIssue:
    """A real issue a reviewer should report on a PR. Lines are in the reviewed commit's version of the file
    (0-0 for a file-level issue)."""

    id: str
    path: str
    start_line: int
    end_line: int
    severity: Severity
    provenance: GoldProvenance
    conf: float  # provenance confidence, from `metrics.gold_conf`
    description: str = ""
    source_threads: tuple[str, ...] = ()  # the review threads merged into this issue, primary first
    category: str = ""


@dataclass(frozen=True)
class GoldSet:
    """A PR's gold issues, and what the build left out (ARCHITECTURE.md sections 5 and 6)."""

    repo: str
    number: int
    reviewed_commit: str
    issues: tuple[GoldIssue, ...]
    candidates: int = 0  # threads that qualified by outcome
    excluded_later_round: tuple[str, ...] = ()  # thread ids whose flagged code did not exist at the reviewed commit
    excluded_unreadable: tuple[str, ...] = ()  # thread ids whose code could not be read to apply that rule

    @property
    def key(self) -> PRKey:
        return PRKey(self.repo, self.number)


@dataclass(frozen=True)
class Judgment:
    """One verdict on one thread: a mechanical check or a judge call (`JudgmentKind`)."""

    repo: str
    number: int
    thread_id: str
    kind: JudgmentKind
    value: str  # suggestion: applied / not_applied / none; addressed: an `Addressed`; classify: a `Stance` or ""
    reason: str = ""
    category: str | None = None  # classify only
    model: str = ""  # the judge model, or "" for mechanical checks


@dataclass(frozen=True)
class JudgedLabel:
    """A thread's label after judging: the outcome (mechanical, or `changed_unaddressed`), and what it means as a
    training signal. A human dismissal of a high-risk finding is flagged and is never a negative label."""

    thread_id: str
    author_kind: AuthorKind
    outcome: Outcome
    polarity: Polarity | None  # None: a judgment it needs has not been made yet
    strength: Strength | None
    applied_suggestion: bool = False
    addressed: Addressed | None = None
    stance: Stance | None = None
    category: str | None = None
    high_risk_dismissal: bool = False


@dataclass(frozen=True)
class Match:
    """The judge's verdict on one finding: its class and, for a TP, the gold issue it matched. For a valid unlabeled
    finding (VU), `judged_severity` is the severity the judge rated it blind to the claimed one (Important or Nit;
    None for other classes and for runs judged before the judge rated severity)."""

    finding_id: str
    klass: FindingClass
    gold_id: str | None = None
    rationale: str = ""
    judged_severity: Severity | None = None


@dataclass(frozen=True)
class EvalResult:
    """One policy's review of one PR-round, judged against that round's gold issues. A PR-round is the unit of
    scoring; the bootstrap resamples whole PRs (ARCHITECTURE.md section 6)."""

    pr: PRKey
    language: str
    gold: tuple[GoldIssue, ...]
    findings: tuple[Finding, ...]  # the posted findings
    matches: tuple[Match, ...]
    cost_usd: float = 0.0
    latency_s: float = 0.0
    round: int = 1


@dataclass(frozen=True)
class LessonScope:
    languages: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()  # globs
    repos: tuple[str, ...] = ()


@dataclass(frozen=True)
class LessonStats:
    fires: int = 0
    precision: float | None = None


class CheckEngine(StrEnum):
    """The fixed engines that run `check` lessons (`review/checks.py`)."""

    ADDED_LINES_REGEX = "added_lines_regex"  # an added line matches `pattern` (and not `exclude`)
    STRUCTURAL_PATTERN = "structural_pattern"  # a file adds lines matching `select`, and every one matches `pattern`
    THRESHOLD = "threshold"  # the change pushes a file from under `threshold` lines to at least that many
    FINDING_TEXT = "finding_text"  # suppress only: a finding's title or body matches `pattern`


class CheckAction(StrEnum):
    FLAG = "flag"  # raise a finding
    SUPPRESS = "suppress"  # dismiss matching findings; never in a [safety] high_risk_categories category


@dataclass(frozen=True)
class CheckRule:
    """The declarative rule of a `check` lesson, run by a fixed engine. Data, never code."""

    engine: CheckEngine
    pattern: str = ""  # a regex (Python `re` syntax)
    threshold: int | None = None  # e.g. 1000 for "file crosses 1,000 lines"
    select: str = ""  # structural_pattern: the lines the rule is about (for example assertion lines)
    exclude: str = ""  # added lines matching this never match (for example comment lines)
    action: CheckAction = CheckAction.FLAG
    severity: Severity = Severity.NIT  # of the findings a flag check raises
    category: str = ""  # of the findings a flag check raises


@dataclass(frozen=True)
class Lesson:
    """A learned rule in `policy/lessons.yaml` (ARCHITECTURE.md section 3), in Bugbot-triage shape."""

    id: str
    kind: LessonKind
    scope: LessonScope
    text: str
    evidence: tuple[str, ...]  # PR refs and outcome ids
    applies_when: str = ""
    skip_when: str = ""  # for lessons that suppress findings
    do_not_skip_when: str = ""  # the risk boundary a suppressing lesson must respect
    example_signal: str = ""
    confidence: LessonConfidence = LessonConfidence.CANDIDATE
    stats: LessonStats = LessonStats()
    check: CheckRule | None = None  # required when kind is CHECK
    categories: tuple[str, ...] = ()  # the finding categories it is about; required for a suppressing lesson

    @property
    def suppresses(self) -> bool:
        """It can dismiss or downgrade findings: a skip rule, or a suppress check."""
        return bool(self.skip_when.strip()) or (self.check is not None and self.check.action is CheckAction.SUPPRESS)


@dataclass(frozen=True)
class PolicyVersion:
    """A policy is identified by the content hash of the `policy/` directory."""

    content_hash: str
    files: tuple[str, ...] = ()
