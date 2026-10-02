"""A CodeReader with no code: a review of a bare diff file reads nothing beyond the diff."""

from __future__ import annotations

from collections.abc import Sequence

from honed.core.types import GrepHit


class NullReader:
    def read(self, path: str, commit: str) -> str | None:
        return None

    def grep(self, pattern: str, commit: str, paths: Sequence[str] | None = None, *, word: bool = False,
             max_per_file: int | None = None) -> list[GrepHit]:  # fmt: skip
        return []

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        return []

    def prefetch(self, commit: str, paths: Sequence[str]) -> None:
        return None
