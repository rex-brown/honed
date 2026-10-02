"""`honed export-gold --format martian`: our gold issues as Martian Code Review Bench golden comments. The shape is
checked against what Martian's own pipeline reads (withmartian/code-review-benchmark at e616e849, `offline/`:
`step1_download_prs.py` `load_golden_comments`, `step3_judge_comments.py` `evaluate_review`, and the category
profiles of `analysis/score_profiles.py`), and against the golden files themselves when they are downloaded."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evalkit import GOLD, build_store
from honed import cli
from honed.core.benchmarks import (
    MARTIAN,
    MARTIAN_SEVERITIES,
    MARTIAN_TAG,
    MARTIAN_TAGS,
    BenchmarkComment,
    classify,
    martian_comment,
)
from honed.core.removals import Removals
from honed.core.types import GoldIssue, GoldProvenance, GoldSet, PRKey, PRSource, Severity
from honed.learn import gold_export
from honed.learn.bundle_text import ExportReport, TextOptions, TextPolicy
from reviewkit import HEAD, ROOT, SETTINGS, STORE

STRICT = {"bug", "security", "concurrency", "data", "api"}  # Martian's profiles: strict < core < all
CORE = STRICT | {"perf", "test_gap", "doc_defect"}


def load_like_martian(folder: Path) -> dict[str, dict]:
    """Martian's `load_golden_comments`, and the fields its judge step reads from each golden comment."""
    golden = {}
    for path in folder.glob("*.json"):
        for entry in json.loads(path.read_text()):
            golden[entry["url"]] = {"pr_title": entry.get("pr_title"), "original_url": entry.get("original_url"),
                                    "comments": entry.get("comments", []), "source_file": path.name}  # fmt: skip
    for value in golden.values():
        for gc in value["comments"]:
            assert isinstance(gc["comment"], str) and gc["comment"]
            gc.get("severity"), gc.get("category")
    return golden


def assert_martian_shape(entries: list[dict]) -> None:
    assert isinstance(entries, list) and entries
    for entry in entries:
        assert set(entry) == {"pr_title", "url", "comments"}
        assert entry["url"].startswith("https://github.com/") and "/pull/" in entry["url"]
        for c in entry["comments"]:
            assert set(c) == {"comment", "severity", "category"}
            assert c["severity"] in MARTIAN_SEVERITIES and c["category"] in MARTIAN_TAGS


def build(path: Path):
    """PRs 1 and 3 with one gold issue each (3's is a Nit about style), the clean PR 2 without a gold set, PR 4 with an
    empty gold set, and a benchmark PR 5 with the benchmark's own gold."""
    store = build_store(path, humans=("ann", "bob", "cy"))
    store.save_gold(GoldSet("o/r", 3, HEAD, (replace(GOLD, id="o/r#3:t3", severity=Severity.NIT, category="style",
                                                     source_threads=("t3",)),)))  # fmt: skip
    store.save_gold(GoldSet("o/r", 4, HEAD, ()))
    item = store.get_pr(PRKey("o/r", 1))
    bench = replace(item, pr=replace(item.pr, number=5, threads=()), labels=(), source=PRSource.BENCHMARK, split="test")
    store.upsert_pr(bench)
    store.save_gold(GoldSet("o/r", 5, HEAD, (GoldIssue("o/r#5:martian-1", "", 0, 0, Severity.IMPORTANT,
                                                       GoldProvenance.BENCHMARK, 1.0, "theirs"),)))  # fmt: skip
    return store


def test_gold_goes_out_in_martians_shape_with_mapped_severity_and_category(tmp_path):
    store = build(tmp_path)
    keys = store.pr_keys()
    result = gold_export.martian(store, store, keys, Removals(), TextPolicy(TextOptions(), ExportReport()))
    assert list(result.files) == ["o__r.json"]
    (entries,) = result.files.values()
    assert_martian_shape(entries)
    assert [e["url"] for e in entries] == ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/3"]
    assert entries[0]["comments"] == [{"comment": GOLD.description, "severity": "High", "category": "bug"}]
    assert entries[1]["comments"] == [{"comment": GOLD.description, "severity": "Low", "category": "style"}]
    assert (result.prs, result.issues, result.clean, result.no_gold) == (2, 2, 1, 1)  # the benchmark PR is theirs

    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "o__r.json").write_text(json.dumps(entries))
    golden = load_like_martian(tmp_path / "out")
    assert set(golden) == {e["url"] for e in entries}

    removed = Removals(prs=frozenset({PRKey("o/r", 3)}))
    fewer = gold_export.martian(store, store, keys, removed, TextPolicy(TextOptions(), ExportReport()))
    assert fewer.prs == 1 and fewer.removed == 1
    store.close()


