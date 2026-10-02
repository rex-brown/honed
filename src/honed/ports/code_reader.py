"""CodeReader: read and search code as of a given commit, never the repo's current state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from honed.core.types import FilePatch, GrepHit


class ReaderError(RuntimeError):
    """The code could not be read (for example, a commit could not be fetched)."""


class ReaderUnavailable(ReaderError):
    """The code source is unreachable (the network is down). Stop and resume later rather than degrade."""


class CodeReader(Protocol):
    def read(self, path: str, commit: str) -> str | None:
        """The file's text at `commit`, or None when it is absent, binary, or not available (offline)."""
        ...

    def grep(
        self,
        pattern: str,
        commit: str,
        paths: Sequence[str] | None = None,
        *,
        word: bool = False,
        max_per_file: int | None = None,
    ) -> list[GrepHit]:
        """Lines matching `pattern` (a Perl-compatible regex; keep to the subset Python's `re` shares) at `commit`,
        within `paths` (files or directories; None for everything). `word` matches whole words only."""
        ...

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        """Paths of the files under `prefix` at `commit`."""
        ...

    def prefetch(self, commit: str, paths: Sequence[str]) -> None:
        """Hint that `paths` at `commit` will be read soon, so an implementation can batch downloads. May no-op."""
        ...


class Differ(Protocol):
    """Diffs between two commits, as the code host's compare gives them (`honed rehydrate`: a bundle carries no
    patches, so they are rebuilt from git)."""

    def diff(self, base: str, head: str, paths: Sequence[str]) -> dict[str, str | None]:
        """The unified-diff hunks (from the first `@@`, three lines of context) of each of `paths` that changed
        between `base` and `head`, keyed by its path at `head` (at `base` for a removed file); None for a binary
        file. List both names of a renamed file, so the rename is followed."""
        ...

    def read(self, path: str, commit: str) -> str | None: ...


class RepoReader(CodeReader, Differ, Protocol):
    """Reading code (context packs) and diffing commits (patches): what `honed rehydrate` needs of a repo."""


@dataclass(frozen=True)
class BlameLine:
    line: int  # in the blamed commit's version of the file
    commit: str  # the commit that last changed the line
    original_line: int  # the line's number in that commit
    text: str
    boundary: bool = False  # the commit is at the edge of the history fetched: the line is older than it


class History(Protocol):
    """A repo's commit history, for blaming lines back to the commits that wrote them (escaped-defect mining)."""

    def deepen(self, commit: str, since: str) -> None:
        """Make the history of `commit` back to the ISO date `since` available."""
        ...

    def commit_diff(self, commit: str) -> list[FilePatch]:
        """The changes `commit` made against its first parent, as zero-context hunks."""
        ...

    def blame(self, path: str, commit: str, start: int, end: int) -> list[BlameLine]:
        """Who last changed lines [start, end] of `path` as of `commit`."""
        ...

    def message(self, commit: str) -> str:
        """The commit message."""
        ...

    def read(self, path: str, commit: str) -> str | None: ...
