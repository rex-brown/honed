"""Public code-review benchmarks as held-out test data (ARCHITECTURE.md sections 6 and 11): their PRs and golden
comments, and how their labels map to ours. Pure data and functions.

Both benchmarks' repos are on `[corpus.exclude]`: their PRs are never harvested, labeled, mined or trained on. An
imported benchmark PR is stored with `source = "benchmark"` and the fixed split `test`, and its gold issues are the
benchmark's golden comments (provenance `benchmark`), not anything our judge derived.

Mapping:
- Martian Code Review Bench rates severity Critical / High / Medium / Low: the first three are Important (a defect
  worth blocking on), Low is a Nit. Its golden comments name no file or line, so their gold issues have none either
  (the judge matches them on the description).
- AACR-Bench has no severity, only four categories: a code defect or a security vulnerability is Important, a
  performance or maintainability remark a Nit. Its comments name a file and lines on the new side (`right`), which
  match our convention (lines at the reviewed commit); a comment on the old side is kept as a file-level issue.
  Comments its annotators rejected (`negative_samples.json`) are kept in the benchmark record, never as gold.

Export (`honed export-gold --format martian`): our gold issues in Martian's golden-comment shape, so a tool scored
by Martian's pipeline can be scored on our answer keys too. Martian's scorer uses a golden comment's text (its judge
matches on it) and its category (the strict, core and all profiles keep different categories); severity is
descriptive only. Our categories map to Martian's tags as the inverse of the import mapping where one exists
(`MARTIAN_TAG`); severity: Important is High, Nit is Low, and Pre-existing (outside the diff, which Martian has no
word for) is Low. Our gold descriptions go out as they are, without file or line, as Martian's own carry none.
"""

from __future__ import annotations

from dataclasses import dataclass

from honed.core.types import GoldIssue, GoldProvenance, GoldSet, PRKey, Severity

MARTIAN, AACR = "martian", "aacr"
TEST_SPLIT = "test"


@dataclass(frozen=True)
class BenchmarkComment:
    text: str
    severity: str = ""  # the benchmark's own label ("" when it has none)
    category: str = ""  # the benchmark's own category
    path: str = ""  # "" when the benchmark names no location
    start_line: int = 0
    end_line: int = 0
    side: str = "right"  # right: lines of the new file; left: of the old one
    valid: bool = True  # False: a comment the benchmark's annotators rejected


@dataclass(frozen=True)
class BenchmarkPR:
    benchmark: str  # MARTIAN or AACR
    url: str  # the reviewed PR
    repo: str  # owner/name, from the URL
    number: int
    title: str = ""
    language: str = ""  # the benchmark's language label, when it gives one (a GitHub language name)
    base_commit: str = ""  # the reviewed range, when the benchmark pins it (AACR-Bench)
    head_commit: str = ""
    original_url: str = ""  # Martian: the upstream PR a replicated one copies
    comments: tuple[BenchmarkComment, ...] = ()

    @property
    def key(self) -> PRKey:
        return PRKey(self.repo, self.number)

    @property
    def golden(self) -> tuple[BenchmarkComment, ...]:
        return tuple(c for c in self.comments if c.valid)


MARTIAN_SEVERITY = {"critical": Severity.IMPORTANT, "high": Severity.IMPORTANT, "medium": Severity.IMPORTANT,
                    "low": Severity.NIT}  # fmt: skip
MARTIAN_CATEGORY = {
    "bug": "correctness", "concurrency": "concurrency", "api": "types/contracts", "security": "security",
    "style": "style", "doc_defect": "documentation", "data": "correctness", "perf": "performance",
    "speculative": "other", "test_gap": "tests",
}  # fmt: skip
AACR_CATEGORY = {
    "code defect": (Severity.IMPORTANT, "correctness"),
    "security vulnerability": (Severity.IMPORTANT, "security"),
    "performance": (Severity.NIT, "performance"),
    "maintainability and readability": (Severity.NIT, "design"),
}


def classify(benchmark: str, comment: BenchmarkComment) -> tuple[Severity, str]:
    """Our severity and category for a golden comment (unknown labels: Important, `other`)."""
    if benchmark == MARTIAN:
        severity = MARTIAN_SEVERITY.get(comment.severity.strip().lower(), Severity.IMPORTANT)
        return severity, MARTIAN_CATEGORY.get(comment.category.strip().lower(), "other")
    return AACR_CATEGORY.get(comment.category.strip().lower(), (Severity.IMPORTANT, "other"))


def lines(comment: BenchmarkComment) -> tuple[int, int]:
    """The comment's lines at the reviewed commit, or (0, 0): no location, or the old side of the diff."""
    if not comment.path or comment.side.lower() != "right" or comment.start_line < 1:
        return 0, 0
    start, end = sorted((comment.start_line, comment.end_line or comment.start_line))
    return start, end


def gold_set(pr: BenchmarkPR, key: PRKey, reviewed_commit: str, conf: float) -> GoldSet:
    """The PR's gold issues: one per golden comment, provenance `benchmark`."""
    issues = []
    for n, comment in enumerate(pr.golden, 1):
        severity, category = classify(pr.benchmark, comment)
        start, end = lines(comment)
        issues.append(GoldIssue(
            id=f"{key}:{pr.benchmark}-{n}", path=comment.path, start_line=start, end_line=end, severity=severity,
            provenance=GoldProvenance.BENCHMARK, conf=conf, description=comment.text.strip(), category=category,
        ))  # fmt: skip
    issues.sort(key=lambda g: (g.path, g.start_line, g.id))
    return GoldSet(repo=key.repo, number=key.number, reviewed_commit=reviewed_commit, issues=tuple(issues),
                   candidates=len(pr.golden))  # fmt: skip


# ---- export to Martian's shape -------------------------------------------------------------------------------

MARTIAN_SEVERITIES = ("Critical", "High", "Medium", "Low")
MARTIAN_TAGS = ("bug", "security", "concurrency", "data", "api", "perf", "test_gap", "doc_defect", "style",
                "speculative")  # fmt: skip
MARTIAN_SEVERITY_OUT = {Severity.IMPORTANT: "High", Severity.PRE_EXISTING: "Low", Severity.NIT: "Low"}
MARTIAN_TAG = {
    "correctness": "bug", "error handling": "bug", "memory/undefined behavior": "bug", "billing": "bug",
    "idempotency": "bug", "types/contracts": "api", "compatibility": "api", "performance": "perf",
    "tests": "test_gap", "documentation": "doc_defect", "comments": "doc_defect", "design": "style",
    "style": "style", "security": "security", "privacy": "security", "auth": "security", "concurrency": "concurrency",
    "data retention": "data", "migrations/schema": "data", "other": "speculative",
}  # fmt: skip


def martian_comment(issue: GoldIssue) -> dict[str, str]:
    """One gold issue as a Martian golden comment: `{comment, severity, category}` (an unmapped category: `bug`)."""
    return {"comment": issue.description.strip(), "severity": MARTIAN_SEVERITY_OUT[issue.severity],
            "category": MARTIAN_TAG.get(issue.category, "bug")}  # fmt: skip


def martian_entry(title: str, url: str, issues: tuple[GoldIssue, ...]) -> dict[str, object]:
    """A PR's entry in a Martian golden-comments file: `{pr_title, url, comments}`."""
    return {"pr_title": title, "url": url, "comments": [martian_comment(g) for g in issues if g.description.strip()]}