def test_the_mapping_covers_every_category_and_inverts_the_import():
    assert set(SETTINGS.label.categories) <= set(MARTIAN_TAG) and set(MARTIAN_TAG.values()) <= set(MARTIAN_TAGS)
    assert {"bug", "security", "concurrency", "data", "api", "perf", "test_gap", "doc_defect", "style",
            "speculative"} == set(MARTIAN_TAGS)  # fmt: skip
    for tag in MARTIAN_TAGS:  # their tag -> our category (the import) -> the same tag (the export), except `data`,
        ours = classify(MARTIAN, BenchmarkComment("x", "High", tag))[1]  # which the import files as correctness
        assert MARTIAN_TAG[ours] == ("bug" if tag == "data" else tag)
    for severity, label in (("important", "High"), ("nit", "Low")):
        issue = replace(GOLD, severity=Severity(severity))
        assert martian_comment(issue)["severity"] == label
        assert classify(MARTIAN, BenchmarkComment("x", label, "bug"))[0] is Severity(severity)  # round trips
    high_risk = {MARTIAN_TAG[c] for c in SETTINGS.safety.high_risk_categories}
    assert high_risk <= CORE  # our high-risk issues all count under Martian's default (core) profile


def test_the_cli_writes_one_file_per_repo(tmp_path, capsys):
    store = build(tmp_path)
    store.close()
    (tmp_path / STORE).write_bytes((tmp_path / "db.sqlite").read_bytes())
    config = ["--config", str(ROOT / "honed.toml"), "--data-dir", str(tmp_path)]
    assert cli.main([*config, "export-gold", "--format", "martian", "--out", str(tmp_path / "martian")]) == 0
    assert "2 PRs, 2 golden comments" in capsys.readouterr().out
    assert_martian_shape(json.loads((tmp_path / "martian" / "o__r.json").read_text()))
    assert cli.main([*config, "export-gold", "--split", "dev"]) == 0  # default out: [paths] exports/martian
    assert (tmp_path / "exports" / "martian" / "o__r.json").exists()


def test_important_only_exports_only_important_issues(tmp_path):
    store = build(tmp_path)
    store.save_gold(
        GoldSet(
            "o/r",
            6,
            HEAD,
            (
                replace(GOLD, id="o/r#6:t1", severity=Severity.IMPORTANT, description="important defect"),
                replace(GOLD, id="o/r#6:t2", severity=Severity.NIT, category="style", description="style nit"),
            ),
        )
    )
    item = store.get_pr(PRKey("o/r", 1))
    pr6 = replace(item, pr=replace(item.pr, number=6))
    store.upsert_pr(pr6)

    keys = store.pr_keys()
    result = gold_export.martian(
        store, store, keys, Removals(), TextPolicy(TextOptions(), ExportReport()), important_only=True
    )
    assert list(result.files) == ["o__r.json"]
    (entries,) = result.files.values()
    assert_martian_shape(entries)
    assert [e["url"] for e in entries] == ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/6"]
    assert entries[0]["comments"] == [{"comment": GOLD.description, "severity": "High", "category": "bug"}]
    assert entries[1]["comments"] == [{"comment": "important defect", "severity": "High", "category": "bug"}]
    assert (result.prs, result.issues, result.clean, result.no_gold) == (2, 2, 2, 1)
    store.close()


def test_cli_important_only_exports_only_important_issues(tmp_path, capsys):
    store = build(tmp_path)
    store.close()
    (tmp_path / STORE).write_bytes((tmp_path / "db.sqlite").read_bytes())
    config = ["--config", str(ROOT / "honed.toml"), "--data-dir", str(tmp_path)]
    assert cli.main([*config, "export-gold", "--important-only", "--out", str(tmp_path / "martian")]) == 0
    assert "1 PRs, 1 golden comments" in capsys.readouterr().out
    entries = json.loads((tmp_path / "martian" / "o__r.json").read_text())
    assert [e["url"] for e in entries] == ["https://github.com/o/r/pull/1"]
    assert entries[0]["comments"] == [{"comment": GOLD.description, "severity": "High", "category": "bug"}]


@pytest.mark.parametrize("name", ["cal_dot_com", "discourse", "grafana", "keycloak", "sentry"])
def test_our_shape_is_a_subset_of_martians_own_golden_files(name):
    """Against the downloaded golden files (`honed import-benchmark martian --fetch`), when they are here."""
    folder = SETTINGS.paths.benchmarks / "martian"
    found = [p for p in (folder / f"{name}.json", ROOT / "data-dev" / "benchmarks" / "martian" / f"{name}.json")
             if p.exists()]  # fmt: skip
    if not found:
        pytest.skip("Martian's golden files are not downloaded")
    theirs = json.loads(found[0].read_text())
    assert all({"pr_title", "url", "comments"} <= set(e) for e in theirs)
    assert all(set(c) == {"comment", "severity", "category"} for e in theirs for c in e["comments"])
    assert {c["severity"] for e in theirs for c in e["comments"]} <= set(MARTIAN_SEVERITIES)
    assert {c["category"] for e in theirs for c in e["comments"]} <= set(MARTIAN_TAGS)
