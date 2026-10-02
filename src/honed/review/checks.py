"""The fixed engine that runs `check` lessons (ARCHITECTURE.md sections 3 and 4). A check is data: a regex over added
lines, a structural pattern, or a threshold. Nothing a lesson says is ever executed.

- `added_lines_regex`: an added line matches `pattern` and not `exclude`; one finding per file, at the first match.
- `structural_pattern`: a file adds lines matching `select` (for example assertions), and every one of them also
  matches `pattern` (for example a weak assertion).
- `threshold`: the change pushes a file from under `threshold` lines to at least that many.
- `finding_text` (suppress checks only): a finding's title or body matches `pattern`; the finding is dismissed, unless
  its category is high-risk (the safety invariant holds here too, whatever the loader let through).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import replace

from honed.core import patches
from honed.core.policy import path_in_scope
from honed.core.reviews import ReviewRequest
from honed.core.types import Bucket, CheckAction, CheckEngine, Finding, Lesson, LessonKind
from honed.review.context import FileView, ReviewContext


def _added(view: FileView) -> list[tuple[int, str]]:
    """(new-file line, text) of every added line."""
    out = []
    for hunk in patches.parse_hunks(view.patch.patch or ""):
        new = hunk.new_start
        for line in hunk.lines:
            if line.startswith("+"):
                out.append((new, line[1:]))
                new += 1
            elif line[:1] in (" ", ""):
                new += 1
    return out


def _finding(lesson: Lesson, view: FileView, line: int, detail: str, n: int) -> Finding:
    rule = lesson.check
    assert rule is not None
    return Finding(
        id=f"check:{lesson.id}:{n}", path=view.patch.path, start_line=line, end_line=line, severity=rule.severity,
        category=rule.category, title=lesson.text.split(". ")[0].rstrip("."), body=f"{lesson.text} {detail}".strip(),
        raised_by=(f"check:{lesson.id}",), lessons_cited=(lesson.id,),
    )  # fmt: skip


def _matches(lesson: Lesson, view: FileView) -> tuple[int, str] | None:
    """Where the check fires in this file, and what it saw; None when it doesn't."""
    rule = lesson.check
    assert rule is not None
    if rule.engine is CheckEngine.THRESHOLD:
        if view.head is None or rule.threshold is None:
            return None
        before = len(view.base.splitlines()) if view.base is not None else 0
        after = len(view.head.splitlines())
        if not before < rule.threshold <= after:
            return None
        added = sorted(view.added)
        line = next((n for n in added if n >= rule.threshold), added[-1] if added else after)
        return line, f"({before} lines before this change, {after} after.)"
    pattern = re.compile(rule.pattern)
    exclude = re.compile(rule.exclude) if rule.exclude else None
    lines = [(n, text) for n, text in _added(view) if not (exclude and exclude.search(text))]
    if rule.engine is CheckEngine.ADDED_LINES_REGEX:
        hits = [(n, text) for n, text in lines if pattern.search(text)]
        if not hits:
            return None
        n, text = hits[0]
        more = f" and {len(hits) - 1} more added lines" if len(hits) > 1 else ""
        return n, f"(Line {n}: `{text.strip()[:120]}`{more}.)"
    if rule.engine is CheckEngine.STRUCTURAL_PATTERN:
        selected = [(n, text) for n, text in lines if re.search(rule.select, text)]
        if not selected or not all(pattern.search(text) for _, text in selected):
            return None
        return selected[0][0], f"({len(selected)} added assertion lines, first at line {selected[0][0]}.)"
    return None


def run_checks(lessons: Sequence[Lesson], request: ReviewRequest, context: ReviewContext) -> list[Finding]:
    """Findings raised by the flag checks among `lessons` (already scoped to the review)."""
    findings: list[Finding] = []
    for lesson in lessons:
        rule = lesson.check
        if lesson.kind is not LessonKind.CHECK or rule is None or rule.action is not CheckAction.FLAG:
            continue
        if lesson.scope.languages and request.language not in lesson.scope.languages:
            continue
        for view in context.files:
            if not path_in_scope(view.patch.path, lesson.scope.paths) or view.patch.status == "removed":
                continue
            hit = _matches(lesson, view)
            if hit is not None:
                findings.append(_finding(lesson, view, hit[0], hit[1], len(findings) + 1))
    return findings


def _pattern(lesson: Lesson) -> str:
    return lesson.check.pattern if lesson.check is not None else r"(?!)"


def suppress(lessons: Sequence[Lesson], findings: Sequence[Finding], high_risk: Collection[str]) -> list[Finding]:
    """Findings after the suppress checks among `lessons`: a match is dismissed with the lesson cited, except in a
    high-risk category, which no lesson may touch."""
    rules = [lesson for lesson in lessons if lesson.check is not None and lesson.check.action is CheckAction.SUPPRESS]
    out = []
    for finding in findings:
        text = f"{finding.title}\n{finding.body}"
        hit = None
        if finding.category not in high_risk:
            hit = next((lesson for lesson in rules if finding.category in lesson.categories
                        and path_in_scope(finding.path, lesson.scope.paths)
                        and re.search(_pattern(lesson), text)), None)  # fmt: skip
        if hit is None:
            out.append(finding)
            continue
        out.append(replace(finding, bucket=Bucket.DISMISSED, bucket_reason=f"matches the team lesson `{hit.id}`",
                           lessons_cited=(*finding.lessons_cited, hit.id)))  # fmt: skip
    return out
