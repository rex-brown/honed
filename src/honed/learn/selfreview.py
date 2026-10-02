"""Self-review (ARCHITECTURE.md section 7, step 2; METRICS.md section 3, rule 9): the incumbent policy reviews the
candidate's `policy/` diff through the normal review pipeline, and any posted Important finding rejects the candidate.

The diff becomes a synthetic pull request: one changed file per policy file the candidate changes (a unified diff
with context), the proposal's change as the title and its hypothesis as the description, read through an in-memory
code reader that holds the incumbent's files at the base commit and the candidate's at the head. Commits are named
after the two policies' hashes. Like any PR text, the title and description reach the reviewer as untrusted data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from honed.core import patches
from honed.core.improve import Proposal, SelfReview
from honed.core.reviews import ReviewRequest
from honed.core.types import FilePatch, GrepHit, Severity
from honed.ports.reviewer import Reviewer

REPO = "honed/policy"  # the synthetic PR's repository name
CONTEXT = 3


class MemoryReader:
    """A `CodeReader` over files held in memory, keyed by (path, commit)."""

    def __init__(self, files: Mapping[tuple[str, str], str]) -> None:
        self._files = dict(files)

    def read(self, path: str, commit: str) -> str | None:
        return self._files.get((path, commit))

    def grep(self, pattern: str, commit: str, paths: Sequence[str] | None = None, *, word: bool = False,
             max_per_file: int | None = None) -> list[GrepHit]:  # fmt: skip
        regex = re.compile(rf"\b(?:{pattern})\b" if word else pattern)
        hits = []
        for (path, at), text in sorted(self._files.items()):
            if at != commit or (paths and not any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths)):
                continue
            found = 0
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(GrepHit(path, n, line))
                    found += 1
                    if max_per_file and found >= max_per_file:
                        break
        return hits

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        return sorted(path for path, at in self._files if at == commit and path.startswith(prefix))

    def prefetch(self, commit: str, paths: Sequence[str]) -> None:
        return None


@dataclass(frozen=True)
class SyntheticPR:
    request: ReviewRequest
    reader: MemoryReader


def synthetic_pr(proposal: Proposal, old: Mapping[str, str], new: Mapping[str, str], *, parent_hash: str,
                 candidate_hash: str, prefix: str, language: str) -> SyntheticPR:  # fmt: skip
    """The candidate's diff as a pull request against the incumbent (paths relative to the policy directory in
    `old` and `new`; `prefix` is that directory relative to the project root)."""
    base, head = parent_hash[:40], candidate_hash[:40]
    files, held = [], {}
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        full = prefix + path
        status = "added" if before is None else "removed" if after is None else "modified"
        files.append(FilePatch(full, status, patches.unified_diff(before or "", after or "", full, context=CONTEXT)))
        if before is not None:
            held[(full, base)] = before
        if after is not None:
            held[(full, head)] = after
    for path, text in old.items():  # the rest of the policy, readable at both commits
        if path in new and new[path] == text:
            held[(prefix + path, base)] = held[(prefix + path, head)] = text
    body = f"{proposal.hypothesis}\n\nEvidence: {'; '.join(proposal.evidence) or 'none given'}"
    request = ReviewRequest(
        repo=REPO, number=None, title=f"Policy change: {proposal.change}"[:200], body=body, author="improve-loop",
        language=language, base_commit=base, head_commit=head, files=tuple(files),
    )  # fmt: skip
    return SyntheticPR(request, MemoryReader(held))


def self_review(reviewer: Reviewer, pr: SyntheticPR) -> SelfReview:
    """The incumbent's review of the candidate; any posted Important finding fails it."""
    result = reviewer.review(pr.request, pr.reader)
    findings = tuple(
        {"severity": f.severity.value, "bucket": f.bucket.value if f.bucket else None, "path": f.path,
         "line": f.start_line, "category": f.category, "title": f.title, "reason": f.bucket_reason}
        for f in result.findings
    )  # fmt: skip
    important = sum(f.severity is Severity.IMPORTANT for f in result.posted)
    return SelfReview(important=important, findings=findings, cost_usd=result.cost_usd)
