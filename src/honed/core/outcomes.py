"""Mechanical thread outcomes (ARCHITECTURE.md section 5). First match wins:

1. `thumbs_down`: someone other than the finding's author reacted 👎 to it.
2. `fixed`: a later commit before merge changed the flagged lines. Decided from compare patches; GitHub's
   `isOutdated` is only the fallback, because force-pushes make it unreliable.
3. `resolved_no_change`: resolved, lines unchanged.
4. `open_at_merge`: unresolved and lines unchanged, but someone replied (a stance for the judge to classify).
5. `ignored`: unresolved, lines unchanged, no reply.
"""

from __future__ import annotations

from collections.abc import Iterable

from honed.core import patches
from honed.core.filters import is_bot
from honed.core.types import AuthorKind, Compare, LineBasis, Outcome, Thread, ThreadLabel

THUMBS_DOWN = "THUMBS_DOWN"


def has_thumbs_down(thread: Thread) -> bool:
    """A 👎 on the thread's first comment (the finding) from anyone but its author. The author is excluded because
    a reviewer bot pre-attaches 👍/👎 to its own comments."""
    first = thread.first
    if first is None:
        return False
    author = first.author.login if first.author else None
    return any(r.content == THUMBS_DOWN and r.user != author for r in first.reactions)


def has_reply(thread: Thread) -> bool:
    """Someone other than the finding's author, and not automation, commented after it."""
    first = thread.first
    if first is None:
        return False
    author = first.author.login if first.author else None
    return any(c.author is not None and c.author.login != author and not is_bot(c.author) for c in thread.comments[1:])


def compare_for(thread: Thread, compares: Iterable[Compare]) -> Compare | None:
    """The compare from the thread's anchor commit that can decide it: a direct diff listing the thread's file,
    else a complete direct diff (where the file's absence means it did not change), else any from the anchor.
    No anchor (GitHub drops `originalCommit` when it loses the commit) means no compare."""
    anchor = thread.anchor_commit
    if anchor is None:
        return None
    mine = [c for c in compares if c.base == anchor]
    direct = [c for c in mine if c.is_direct]
    listing = next((c for c in direct if c.file(thread.path)), None)
    return listing or next((c for c in direct if c.complete), None) or (mine[0] if mine else None)


def lines_changed_by_compare(thread: Thread, compare: Compare | None, slack: int) -> bool | None:
    """Whether a commit after the thread's anchor changed its flagged lines, or None when the compare can't tell:
    no compare, not a direct diff, a patch the host omitted, a capped file list, or a comment on the old side
    (whose line numbers refer to the base file, not the anchor commit's)."""
    if compare is None or not compare.is_direct or thread.diff_side != "RIGHT":
        return None
    changed = compare.file(thread.path)
    if changed is None:
        return False if compare.complete else None
    if changed.status == "removed":
        return True
    if changed.patch is None:
        return None
    flagged = thread.flagged_lines
    if flagged is None:  # a file-level thread: any change to the file counts
        return bool(changed.patch.strip())
    return patches.touches(changed.patch, flagged[0], flagged[1], slack)


def lines_changed(thread: Thread, compare: Compare | None, slack: int) -> tuple[bool, LineBasis]:
    """Compare patches decide; GitHub's `isOutdated` is the fallback."""
    by_compare = lines_changed_by_compare(thread, compare, slack)
    if by_compare is None:
        return thread.is_outdated, LineBasis.IS_OUTDATED
    return by_compare, LineBasis.COMPARE


def outcome(thread: Thread, changed: bool) -> Outcome:
    """The first matching rule of the module docstring."""
    if has_thumbs_down(thread):
        return Outcome.THUMBS_DOWN
    if changed:
        return Outcome.FIXED
    if thread.is_resolved:
        return Outcome.RESOLVED_NO_CHANGE
    if has_reply(thread):
        return Outcome.OPEN_AT_MERGE
    return Outcome.IGNORED


def label(thread: Thread, kind: AuthorKind, compares: Iterable[Compare], slack: int) -> ThreadLabel:
    changed, basis = lines_changed(thread, compare_for(thread, compares), slack)
    return ThreadLabel(
        thread_id=thread.id, author_kind=kind, outcome=outcome(thread, changed), lines_changed=changed, line_basis=basis
    )
