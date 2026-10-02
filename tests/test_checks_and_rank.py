"""The check engine on the seed's check lessons, the runtime safety invariant, dedup and the comment lint."""

from __future__ import annotations

from dataclasses import replace

from honed.core import patches
from honed.core.reviews import ContextStats, ReviewRequest
from honed.core.types import (
    Bucket,
    CheckAction,
    CheckEngine,
    CheckRule,
    FilePatch,
    Finding,
    Lesson,
    LessonKind,
    LessonScope,
    Severity,
)
from honed.review import checks, rank, render
from honed.review.context import FileView, ReviewContext
from honed.review.verifier import Verifier
from reviewkit import BASE, HEAD, SETTINGS, seed_policy

HIGH_RISK = frozenset(SETTINGS.safety.high_risk_categories)


def lesson(lesson_id: str) -> Lesson:
    return next(item for item in seed_policy().lessons if item.id == lesson_id)


def view(path: str, before: str | None, after: str) -> FileView:
    patch = patches.unified_diff(before or "", after, path)
    status = "added" if before is None else "modified"
    added = frozenset(n for n in range(1, len(after.splitlines()) + 1)
                      if n not in {m for m in range(1, len((before or "").splitlines()) + 1)})  # fmt: skip
    return FileView(FilePatch(path, status, patch), after, before, added, tuple(patches.new_ranges(patch)))


def run(lesson_id: str, path: str, before: str | None, after: str, language: str = "typescript") -> list[Finding]:
    request = ReviewRequest("o/r", 1, "t", "", "dev", language, BASE, HEAD, ())
    context = ReviewContext("", "", (view(path, before, after),), ContextStats())
    return checks.run_checks([lesson(lesson_id)], request, context)


def test_file_crossing_1000_lines():
    small, big = "x = 1\n" * 990, "x = 1\n" * 1005
    (hit,) = run("file-over-1000-lines", "a.py", small, big, "python")
    assert hit.severity is Severity.IMPORTANT and hit.start_line == 1000 and "990 lines before" in hit.body
    assert run("file-over-1000-lines", "a.py", big, big + "y = 2\n", "python") == []  # already over


def test_new_suppressions():
    (hit,) = run("new-lint-suppression", "a.ts", "let a = 1;\n", "let a = 1;\n// @ts-ignore\nlet b: string = 2;\n")
    assert hit.start_line == 2 and hit.raised_by == ("check:new-lint-suppression",) and hit.category == "comments"
    assert run("new-lint-suppression", "a.py", "x = 1\n", "x = 1  # a note\n", "python") == []


def test_unvalidated_casts_and_any_in_typescript_only():
    cast = "const user = JSON.parse(raw) as User;\n"
    assert run("unvalidated-cast-of-external-data", "src/u.ts", None, cast)
    assert not run("unvalidated-cast-of-external-data", "src/u.ts", None, "const x = y as const;\n")
    assert not run("unvalidated-cast-of-external-data", "src/u.test.ts", None, cast)  # tests are out of scope
    assert not run("unvalidated-cast-of-external-data", "src/u.ts", None, cast, "python")
    assert run("any-in-new-code", "src/a.ts", None, "function f(x: any) {}\n")
    assert not run("any-in-new-code", "src/a.ts", None, "// x: any is fine in a comment\nconst many = 1;\n")
    assert not run("any-in-new-code", "src/a.d.ts", None, "declare const x: any;\n")


def test_tests_with_only_weak_assertions():
    weak = "test('it', () => {\n  expect(parse(x)).toBeDefined();\n  expect(fn).toHaveBeenCalled();\n});\n"
    strong = "test('it', () => {\n  expect(parse(x)).toBeDefined();\n  expect(parse(x)).toEqual([1]);\n});\n"
    assert run("weak-assertions-only", "src/parse.test.ts", None, weak)
    assert not run("weak-assertions-only", "src/parse.test.ts", None, strong)
    assert not run("weak-assertions-only", "src/parse.ts", None, weak)  # not a test file
    assert run("weak-assertions-only", "tests/test_x.py", None, "def test_x():\n    assert f() is not None\n",
               "python")  # fmt: skip


