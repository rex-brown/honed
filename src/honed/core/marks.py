"""What an imported PR still waits for: the marks `Store.mark_stripped` keeps for it (ARCHITECTURE.md section 8).
Pure functions.

Code marks name a file whose patch the bundle left out (`reviewed:<path>`, `thread:<n>:<path>`,
`round:<commit>:<path>`); `honed rehydrate` restores them from git. Text marks name quoted text a stripped bundle
left out (`comment:<node id>:<sha256>`, `body:<sha256>` for the PR description), with the hash the text must have;
`honed rehydrate --comments` refetches it. A PR with any mark is not reviewed or replayed.
"""

from __future__ import annotations

from collections.abc import Iterable

CODE_PREFIXES = ("reviewed:", "thread:", "round:")
_COMMENT, _BODY = "comment:", "body:"


def comment_mark(comment_id: str, text_sha256: str) -> str:
    return f"{_COMMENT}{comment_id}:{text_sha256}"


def body_mark(text_sha256: str) -> str:
    return f"{_BODY}{text_sha256}"


def is_code(mark: str) -> bool:
    return mark.startswith(CODE_PREFIXES)


def is_text(mark: str) -> bool:
    return mark.startswith((_COMMENT, _BODY))


def code_marks(marks: Iterable[str]) -> list[str]:
    return [m for m in marks if is_code(m)]


def text_marks(marks: Iterable[str]) -> list[str]:
    return [m for m in marks if is_text(m)]


def parse_text(mark: str) -> tuple[str | None, str]:
    """(comment node id, or None for the PR description; the hash its text must have)."""
    if mark.startswith(_BODY):
        return None, mark.removeprefix(_BODY)
    comment_id, _, digest = mark.removeprefix(_COMMENT).rpartition(":")
    return comment_id, digest


def waiting_for(marks: Iterable[str]) -> str:
    """Why a PR with these marks can't be reviewed yet, and the command that fixes it."""
    marks = list(marks)
    code, text = any(map(is_code, marks)), any(map(is_text, marks))
    if code and text:
        return "imported without its code and comment text: run `honed rehydrate` and `honed rehydrate --comments`"
    if text:
        return "imported without its comment text: run `honed rehydrate --comments` first"
    return "imported without its code: run `honed rehydrate` first"
