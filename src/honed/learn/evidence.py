"""What the judge sees of a review thread, read as of the right commits (ARCHITECTURE.md sections 5 and 6).

- The flagged code where the comment was made: the file at the thread's anchor commit, flagged lines marked.
- The same region at the PR's final head, followed through the change (the addressed check).
- The thread's location at the PR's reviewed commit (the head of the first review round), for the gold set's
  reviewed-commit rule: the anchor is the reviewed commit, or the flagged lines are unchanged between the two.
When a file can't be read at a commit, the comment's diff hunk (from the code host) stands in for the code.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from honed.core import lines as line_map
from honed.core.filters import is_bot
from honed.core.outcomes import compare_for
from honed.core.types import GrepHit, HarvestedPR, Thread
from honed.ports.code_reader import CodeReader
from honed.ports.judge import ThreadComment, ThreadEvidence

MAX_COMMENT_CHARS = 6000


class SerialReader:
    """A CodeReader whose calls never overlap: worker threads share one git clone, and concurrent fetches into a
    shallow clone collide on its lock files."""

    def __init__(self, reader: CodeReader) -> None:
        self._reader = reader
        self._lock = threading.Lock()

    def read(self, path: str, commit: str) -> str | None:
        with self._lock:
            return self._reader.read(path, commit)

    def grep(self, pattern: str, commit: str, paths: Sequence[str] | None = None, *, word: bool = False,
             max_per_file: int | None = None) -> list[GrepHit]:  # fmt: skip
        with self._lock:
            return self._reader.grep(pattern, commit, paths, word=word, max_per_file=max_per_file)

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        with self._lock:
            return self._reader.list_files(commit, prefix)

    def prefetch(self, commit: str, paths: Sequence[str]) -> None:
        with self._lock:
            self._reader.prefetch(commit, paths)


@dataclass(frozen=True)
class ReviewedLocation:
    """Where a thread's flagged code sits at the reviewed commit. `lines` is None when the code did not exist there
    (a later-round thread); `readable` is False when the files could not be read to tell."""

    lines: tuple[int, int] | None
    readable: bool = True


def comments(item: HarvestedPR, thread: Thread) -> tuple[ThreadComment, ...]:
    pr_author = item.pr.author.login if item.pr.author else None
    opener = thread.first.author.login if thread.first and thread.first.author else None
    out = []
    for comment in thread.comments:
        login = comment.author.login if comment.author else "(deleted account)"
        if comment.author is not None and login == pr_author:
            role = "PR author"
        elif is_bot(comment.author):
            role = "bot"
        elif login == opener:
            role = "reviewer"
        else:
            role = "other reviewer"
        body = comment.body if len(comment.body) <= MAX_COMMENT_CHARS else comment.body[:MAX_COMMENT_CHARS] + " [...]"
        out.append(ThreadComment(login, role, body))
    return tuple(out)


class EvidenceBuilder:
    def __init__(self, reader: CodeReader, context_lines: int) -> None:
        self._reader = reader
        self._context = context_lines

    def thread(self, item: HarvestedPR, thread: Thread, *, with_head: bool = False) -> ThreadEvidence:
        anchor_text = self._anchor_text(thread)
        flagged = thread.flagged_lines
        if anchor_text is not None and flagged is not None:
            anchor_code = line_map.excerpt(anchor_text, flagged[0], flagged[1], self._context)
        else:
            anchor_code = self._hunk(thread)
        head_code = self._head_code(item, thread, anchor_text) if with_head else None
        return ThreadEvidence(
            pr=str(item.key), title=item.pr.title, thread_id=thread.id, path=thread.path, lines=flagged,
            comments=comments(item, thread), anchor_code=anchor_code, head_code=head_code, side=thread.diff_side,
        )  # fmt: skip

    def reviewed_location(self, item: HarvestedPR, thread: Thread) -> ReviewedLocation:
        """The reviewed-commit rule (ARCHITECTURE.md section 6)."""
        return self.location_at(thread, item.reviewed_commit)

    def location_at(self, thread: Thread, reviewed: str) -> ReviewedLocation:
        """The reviewed-commit rule at any commit (a replayed round's): where the thread's flagged code sits at
        `reviewed`, when the anchor is `reviewed` or the flagged lines are unchanged between the two."""
        anchor, flagged = thread.anchor_commit, thread.flagged_lines
        if anchor is None:
            return ReviewedLocation(None, readable=False)
        if anchor == reviewed or thread.diff_side != "RIGHT":  # old-side lines belong to the base, not a round
            return ReviewedLocation(flagged or (0, 0))
        at_reviewed = self._reader.read(thread.path, reviewed)
        if at_reviewed is None:
            return ReviewedLocation(None)  # the file did not exist at the reviewed commit
        if flagged is None:
            return ReviewedLocation((0, 0))
        at_anchor = self._reader.read(thread.path, anchor)
        if at_anchor is None:
            return ReviewedLocation(None, readable=False)
        return ReviewedLocation(line_map.unchanged_back(at_reviewed, at_anchor, flagged[0], flagged[1]))

    # ---- pieces ------------------------------------------------------------------------------------------

    def _anchor_text(self, thread: Thread) -> str | None:
        if thread.anchor_commit is None or thread.diff_side != "RIGHT":
            return None
        return self._reader.read(thread.path, thread.anchor_commit)

    @staticmethod
    def _hunk(thread: Thread) -> str:
        hunk = thread.first.diff_hunk if thread.first else ""
        return ("Diff hunk the comment was made on (its last line is the commented line):\n" + hunk) if hunk else (
            "(code not available)")  # fmt: skip

    def _head_code(self, item: HarvestedPR, thread: Thread, anchor_text: str | None) -> str:
        head = item.pr.head_oid
        compare = compare_for(thread, item.pr.compares)
        changed = compare.file(thread.path) if compare else None
        head_path = changed.path if changed is not None and changed.status == "renamed" else thread.path
        head_text = self._reader.read(head_path, head)
        if head_text is None:
            return f"(the file {thread.path} does not exist at the final head)"
        moved = f"(the file was renamed to {head_path})\n" if head_path != thread.path else ""
        flagged = thread.flagged_lines
        if anchor_text is None or flagged is None:
            if changed is not None and changed.patch:
                return moved + "Changes to this file after the comment (unified diff):\n" + changed.patch[:8000]
            return moved + "(the region at the final head could not be located)"
        start, end = line_map.map_forward(anchor_text, head_text, flagged[0], flagged[1])
        if end < start:
            note = "(the flagged lines were deleted; the code around where they were:)\n"
            return moved + note + line_map.excerpt(head_text, start, start - 1, self._context)
        return moved + line_map.excerpt(head_text, start, end, self._context)
