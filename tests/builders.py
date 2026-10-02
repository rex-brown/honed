"""Small constructors for core types, so tests state only what matters to them."""

from __future__ import annotations

from honed.core.types import Actor, Comment, Compare, CompareSource, FilePatch, Reaction, Thread

ANCHOR = "a" * 40
HEAD = "f" * 40


def comment(
    login: str = "reviewer",
    *,
    body: str = "please fix",
    typename: str = "User",
    reactions: tuple[tuple[str, str], ...] = (),
    at: str = "2026-03-01T00:00:00Z",
    commit: str | None = ANCHOR,
    line: int | None = 10,
    cid: str | None = None,
) -> Comment:
    return Comment(
        id=cid or f"c-{login}-{at}",
        author=Actor(login, typename),
        body=body,
        created_at=at,
        diff_hunk="@@ -1,3 +1,3 @@\n a\n-b\n+c",
        commit=commit,
        original_commit=commit,
        line=line,
        original_line=line,
        reactions=tuple(Reaction(content, user) for content, user in reactions),
        reaction_count=len(reactions),
    )


def thread(
    *comments: Comment,
    path: str = "src/app.py",
    lines: tuple[int, int] | None = (10, 10),
    resolved: bool = False,
    outdated: bool = False,
    side: str = "RIGHT",
    tid: str = "t1",
) -> Thread:
    start, end = lines if lines else (None, None)
    return Thread(
        id=tid,
        path=path,
        comments=comments or (comment(),),
        is_resolved=resolved,
        is_outdated=outdated,
        diff_side=side,
        subject_type="LINE" if lines else "FILE",
        line=end,
        original_line=end,
        start_line=start if start != end else None,
        original_start_line=start if start != end else None,
        comment_count=len(comments or (None,)),
    )


def compare(*files: FilePatch, status: str = "ahead", complete: bool = True, base: str = ANCHOR) -> Compare:
    return Compare(base=base, head=HEAD, status=status, files=files, complete=complete)


def content_compare(*files: FilePatch, base: str = ANCHOR) -> Compare:
    return Compare(
        base=base, head=HEAD, status="diverged", files=files, complete=False, source=CompareSource.CONTENT_DIFF
    )


def patch(path: str, body: str | None, status: str = "modified", previous: str | None = None) -> FilePatch:
    return FilePatch(path=path, status=status, patch=body, previous_path=previous)
