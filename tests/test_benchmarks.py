"""Benchmark adapters (Martian Code Review Bench, AACR-Bench): their files parse, their labels map to ours, and an
imported PR is test-split gold that is never labeled, moved to the clean-PR set, or put in the dev split. No network:
the code host is a fake."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from fakes import FakeCodeHost
from honed import config
from honed.adapters.bench_aacr import AACRBenchmark
from honed.adapters.bench_martian import MartianBenchmark
from honed.adapters.blobs import BlobStore
from honed.adapters.sqlite_store import SqliteStore
from honed.cli.wiring import label_keys
from honed.core.benchmarks import AACR, MARTIAN, BenchmarkComment, classify, lines
from honed.core.types import (
    Actor,
    Compare,
    Corpus,
    FilePatch,
    GoldProvenance,
    PRKey,
    PRSource,
    PullRequest,
    RepoInfo,
    Severity,
)
from honed.learn import splits
from honed.learn.benchmarks import BenchmarkImporter, ImportOptions
from honed.learn.label import mark_approval_only
from honed.ports.benchmark import BenchmarkError
from reviewkit import ROOT

SETTINGS = config.load(ROOT / "honed.toml")


def martian_files(tmp_path, *, extra: dict | None = None):
    golden = [{"pr_title": "Paginate audit logs", "url": "https://github.com/getsentry/sentry/pull/93824",
               "comments": [{"comment": "Negative slicing crashes Django querysets", "severity": "High",
                             "category": "bug"},
                            {"comment": "Unused import", "severity": "Low", "category": "style"}]}]  # fmt: skip
    (tmp_path / "golden_comments").mkdir(parents=True)
    (tmp_path / "golden_comments" / "sentry.json").write_text(json.dumps(golden))
    combined = {g["url"]: {"pr_title": g["pr_title"], "golden_comments": g["comments"], "source_repo": "sentry",
                           "reviews": []} for g in golden}  # fmt: skip
    combined.update(extra or {})
    (tmp_path / "benchmark_data.json").write_text(json.dumps(combined))
    return tmp_path


def aacr_files(tmp_path):
    def entry(url, comments):
        return {"githubPrUrl": url, "source_commit": "b" * 40, "target_commit": "h" * 40, "change_line_count": 9,
                "project_main_language": "Go", "category": "Bug Fix", "comments": comments}  # fmt: skip

    def comment(note, category, path="pkg/a.go", start=10, end=12, side="right"):
        return {"note": note, "category": category, "path": path, "from_line": start, "to_line": end, "side": side,
                "context": "Diff Level", "is_ai_comment": False, "source_model": ""}  # fmt: skip

    positive = [
        entry(
            "https://github.com/gofr-dev/gofr/pull/1",
            [comment("nil map write", "Code Defect"), comment("old side", "Performance", side="left")],
        )
    ]
    nit = comment("style nit", "Maintainability and Readability")
    negative = [
        entry("https://github.com/gofr-dev/gofr/pull/1", [comment("not a bug", "Code Defect")]),
        entry("https://github.com/gofr-dev/gofr/pull/2", [nit]),
    ]
    (tmp_path / "dataset").mkdir(parents=True)
    (tmp_path / "dataset" / "positive_samples.json").write_text(json.dumps(positive))
    (tmp_path / "dataset" / "negative_samples.json").write_text(json.dumps(negative))
    return tmp_path


def test_martian_files_parse_and_disagreements_are_errors(tmp_path):
    (pr,) = MartianBenchmark(martian_files(tmp_path / "a")).prs()
    assert (pr.repo, pr.number, pr.benchmark) == ("getsentry/sentry", 93824, MARTIAN)
    assert [c.severity for c in pr.golden] == ["High", "Low"] and all(c.path == "" for c in pr.golden)
    bad = martian_files(tmp_path / "b", extra={"https://github.com/getsentry/sentry/pull/1": {"golden_comments": []}})
    with pytest.raises(BenchmarkError, match="not in the golden comments"):
        MartianBenchmark(bad).prs()


def test_aacr_merges_confirmed_and_rejected_comments(tmp_path):
    first, second = AACRBenchmark(aacr_files(tmp_path)).prs()
    assert (first.base_commit, first.head_commit, first.language) == ("b" * 40, "h" * 40, "Go")
    assert [c.valid for c in first.comments] == [True, True, False]
    assert second.golden == () and len(second.comments) == 1  # only rejected comments: no gold


def test_severities_categories_and_lines_map_to_ours():
    assert classify(MARTIAN, BenchmarkComment("x", "Critical", "concurrency")) == (Severity.IMPORTANT, "concurrency")
    assert classify(MARTIAN, BenchmarkComment("x", "Medium", "doc_defect")) == (Severity.IMPORTANT, "documentation")
    assert classify(MARTIAN, BenchmarkComment("x", "Low", "style")) == (Severity.NIT, "style")
    assert classify(AACR, BenchmarkComment("x", category="Security Vulnerability")) == (Severity.IMPORTANT, "security")
    assert classify(AACR, BenchmarkComment("x", category="Maintainability and Readability")) == (Severity.NIT, "design")
    every = set(SETTINGS.label.categories)
    from honed.core.benchmarks import AACR_CATEGORY, MARTIAN_CATEGORY

    assert set(MARTIAN_CATEGORY.values()) <= every and {c for _, c in AACR_CATEGORY.values()} <= every
    assert lines(BenchmarkComment("x", path="a.go", start_line=12, end_line=10)) == (10, 12)
    assert lines(BenchmarkComment("x", path="a.go", start_line=3, end_line=4, side="left")) == (0, 0)
    assert lines(BenchmarkComment("x")) == (0, 0)


def _host() -> FakeCodeHost:
    diff = Compare("b" * 40, "h" * 40, "ahead", (FilePatch("pkg/a.go", "modified", "@@ -1 +1 @@\n-a\n+b"),),
                   merge_base="b" * 40)  # fmt: skip

    def pr(number: int) -> PullRequest:
        return PullRequest(repo="gofr-dev/gofr", number=number, title=f"PR {number}", author=Actor("dev"),
                           created_at="2025-02-01T00:00:00Z", landed_at=None, base_ref="main", base_oid="b" * 40,
                           head_oid="z" * 40)  # fmt: skip

    return FakeCodeHost(repos={"gofr-dev/gofr": RepoInfo("gofr-dev/gofr", "main", "Go")}, pages={},
                        prs={("gofr-dev/gofr", 1): pr(1), ("gofr-dev/gofr", 2): pr(2)},
                        compares={("b" * 40, "h" * 40): diff})  # fmt: skip


def test_an_imported_benchmark_pr_is_test_gold_and_never_trained_on(tmp_path):
    store = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    options = ImportOptions(SETTINGS.corpus.github_languages, SETTINGS.corpus.fallback_language, 1.0)
    report = BenchmarkImporter(_host(), store, store, options).run(AACRBenchmark(aacr_files(tmp_path / "files")))
    assert (report.listed, report.imported, report.gold_issues, report.rejected_comments, report.without_gold) == (
        2, 2, 2, 2, 1)  # fmt: skip

    key = PRKey("gofr-dev/gofr", 1)
    item = store.get_pr(key)
    assert (item.source, item.split, item.corpus, item.language) == (PRSource.BENCHMARK, "test", Corpus.HUMAN, "other")
    assert item.reviewed_commit == "h" * 40 and item.pr.threads == () and item.pr.reviewed_diff.files[0].path
    gold = store.get_gold(key)
    assert [(g.provenance, g.severity, g.conf, (g.start_line, g.end_line)) for g in gold.issues] == [
        (GoldProvenance.BENCHMARK, Severity.NIT, 1.0, (0, 0)),  # the old-side performance remark: file-level
        (GoldProvenance.BENCHMARK, Severity.IMPORTANT, 1.0, (10, 12)),
    ]
    assert [r.benchmark for _, r in store.benchmark_prs()] == [AACR, AACR]

    # Never labeled (its gold is the benchmark's), never moved to the clean-PR set, test only, never in dev.
    assert label_keys(store, SETTINGS, None, None) == []
    mark_approval_only(store)
    assert store.get_pr(key).corpus is Corpus.HUMAN
    facts = store.pr_facts()
    has_gold = {k for k in (PRKey(f.repo, f.number) for f in facts) if store.get_gold(k) is not None}
    items = splits.eligible(facts, has_gold, SETTINGS.corpus.excluded)
    fractions, dev = SETTINGS.eval.split_fractions, SETTINGS.eval.dev_split
    assert splits.select("test", items, fractions, dev) == [PRKey("gofr-dev/gofr", 1), PRKey("gofr-dev/gofr", 2)]
    assert splits.select("train", items, fractions, dev) == [] and splits.select(dev, items, fractions, dev) == []
    store.close()


def test_a_corpus_pr_with_a_fixed_split_keeps_it():
    from honed.core.types import AuthorKind, PRFact

    facts = [PRFact("o/r", n, "python", Corpus.HUMAN, AuthorKind.HUMAN, f"2026-01-0{n}T00:00:00Z", 1)
             for n in range(1, 6)]  # fmt: skip
    facts[0] = replace(facts[0], split="test")  # the oldest, fixed to test by a bundle
    items = splits.eligible(facts, {PRKey("o/r", n) for n in range(1, 6)}, ())
    assigned = splits.assign(items, {"train": 0.5, "validation": 0.25, "test": 0.25})
    assert assigned[PRKey("o/r", 1)] == "test" and assigned[PRKey("o/r", 2)] == "train"


def test_benchmark_repos_and_their_copies_are_excluded():
    excluded = SETTINGS.corpus.excluded
    for repo in ("ai-code-review-evaluation/sentry-greptile", "gofr-dev/gofr", "getsentry/sentry"):
        assert repo in excluded
