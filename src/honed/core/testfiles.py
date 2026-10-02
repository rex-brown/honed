"""Which files are tests, and which tests sit near a changed file."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence

_TEST_DIR = re.compile(r"(^|/)(tests?|__tests__|specs?|testing|testdata|unittests?|e2e)(/|$)", re.I)
_TEST_NAME = re.compile(r"^(test_.+|.+_tests?|.+[._-](test|spec)s?|tests?|.+Tests?)\.[^/]+$")
_AFFIX = re.compile(r"^(test_)|(_tests?|[._-](test|spec)s?|Tests?)$")


def is_test(path: str) -> bool:
    return bool(_TEST_DIR.search(path) or _TEST_NAME.match(posixpath.basename(path)))


def stem(path: str) -> str:
    """The name a test and its subject share: `_ridge.py`, `test_ridge.py` and `ridge.test.ts` all give "ridge"."""
    name = posixpath.basename(path).split(".", 1)[0]
    name = _AFFIX.sub("", name)
    return name.lstrip("_").lower()


def distance(a: str, b: str) -> int:
    """Steps between the directories of two paths."""
    da = [p for p in posixpath.dirname(a).split("/") if p]
    db = [p for p in posixpath.dirname(b).split("/") if p]
    common = 0
    for x, y in zip(da, db, strict=False):
        if x != y:
            break
        common += 1
    return len(da) + len(db) - 2 * common


def near_tests(changed: Sequence[str], tree: Iterable[str], limit: int, max_distance: int = 4) -> list[tuple[str, str]]:
    """Tests named after a changed file, closest first, as (path, reason)."""
    subjects = [(stem(p), p) for p in changed if not is_test(p) and stem(p)]
    changed_set = set(changed)
    found: dict[str, tuple[int, str]] = {}
    for candidate in tree:
        if candidate in changed_set or not is_test(candidate):
            continue
        name = stem(candidate)
        for subject_stem, subject in subjects:
            if name == subject_stem:
                d = distance(candidate, subject)
                if d <= max_distance and (candidate not in found or d < found[candidate][0]):
                    found[candidate] = (d, f"test named after {subject}")
    ordered = sorted(found.items(), key=lambda item: (item[1][0], item[0]))
    return [(path, reason) for path, (_, reason) in ordered[:limit]]
