"""Following lines of code between two versions of a file, and numbered excerpts for judge prompts.

Versions are compared line by line with difflib (no autojunk, so repeated lines such as "}" still align).
Line numbers are 1-based and ranges inclusive.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence


def _opcodes(old: Sequence[str], new: Sequence[str]) -> list[tuple[str, int, int, int, int]]:
    return difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()


def map_forward(old_text: str, new_text: str, start: int, end: int) -> tuple[int, int]:
    """The new-file range holding what old lines [start, end] became. When they were deleted outright, an empty
    range (end = start - 1) at the position they were removed from."""
    s0, e0 = start - 1, end  # 0-based, half-open
    lo: int | None = None
    hi: int | None = None
    anchor = None
    for tag, i1, i2, j1, j2 in _opcodes(old_text.splitlines(), new_text.splitlines()):
        if tag == "insert":
            if s0 < i1 < e0:  # inserted inside the range, not at its edges
                lo, hi = min(lo if lo is not None else j1, j1), max(hi if hi is not None else j2, j2)
            continue
        a, b = max(i1, s0), min(i2, e0)
        if a >= b:
            continue
        if tag == "equal":
            n1, n2 = j1 + (a - i1), j1 + (b - i1)
        elif tag == "replace":
            n1, n2 = j1, j2
        else:  # delete
            anchor = j1 if anchor is None else anchor
            continue
        lo, hi = min(lo if lo is not None else n1, n1), max(hi if hi is not None else n2, n2)
    if lo is None or hi is None:
        position = (anchor if anchor is not None else 0) + 1
        return position, position - 1
    return lo + 1, hi


def unchanged_back(old_text: str, new_text: str, start: int, end: int) -> tuple[int, int] | None:
    """Where new lines [start, end] sit in the old file, if every one of them is unchanged there; else None."""
    wanted = range(start - 1, end)
    mapped: list[int] = []
    equal = [(i1, j1, j2) for tag, i1, _i2, j1, j2 in _opcodes(old_text.splitlines(), new_text.splitlines())
             if tag == "equal"]  # fmt: skip
    for n in wanted:
        hit = next((i1 + n - j1 for i1, j1, j2 in equal if j1 <= n < j2), None)
        if hit is None:
            return None
        mapped.append(hit)
    return (min(mapped) + 1, max(mapped) + 1) if mapped else None


def excerpt(text: str, start: int, end: int, context: int, *, max_lines: int = 160) -> str:
    """Lines [start - context, end + context] of `text`, numbered, with the flagged lines [start, end] marked ">".
    An empty flagged range (end < start) marks nothing. Long excerpts keep their head and tail."""
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    lo = max(1, min(start, len(lines)) - context)
    hi = min(len(lines), max(end, start - 1) + context)
    width = len(str(hi))
    out = [f"{'>' if start <= n <= end else ' '}{n:>{width}} | {lines[n - 1]}" for n in range(lo, hi + 1)]
    if len(out) > max_lines:
        keep = max_lines // 2
        out = [*out[:keep], f"  ... {len(out) - 2 * keep} lines omitted ...", *out[-keep:]]
    return "\n".join(out)
