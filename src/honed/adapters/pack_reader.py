"""CodeReader for offline runs: serves only what a PR's context pack holds, and counts what it could not serve.

A file the PR did not change is identical at base and head, so a read at either commit is served from whichever
version the pack holds. Any other read returns None ("not available") and is logged as a miss, which is how the
pack hit rate (METRICS.md section 7) is measured.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from honed.core.types import ContextPack, GrepHit

log = logging.getLogger(__name__)


@dataclass
class ReadStats:
    hits: int = 0
    misses: list[tuple[str, str]] = field(default_factory=list)  # (path, commit)

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + len(self.misses)
        return self.hits / total if total else None


class PackReader:
    def __init__(self, pack: ContextPack, get_blob: Callable[[str], bytes]) -> None:
        self._pack = pack
        self._get_blob = get_blob
        self._files = {(f.path, f.commit): f.blob for f in pack.files}
        self._changed = set(pack.changed_paths)
        self._tree: list[str] | None = None
        self.stats = ReadStats()

    def _blob_for(self, path: str, commit: str) -> str | None:
        digest = self._files.get((path, commit))
        if digest is None and path not in self._changed and commit in (self._pack.base_commit, self._pack.head_commit):
            other = self._pack.head_commit if commit == self._pack.base_commit else self._pack.base_commit
            digest = self._files.get((path, other))
        return digest

    def read(self, path: str, commit: str) -> str | None:
        digest = self._blob_for(path, commit)
        if digest is None:
            self.stats.misses.append((path, commit))
            log.info("pack miss: %s at %s (%s)", path, commit[:10], self._pack.key)
            return None
        self.stats.hits += 1
        return self._get_blob(digest).decode("utf-8", errors="replace")

    def grep(
        self,
        pattern: str,
        commit: str,
        paths: Sequence[str] | None = None,
        *,
        word: bool = False,
        max_per_file: int | None = None,
    ) -> list[GrepHit]:
        """Searches only the pack's files at `commit`; files outside the pack are not searched."""
        regex = re.compile(rf"\b(?:{pattern})\b" if word else pattern)
        hits: list[GrepHit] = []
        for path in sorted({p for p, _ in self._files}):
            if paths and not _under(path, paths):
                continue
            digest = self._blob_for(path, commit)
            if digest is None:
                continue
            found = 0
            for number, line in enumerate(self._get_blob(digest).decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(GrepHit(path, number, line))
                    found += 1
                    if max_per_file and found >= max_per_file:
                        break
        return hits

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        """The full file list at the reviewed commit when the pack stored it; otherwise just the pack's files."""
        if self._tree is None:
            if self._pack.tree_blob:
                self._tree = self._get_blob(self._pack.tree_blob).decode().split("\n")
            else:
                self._tree = sorted({p for p, _ in self._files})
        return [p for p in self._tree if p and (not prefix or _under(p, [prefix]))]

    def prefetch(self, commit: str, paths: Sequence[str]) -> None:
        return None


def _under(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)