def _finding(category: str, title: str = "Unsanitized input reaches the SQL query") -> Finding:
    return Finding("p1", "a.py", 3, 3, Severity.IMPORTANT, category, title, raised_by=("a",))


def test_no_suppress_check_touches_a_high_risk_finding():
    rule = CheckRule(CheckEngine.FINDING_TEXT, pattern="SQL", action=CheckAction.SUPPRESS)
    hide = Lesson("hide-sql", LessonKind.CHECK, LessonScope(), "t", ("x",), skip_when="s", do_not_skip_when="d",
                  check=rule, categories=("correctness",))  # fmt: skip
    kept, gone = checks.suppress([hide], [_finding("security"), replace(_finding("correctness"), id="p2")], HIGH_RISK)
    assert kept.bucket is None and gone.bucket is Bucket.DISMISSED and "hide-sql" in gone.lessons_cited


def test_the_verifier_cannot_use_a_lesson_to_dismiss_or_downgrade_a_high_risk_finding():
    policy = seed_policy()
    skip = next(item for item in policy.lessons if item.suppresses)
    verifier = Verifier(None, policy, categories=SETTINGS.label.categories, high_risk=HIGH_RISK)  # type: ignore[arg-type]
    verdict = {"bucket": "dismissed", "severity": "nit", "evidence_level": 3, "confidence": 0.9, "reason": "skip",
               "checked": "walked the query path", "lessons": [skip.id], "duplicate_of": "", "title": "",
               "body": ""}  # fmt: skip
    (security, design) = verifier.apply([_finding("security"), replace(_finding("design", "Split this module"),
                                                                       id="p2")],
                                        ["P1", "P2"], [{**verdict, "id": "P1"}, {**verdict, "id": "P2"}],
                                        {skip.id: skip})  # fmt: skip
    assert security.bucket is Bucket.CONSIDER and security.severity is Severity.IMPORTANT
    assert "restored" in security.bucket_reason
    assert design.bucket is Bucket.DISMISSED and design.severity is Severity.NIT


def test_rank_never_caps_a_high_risk_category_at_nit():
    rules = replace(seed_policy().config.rank, nit_only_categories=("security",))  # the loader forbids this
    f = replace(_finding("security"), bucket=Bucket.ACT_ON, evidence_level=3, confidence=0.9)
    request = ReviewRequest("o/r", 1, "t", "", "dev", "python", BASE, HEAD, ())
    (ranked,) = rank.rank([f], request, rules, HIGH_RISK, "h").findings
    assert ranked.severity is Severity.IMPORTANT and ranked.bucket is Bucket.ACT_ON


def test_dedup_merges_the_same_issue_and_keeps_different_ones():
    a = Finding("a-1", "x.py", 10, 11, Severity.NIT, "correctness", "The loop skips the last element",
                raised_by=("a",))  # fmt: skip
    b = Finding("b-1", "x.py", 11, 11, Severity.IMPORTANT, "correctness", "Off by one: the loop skips the last element",
                raised_by=("b",))  # fmt: skip
    c = Finding("b-2", "x.py", 11, 11, Severity.NIT, "style", "Rename variable i", raised_by=("b",))
    merged, other = rank.merge_duplicates([a, b, c], slack=3)
    assert merged.raised_by == ("b", "a") and merged.severity is Severity.IMPORTANT and merged.consensus
    assert other.title == "Rename variable i" and not other.consensus


def test_the_comment_lint():
    clean = "`parse()` returns `None` for an empty file, and `main()` indexes it. Return an empty list instead."
    assert render.lint_text(clean) == []
    rules = {
        rule
        for rule, _ in render.lint_text(
            "This might potentially break — it is basically a crucial issue. Great work! The value is dropped."
        )
    }
    assert {"hedging", "filler", "ai_vocabulary", "praise", "em_dash", "passive_voice"} <= rules
    assert {rule for rule, _ in render.lint_text("Fix it. Then ship.", title=True)} >= {"title_period"}
