"""GitHub ```suggestion blocks, and whether a later commit applied one (ARCHITECTURE.md section 5).

A suggestion replaces the thread's flagged lines with its body. It counts as applied when the thread's patch
(anchor commit to final head) changed the flagged lines and the new code there contains the suggestion: every
non-trivial suggested line appears among the new-side lines of the hunks touching the flagged lines, and at least
one of them was added by the patch. An empty suggestion (delete the lines) counts when those hunks deleted the
flagged lines and added nothing. Failing that, a commit GitHub made from suggestions ("Apply suggestions from code
review", "Apply suggestion from @reviewer") after the comment counts, when the flagged lines changed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from honed.core import patches
from honed.core.types import CommitInfo, Thread

_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*suggestion\b[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.M | re.S)
_APPLY_COMMIT = re.compile(r"^apply suggestions? from (?:code review|@(?P<login>[\w-]+))", re.I)
_MIN_CHARS = 2  # suggested lines shorter than this (after stripping) prove nothing on their own


def blocks(body: str) -> list[str]:
    """The bodies of the ```suggestion blocks in a comment, in order."""
    return [match.group(2) for match in _BLOCK.finditer(body.replace("\r\n", "\n"))]


def suggestion(thread: Thread) -> str | None:
    """The suggestion in the thread's first comment (the last block, when it has several), or None."""
    first = thread.first
    found = blocks(first.body) if first else []
    return found[-1] if found else None


def applied_in_patch(suggested: str, patch: str, start: int, end: int) -> bool:
    """Whether `patch` (old = the anchor commit) replaced old lines [start, end] with the suggested text."""
    touching = [h for h in patches.parse_hunks(patch) if _touches(h, start, end)]
    if not touching:
        return False
    added = [line[1:].strip() for h in touching for line in h.lines if line.startswith("+")]
    new_side = added + [line[1:].strip() for h in touching for line in h.lines if line.startswith(" ")]
    wanted = [line.strip() for line in suggested.splitlines() if len(line.strip()) >= _MIN_CHARS]
    if not suggested.strip():
        deleted = any(line.startswith("-") for h in touching for line in h.lines)
        return deleted and not added
    if not wanted:
        return False
    return all(line in new_side for line in wanted) and any(line in added for line in wanted)


def _touches(hunk: patches.Hunk, start: int, end: int) -> bool:
    """Whether the hunk's own changes (not just its context lines) touch old lines [start, end]."""
    header = f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
    return patches.touches("\n".join([header, *hunk.lines]), start, end)


def applied_by_commit(thread: Thread, commits: Iterable[CommitInfo]) -> bool:
    """A suggestion commit made after the thread opened, from this reviewer's suggestions or a batch of them."""
    opened = thread.created_at or ""
    reviewer = thread.first.author.login if thread.first and thread.first.author else None
    for commit in commits:
        match = _APPLY_COMMIT.match(commit.message_headline.strip())
        if match and commit.committed_date >= opened and match.group("login") in (None, reviewer):
            return True
    return False
