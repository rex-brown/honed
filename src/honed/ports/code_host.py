"""CodeHost: where PRs and their reviews live (GitHub)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from honed.core.types import Compare, DateWindow, PRPage, PullRequest, RepoInfo


@dataclass(frozen=True)
class MergedPR:
    """A merged PR as the host's issue search lists it (escaped-defect mining)."""

    number: int
    title: str
    labels: tuple[str, ...]
    merged_at: str


class HostError(RuntimeError):
    """The host could not answer (after retries)."""


class NotFound(HostError):
    """The host has no such object."""


class BudgetExhausted(HostError):
    """The run's API budget is spent; stop and resume later."""


class CodeHost(Protocol):
    def repo_info(self, repo: str) -> RepoInfo: ...

    def list_landed_prs(self, repo: str, window: DateWindow, *, after: str | None, page_size: int) -> PRPage:
        """PRs created in `window` that landed (by the repo's landing convention), newest first, one page."""
        ...

    def fetch_pr(self, repo: str, number: int, *, thread_hint: int | None = None) -> PullRequest:
        """Full detail: metadata, every review thread with its comments and reactions, reviews and commits.
        `compares` and `reviewed_diff` are left empty; `compare` fills them. `thread_hint` (the expected thread
        count) sizes the first page, since the host charges by requested page size."""
        ...

    def compare(self, repo: str, base: str, head: str) -> Compare:
        """File changes from the merge base of `base` and `head` to `head`."""
        ...

    def read_file(self, repo: str, path: str, commit: str) -> str | None:
        """A file's text at a commit, or None when it does not exist there or is binary."""
        ...

    def list_merged_prs(self, repo: str, window: DateWindow, *, limit: int) -> list[MergedPR]:
        """PRs merged in `window`, newest first, at most `limit` (escaped-defect mining)."""
        ...

    def merge_commit(self, repo: str, number: int) -> str | None:
        """The commit a merged PR landed as on its base branch (merge or squash commit)."""
        ...


class TextSource(Protocol):
    """Quoted text by id, for `honed rehydrate --comments` (a stripped bundle's review comments and PR bodies)."""

    def comment_bodies(self, ids: Sequence[str]) -> Mapping[str, str | None]:
        """Each review comment's current text by node id; None for one the host no longer has."""
        ...

    def pr_bodies(self, repo: str, numbers: Sequence[int]) -> Mapping[int, str | None]:
        """Each PR's current description; None for one the host no longer has."""
        ...
