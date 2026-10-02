"""Lesson acceptance (ARCHITECTURE.md section 7, pstack `reflect`): the mechanical part of the rules a mined lesson
must meet before the loop spends an evaluation on it. The evaluation itself decides the rest (a lesson that changes
no decision scores no gain and fails the gate).

- Durable: no commit hashes, version numbers or file paths in its text (they drift); scope globs are fine.
- Specific: a prompt lesson says when it applies.
- Evidence: it cites at least `[lessons] min_prs` PRs of the feed (`owner/name#N`), by at least `min_authors`
  different PR authors, which also stops one person from poisoning the data. The feed is what `learn/feed.py`
  admits: the feed split's PRs and the AI-feedback PRs created before their language's validation cut. Anything
  else (the validation and test splits, benchmark repos, AI-feedback PRs from the validation weeks on) doesn't count,
  and citing it is an error.
- Not already covered: a new lesson whose text is close to an active one should strengthen that one instead.
- A check before a prompt: a new prompt lesson says why a `check` lesson can't express it.
- A flag check fires on its evidence: its pattern matches a line one of the cited PRs added, within its scope.
The safety invariant (no lesson suppresses or downgrades a high-risk finding) is enforced by the policy loader when
the edited policy loads (`core.policy`), so it holds here too.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from honed.core import patches
from honed.core.improve import EditKind, Proposal
from honed.core.policy import Policy, lesson_text, path_in_scope
from honed.core.types import CheckAction, CheckEngine, FilePatch, Lesson, LessonKind, PRKey

PR_REF = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)+\b|\bv\d+\.\d+\b")
_PATH = re.compile(r"(?<![\w*])(?:[\w.-]+/)+[\w-]+\.\w{1,6}\b")
_WORD = re.compile(r"[a-z0-9_]{3,}")
COVERED = 0.5  # word overlap (Jaccard) of two lesson texts above which a new lesson repeats an existing one


@dataclass(frozen=True)
class EvidencePR:
    """What acceptance needs to know about a PR of the feed."""

    key: PRKey
    author: str
    files: tuple[FilePatch, ...]  # the reviewed diff


@dataclass(frozen=True)
class LessonRules:
    min_prs: int
    min_authors: int


def cited_prs(evidence: Sequence[str]) -> list[PRKey]:
    out: list[PRKey] = []
    for item in evidence:
        for repo, number in PR_REF.findall(item):
            key = PRKey(repo, int(number))
            if key not in out:
                out.append(key)
    return out


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _fires(lesson: Lesson, prs: Sequence[EvidencePR]) -> bool:
    rule = lesson.check
    if rule is None or rule.action is not CheckAction.FLAG or rule.engine not in (
        CheckEngine.ADDED_LINES_REGEX, CheckEngine.STRUCTURAL_PATTERN
    ):  # fmt: skip
        return True  # thresholds and suppress checks need file contents or findings: the evaluation decides
    pattern = re.compile(rule.pattern)
    exclude = re.compile(rule.exclude) if rule.exclude else None
    select = re.compile(rule.select) if rule.select else None
    for pr in prs:
        for f in pr.files:
            if not f.patch or not path_in_scope(f.path, lesson.scope.paths):
                continue
            for line in patches.added_lines(f.patch):
                if exclude and exclude.search(line):
                    continue
                if select is not None and not select.search(line):
                    continue
                if pattern.search(line):
                    return True
    return False


def problems(proposal: Proposal, candidate: Policy, incumbent: Policy, feed: Mapping[PRKey, EvidencePR],
             rules: LessonRules) -> list[str]:  # fmt: skip
    """Why a lesson add or change fails acceptance (empty when it passes). Other edits pass untouched."""
    edit = proposal.edit
    if edit.kind not in (EditKind.LESSON_ADD, EditKind.LESSON_CHANGE):
        return []
    lesson_id = edit.lesson_id or str((edit.lesson or {}).get("id", ""))
    lesson = next((item for item in candidate.lessons if item.id == lesson_id), None)
    if lesson is None:
        return [f"lesson {lesson_id!r} is not in the edited policy"]
    out = []
    text = lesson_text(lesson)
    for what, pattern in (("a commit hash", _SHA), ("a version number", _VERSION), ("a file path", _PATH)):
        found = pattern.search(text)
        if found:
            out.append(f"not durable: names {what} ({found.group(0)!r})")
    if lesson.kind is LessonKind.PROMPT and not lesson.applies_when.strip():
        out.append("not specific: a prompt lesson says when it applies (applies_when)")
    cited = cited_prs(lesson.evidence)
    outside = [str(k) for k in cited if k not in feed]
    if outside:
        out.append(f"cites PRs outside the feed split (or AI-feedback PRs past the validation cut): {outside[:5]}")
    inside = [feed[k] for k in cited if k in feed]
    authors = {pr.author for pr in inside}
    if len(inside) < rules.min_prs or len(authors) < rules.min_authors:
        out.append(f"evidence from {len(inside)} feed-split PRs by {len(authors)} authors; needs {rules.min_prs} PRs "
                   f"by {rules.min_authors} authors")  # fmt: skip
    if edit.kind is EditKind.LESSON_ADD:
        close = [(overlap(lesson.text, other.text), other.id) for other in incumbent.active_lessons]
        near = [lid for score, lid in sorted(close, reverse=True) if score >= COVERED]
        if near:
            out.append(f"already covered by {near[0]!r}: strengthen it with a lesson_change instead")
        if lesson.kind is LessonKind.PROMPT and not proposal.why_not_check.strip():
            out.append("a prompt lesson must say why a check lesson can't express it (why_not_check)")
    if lesson.kind is LessonKind.CHECK and inside and not _fires(lesson, inside):
        out.append("not decision-changing: the check fires on none of the PRs it cites")
    return out
