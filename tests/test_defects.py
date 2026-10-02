"""Escaped-defect mining on a synthetic repo: a corpus PR writes a line, a later fix rewrites it, blame links them."""

from __future__ import annotations

import re

from gitrepo import GitRepo
from honed.adapters.blobs import BlobStore
from honed.adapters.git_reader import GitReader
from honed.adapters.sqlite_store import SqliteStore
from honed.core.types import (
    Actor,
    AuthorKind,
    CommitInfo,
    Corpus,
    DateWindow,
    GoldProvenance,
    HarvestedPR,
    PRKey,
    PullRequest,
    Severity,
)
from honed.learn.defects import DefectMiner, DefectOptions, is_fix, locate, touched_old_lines
from honed.ports.code_host import MergedPR

PARSER = "def first(items):\n    value = items[0]\n    return value\n"
FIXED = "def first(items):\n    value = items[0] if items else None\n    return value\n"


class Host:
    def __init__(self, fixes: list[MergedPR], commits: dict[int, str]) -> None:
        self.fixes, self.commits = fixes, commits

    def list_merged_prs(self, repo, window, *, limit):
        return self.fixes[:limit]

    def merge_commit(self, repo, number):
        return self.commits.get(number)


def test_a_fix_is_blamed_back_to_the_corpus_pr_that_wrote_the_line(tmp_path):
    repo = GitRepo(tmp_path / "remote")
    repo.commit({"README.md": "hi\n"}, "initial")
    written = repo.commit({"lib/parse.py": PARSER}, "Add first() (#1)")  # the corpus PR, squash-merged
    repo.commit({"lib/other.py": "x = 1\n"}, "Unrelated change (#5)")
    fix = repo.commit({"lib/parse.py": FIXED}, "Fix crash on empty items (#2)")
    store = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    branch = "c" * 40  # the PR's own branch commit: not on main, so blame finds the squash commit instead
    pr = PullRequest("o/r", 1, "Add first()", Actor("dev"), "2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z", "main",
                     "b" * 40, written, commits=(CommitInfo(branch, "2026-02-01", "2026-02-01"),))  # fmt: skip
    store.upsert_pr(HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, written))
    host = Host([MergedPR(2, "Fix crash on empty items", (), "2026-08-01T00:00:00Z"),
                 MergedPR(5, "Unrelated change", (), "2026-08-02T00:00:00Z")], {2: fix})  # fmt: skip
    options = DefectOptions(DateWindow("2000-01-01", "2026-06-30"), 90, r"(?i)\bfix", ("bug",), 30, 15, 0.8, 10)
    history = GitReader(repo.url, tmp_path / "clone.git")
    report = DefectMiner(store, store, host, lambda r: history, options).mine("o/r")
    assert (report.listed, report.fixes, report.blamed_lines) == (2, 1, 1)
    (defect,) = report.defects
    assert defect.pr == PRKey("o/r", 1) and defect.fix_pr == 2 and defect.introduced_by == written
    issue = defect.issue
    assert (issue.path, issue.start_line, issue.end_line) == ("lib/parse.py", 2, 2)
    assert issue.severity is Severity.IMPORTANT and issue.provenance is GoldProvenance.ESCAPED_DEFECT
    assert issue.conf == 0.8 and "#2" in issue.description
    assert store.escaped_defects(PRKey("o/r", 1)) == [defect]
    again = DefectMiner(store, store, host, lambda r: history, options).mine("o/r")
    assert len(again.defects) == 1 and store.escaped_defects() == [defect]  # a re-run replaces, never duplicates
    store.close()


def test_fix_filters():
    title = re.compile(r"(?i)\bfix")
    assert is_fix(MergedPR(1, "Fix the parser", (), ""), title, ("bug",))
    assert is_fix(MergedPR(1, "Parser", ("Bug",), ""), title, ("bug",))
    assert not is_fix(MergedPR(1, "Add prefix support", (), ""), title, ("bug",))
    insertion = "@@ -3,0 +4,2 @@\n+a\n+b"
    rewrite = "@@ -2 +2 @@\n-x\n+y"
    refactor = "@@ -1,40 +1,2 @@\n" + "".join(f"-l{n}\n" for n in range(40)) + "+a\n+b"
    assert touched_old_lines(insertion, 30) == [] and touched_old_lines(refactor, 30) == []
    assert touched_old_lines(rewrite, 30) == [(2, 2)]
    assert locate("a\n  b\nc\n", ["b", "c"]) == (2, 3) and locate("a\n", ["z"]) is None
