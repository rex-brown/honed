"""Unified-diff parsing: which old-file lines a patch changed, and diffs computed from two file versions."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from honed.core.types import FilePatch

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str  # the section heading after the second "@@" (often the enclosing function)
    lines: tuple[str, ...]  # body lines, each starting with " ", "-", "+" or "\"


@dataclass(frozen=True)
class OldSideChanges:
    """Changes described in old-file coordinates."""

    deleted: frozenset[int]  # old lines removed or replaced
    inserted_after: frozenset[int]  # old lines after which new lines were purely inserted (0: before line 1)


def parse_hunks(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    header: re.Match[str] | None = None
    body: list[str] = []
    for line in patch.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            if header is not None:
                hunks.append(_hunk(header, body))
            header, body = match, []
        elif header is not None and line[:1] in (" ", "-", "+", "\\", ""):
            body.append(line)
    if header is not None:
        hunks.append(_hunk(header, body))
    return hunks


def _hunk(header: re.Match[str], body: list[str]) -> Hunk:
    old_start, old_count, new_start, new_count, section = header.groups()
    return Hunk(
        old_start=int(old_start),
        old_count=1 if old_count is None else int(old_count),
        new_start=int(new_start),
        new_count=1 if new_count is None else int(new_count),
        header=section.strip(),
        lines=tuple(body),
    )


def old_side_changes(patch: str) -> OldSideChanges:
    deleted: set[int] = set()
    inserted_after: set[int] = set()
    for hunk in parse_hunks(patch):
        # With a zero count, the start is the line *before* the hunk; otherwise it is the hunk's first line.
        old = hunk.old_start + 1 if hunk.old_count == 0 else hunk.old_start
        replacing = False  # "+" lines right after "-" lines replace them rather than insert
        for line in hunk.lines:
            tag = line[:1]
            if tag == "-":
                deleted.add(old)
                old += 1
                replacing = True
            elif tag == "+":
                if not replacing:
                    inserted_after.add(old - 1)
            elif tag in (" ", ""):
                old += 1
                replacing = False
    return OldSideChanges(frozenset(deleted), frozenset(inserted_after))


def touches(patch: str, start: int, end: int, slack: int = 0) -> bool:
    """True when the patch removes, replaces or inserts next to any old-file line in [start - slack, end + slack]."""
    lo, hi = start - slack, end + slack
    changes = old_side_changes(patch)
    return any(lo <= n <= hi for n in changes.deleted) or any(lo - 1 <= n <= hi for n in changes.inserted_after)


def unified_diff(old: str, new: str, path: str, *, context: int = 0) -> str:
    """A unified diff from `old` to `new` (zero context by default), in the same hunk format hosts return."""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", n=context, lineterm=""
    ))  # fmt: skip
    return "\n".join(lines[2:])  # without the two file headers (a removed "--" line starts with "---" too)


def added_lines(patch: str) -> list[str]:
    """New-side lines the patch adds, without their "+" tag."""
    return [line[1:] for hunk in parse_hunks(patch) for line in hunk.lines if line.startswith("+")]


def removed_lines(patch: str) -> list[str]:
    return [line[1:] for hunk in parse_hunks(patch) for line in hunk.lines if line.startswith("-")]


def new_ranges(patch: str) -> list[tuple[int, int]]:
    """The new-file line range each hunk covers (a pure deletion: the line after it)."""
    out = []
    for hunk in parse_hunks(patch):
        start = hunk.new_start if hunk.new_count else hunk.new_start + 1
        out.append((start, max(start, start + hunk.new_count - 1)))
    return out


_GIT_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def parse_diff_file(text: str) -> list[FilePatch]:
    """The files of a unified diff (`git diff` output, or plain `---`/`+++` sections): path, status, hunks."""
    files: list[FilePatch] = []
    path = old = status = None
    body: list[str] = []

    def flush() -> None:
        if path is not None:
            hunks = "\n".join(body).strip("\n")
            files.append(FilePatch(path, status or "modified", hunks or None, old if old != path else None))

    for line in text.splitlines():
        git = _GIT_HEADER.match(line)
        if git or (line.startswith("--- ") and not body and path is None):
            flush()
            path, old, status, body = (git.group(2), git.group(1), None, []) if git else (None, None, None, [])
            if not git:
                old = line[4:].strip().removeprefix("a/")
            continue
        if line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "removed"
        elif line.startswith("rename from "):
            old, status = line[len("rename from ") :], "renamed"
        elif line.startswith("rename to "):
            path = line[len("rename to ") :]
        elif line.startswith("+++ ") and not body:
            target = line[4:].strip()
            if target != "/dev/null":
                path = target.removeprefix("b/")
            elif old:
                path, status = old, "removed"
        elif line.startswith("--- ") and not body:
            if line[4:].strip() == "/dev/null":
                status = "added"
        elif line.startswith("@@") or (body and line[:1] in (" ", "+", "-", "\\")):
            body.append(line)
    flush()
    return files
