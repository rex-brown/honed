"""The export rules of the published dataset (DATASET.md): attribution and text hashes on every quoted text, the
secrets and personal-data scan with its report, the removal list, the manifest's release fields, and the stripped
variant's round trip through `rehydrate --comments` with hash checks. No network: the code host is a fake."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from evalkit import build_store
from honed import cli
from honed.adapters import removals_file
from honed.adapters.blobs import BlobStore
from honed.adapters.bundle_file import BundleFileReader, BundleFileWriter
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.codec import to_json
from honed.adapters.sqlite_store import SCHEMA_VERSION, SqliteStore
from honed.core import marks
from honed.core.redaction import RULES_VERSION, sha256
from honed.core.removals import Removals, RemovalsError
from honed.core.types import (
    AuthorKind,
    JudgedLabel,
    Judgment,
    JudgmentKind,
    Outcome,
    Polarity,
    PRKey,
    Strength,
)
from honed.learn import bundle
from honed.learn.bundle_text import TextOptions
from honed.learn.judge import PROMPTS
from honed.learn.rehydrate_text import TextRehydrator
from honed.ports.bundle import BundleError, JudgeVersion, PRRecord
from honed.ports.call_store import LedgerEntry
from honed.ports.llm import LLMCall, LLMResult, Usage
from reviewkit import ROOT, SETTINGS, STORE

JUDGE = SETTINGS.models.judge.online
SECRET_KEY = "AKIA" + "Q2J4K5L6M7N8P9R3"  # synthetic, AWS-shaped (split so secret scanners pass this file)
EMAIL = "jane.doe@acme-corp.io"
COMMENT = "items can be empty here"
BODY = "Parse the first item."
LINK = "https://github.com/o/r/pull/{n}#c-{cid}"
P1, P3, P4 = PRKey("o/r", 1), PRKey("o/r", 3), PRKey("o/r", 4)


def judge_version() -> JudgeVersion:
    return JudgeVersion(JUDGE, "medium", {name: "0" * 64 for name in PROMPTS}, f"{JUDGE}:abc")


NO_REMOVALS = Removals()


def options(*, removals: Removals = NO_REMOVALS, strip: bool = False,
            include_private: bool = False) -> bundle.ExportOptions:  # fmt: skip
    def link(url: str, cid: str) -> str:
        return LINK.format(n=url.rsplit("/", 1)[1], cid=cid)

    return bundle.ExportOptions(
        SCHEMA_VERSION, JUDGE, "honed test", SETTINGS.eval.split_fractions, SETTINGS.corpus.excluded,
        include_private=include_private, dataset_version="9.9.9", judge=judge_version(), removals=removals,
        text=TextOptions(strip=strip, link=link),
    )  # fmt: skip


def store_with(path: Path, humans: Sequence[str] = ("ann", "bob")) -> tuple[SqliteStore, SqliteCallStore]:
    """PR 1 (ann; its comment holds an AWS-shaped key and an email address), 2 (the clean PR), 3, 4, ... (one per
    further author), each human PR with one thread `t<n>`, judged, and one gold issue from it; and the judge's cached
    answers, two tied to PRs by the usage ledger and one not."""
    store = build_store(path, humans=humans)
    item = store.get_pr(P1)
    thread = item.pr.threads[0]
    leaky = replace(thread.comments[0], body=f"{COMMENT}; key {SECRET_KEY}, ask {EMAIL}")
    store.upsert_pr(replace(item, pr=replace(item.pr, threads=(replace(thread, comments=(leaky,)),))))
    calls = SqliteCallStore(path / "db.sqlite")
    for key in store.pr_keys():
        if key.number == 2:
            continue
        tid = f"t{key.number}"
        store.save_judgment(Judgment("o/r", key.number, tid, JudgmentKind.ADDRESSED, "addressed", f"fixed; {EMAIL}",
                                     None, JUDGE))  # fmt: skip
        store.save_judged_labels(key, [JudgedLabel(tid, AuthorKind.HUMAN, Outcome.FIXED, Polarity.POSITIVE,
                                                   Strength.STRONG)])  # fmt: skip
    for pr, user in (("o/r#1", "gold 1"), ("o/r#3", "gold 3"), (None, "untied")):
        call = LLMCall(model=JUDGE, system="s", user=user, stage="gold", pr=pr)
        calls.put(call.key, call, LLMResult(f"answer for {user}", {"issues": [{"description": f"see {EMAIL}"}]},
                                            Usage(10, 5), JUDGE))  # fmt: skip
        calls.record(LedgerEntry("2026-10-01T00:00:00Z", "run", "gold", pr, JUDGE, call.key.digest, False, True))
    return store, calls


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]


def export(path: Path, store: SqliteStore, calls: SqliteCallStore, **kwargs) -> bundle.ExportResult:
    return bundle.export(store, store, store, store, calls, BundleFileWriter(path), options(**kwargs))


def empty_store(path: Path) -> tuple[SqliteStore, SqliteCallStore]:
    return SqliteStore(path / "db.sqlite", BlobStore(path / "blobs")), SqliteCallStore(path / "db.sqlite")


def close(*stores) -> None:
    for s in stores:
        s.close()


# ---- attribution, redaction, manifest -------------------------------------------------------------------------


def test_every_quoted_text_carries_attribution_and_a_hash_of_what_was_exported(tmp_path):
    store, calls = store_with(tmp_path / "src")
    result = export(tmp_path / "b.jsonl.gz", store, calls)
    lines = records(tmp_path / "b.jsonl.gz")
    raw = gzip.decompress((tmp_path / "b.jsonl.gz").read_bytes()).decode()
    assert SECRET_KEY not in raw and EMAIL not in raw  # comment, judgment reason and cached answers all redacted
    pr1 = next(r for r in lines if r["kind"] == "pr" and r["item"]["pr"]["number"] == 1)
    exported = pr1["item"]["pr"]["threads"][0]["comments"][0]["body"]
    assert exported == f"{COMMENT}; key [redacted:aws_access_key], ask [redacted:email]"
    (attribution,) = pr1["comments"]
    assert attribution == {"id": "t1-0", "author": "rev", "url": LINK.format(n=1, cid="t1-0"),
                           "text_sha256": sha256(exported), "redacted": ["aws_access_key", "email"],
                           "stripped": False}  # fmt: skip
    assert pr1["description"] == {"id": "", "author": "ann", "url": "https://github.com/o/r/pull/1",
                                  "text_sha256": sha256(BODY), "redacted": [], "stripped": False}  # fmt: skip
    assert all(len(r["comments"]) == sum(len(t["comments"]) for t in r["item"]["pr"]["threads"])
               for r in lines if r["kind"] == "pr")  # fmt: skip

    m = result.manifest
    assert (m.bundle_schema, m.dataset_version, m.comment_text, m.redaction_rules) == (2, "9.9.9", "full",
                                                                                       RULES_VERSION)  # fmt: skip
    assert m.judge == judge_version() and m.licenses.annotations == "CC-BY-4.0"
    assert "not licensed by this project" in m.licenses.comment_text and "Apache-2.0" in m.licenses.software
    assert m.redactions == {"aws_access_key": 1, "email": 6}  # the comment, 2 judgment reasons, 3 cached answers
    assert m.removals == {"listed_prs": 0, "listed_comments": 0, "prs": 0, "comments": 0, "threads": 0,
                          "gold_issues": 0}  # fmt: skip
    manifest = lines[0]
    assert manifest["kind"] == "manifest" and manifest["judge"]["prompts"] == {n: "0" * 64 for n in PROMPTS}
    assert manifest["licenses"]["annotations"] == "CC-BY-4.0" and manifest["redactions"]["email"] == 6

    where = {(h.location, h.kind) for h in result.report.redactions}
    assert ("o/r#1 comment t1-0", "aws_access_key") in where and ("o/r#1 comment t1-0", "email") in where
    assert ("o/r#1 judgment t1 addressed reason", "email") in where
    assert any(loc.startswith("cached answer ") and " data issues[0].description" in loc for loc, _ in where)
    assert SECRET_KEY not in json.dumps([vars(h) for h in result.report.redactions])  # locations, never values

    dest, dest_calls = empty_store(tmp_path / "dst")
    report = bundle.import_bundle(BundleFileReader(tmp_path / "b.jsonl.gz"), dest, dest, dest, dest, dest_calls)
    assert report.mismatched == [] and report.manifest.dataset_version == "9.9.9"
    assert all(marks.is_code(m) for k in dest.stripped_keys() for m in dest.stripped(k))  # full text: code only
    close(store, calls, dest, dest_calls)


def test_import_reports_text_that_does_not_match_its_hash(tmp_path):
    store, calls = store_with(tmp_path)
    export(tmp_path / "b.jsonl.gz", store, calls)
    record = next(r for r in BundleFileReader(tmp_path / "b.jsonl.gz").records() if isinstance(r, PRRecord)
                  and r.item.key == P1)  # fmt: skip
    assert bundle.mismatched_texts(record) == []
    thread = record.item.pr.threads[0]
    edited = replace(thread, comments=(replace(thread.comments[0], body="something else"),))
    damaged = replace(record, item=replace(record.item, pr=replace(record.item.pr, threads=(edited,), body="x")))
    assert bundle.mismatched_texts(damaged) == ["o/r#1 comment t1-0", "o/r#1 body"]
    close(store, calls)


# ---- removals -------------------------------------------------------------------------------------------------


def test_removals_take_out_listed_prs_and_the_threads_of_listed_comments(tmp_path):
    store, calls = store_with(tmp_path, humans=("ann", "bob", "cy"))  # PRs 1, 3, 4 (and the clean 2)
    plain = {(r["pr"]["repo"], r["pr"]["number"]): r["split"] for r in records(
        _exported(tmp_path / "plain.jsonl.gz", store, calls)) if r["kind"] == "split"}  # fmt: skip
    removals = Removals(prs=frozenset({P4}), comments=frozenset({"t3-0"}))
    result = export(tmp_path / "b.jsonl.gz", store, calls, removals=removals)
    assert result.manifest.removals == {"listed_prs": 1, "listed_comments": 1, "prs": 1, "comments": 1,
                                        "threads": 1, "gold_issues": 1}  # fmt: skip
    assert (result.report.removed_prs, result.report.removed_comments, result.report.removed_threads) == (
        ["o/r#4"],
        ["t3-0"],
        ["t3"],
    )
    lines = records(tmp_path / "b.jsonl.gz")
    raw = gzip.decompress((tmp_path / "b.jsonl.gz").read_bytes()).decode()
    assert '"t3-0"' not in raw and '"t3"' not in raw  # no trace of the removed thread
    numbers = {_pr_number(r) for r in lines[1:]} - {None}
    assert 4 not in numbers and {1, 2, 3} <= numbers  # PR 4 is nowhere: record, labels, gold, split
    pr3 = next(r for r in lines if r["kind"] == "pr" and r["item"]["pr"]["number"] == 3)
    assert pr3["item"]["pr"]["threads"] == [] and pr3["item"]["labels"] == [] and pr3["comments"] == []
    assert next(r for r in lines if r["kind"] == "gold" and r["gold"]["number"] == 3)["gold"]["issues"] == []
    assert not [r for r in lines if r["kind"] == "judgment" and r["judgment"]["number"] == 3]
    answers = sorted(r["entry"]["text"] for r in lines if r["kind"] == "cached_answer")
    assert answers == ["answer for gold 1", "answer for untied"]  # the answer the ledger ties to PR 3 is gone
    splits_now = {(r["pr"]["repo"], r["pr"]["number"]): r["split"] for r in lines if r["kind"] == "split"}
    assert splits_now == {k: v for k, v in plain.items() if k != ("o/r", 4)}  # no other PR changed split
    close(store, calls)


def test_the_test_private_split_stays_out_of_an_export_unless_included(tmp_path):
    store, calls = store_with(tmp_path, humans=("ann", "bob", "cy"))  # PRs 1, 3, 4 (and the clean 2)
    store.set_splits({P1: "train", PRKey("o/r", 2): "train", P3: "test-private", P4: "test-public"})
    result = export(tmp_path / "b.jsonl.gz", store, calls)
    m, report = result.manifest, result.report
    assert (m.test_private, m.test_private_prs, report.private_prs, report.private_answers) == ("excluded", 1, 1, 1)
    lines = records(tmp_path / "b.jsonl.gz")
    raw = gzip.decompress((tmp_path / "b.jsonl.gz").read_bytes()).decode()
    assert {_pr_number(r) for r in lines[1:]} - {None} == {1, 2, 4} and '"t3"' not in raw  # PR 3 is nowhere
    answers = sorted(r["entry"]["text"] for r in lines if r["kind"] == "cached_answer")
    assert answers == ["answer for gold 1", "answer for untied"]  # the answer the ledger ties to PR 3 is out
    assert "o/r#3" not in json.dumps(to_json(report))  # counted, never named
    splits_now = {r["pr"]["number"]: r["split"] for r in lines if r["kind"] == "split"}
    assert splits_now == {1: "train", 2: "train", 4: "test-public"}
    assert m.removals["prs"] == 0 and m.repos[0].prs == 3

    full = export(tmp_path / "full.jsonl.gz", store, calls, include_private=True)
    assert (full.manifest.test_private, full.manifest.test_private_prs, full.report.private_prs) == ("included", 1, 0)
    every = records(tmp_path / "full.jsonl.gz")
    assert {_pr_number(r) for r in every[1:]} - {None} == {1, 2, 3, 4}
    assert {r["pr"]["number"]: r["split"] for r in every if r["kind"] == "split"}[3] == "test-private"
    close(store, calls)


def test_the_export_refuses_a_record_that_still_carries_a_removed_item(tmp_path):
    store, calls = store_with(tmp_path)
    item = store.get_pr(P1)
    guard = bundle._Refusing(BundleFileWriter(tmp_path / "x.jsonl.gz"), Removals(comments=frozenset({"t1-0"})))
    with pytest.raises(BundleError, match="holds removed comments"):
        guard.write(PRRecord(item))
    guard = bundle._Refusing(BundleFileWriter(tmp_path / "y.jsonl.gz"), Removals(prs=frozenset({P1})))
    with pytest.raises(BundleError, match="on the removal list"):
        guard.write(bundle.JudgmentRecord(store.judgments(P1)[0]))
    close(store, calls)


def test_the_removal_list_file(tmp_path):
    committed = removals_file.load(ROOT / "yardstick" / "removals.json")
    assert len(committed) == 0  # committed empty; entries come only from removal requests
    assert len(removals_file.load(tmp_path / "missing.json")) == 0
    path = tmp_path / "removals.json"
    path.write_text(json.dumps({"prs": [{"pr": "Owner/Name#12", "added": "2026-10-01"}, "o/r#3"],
                                "comments": [{"id": "PRRC_x"}]}))  # fmt: skip
    loaded = removals_file.load(path)
    assert loaded.removes_pr(PRKey("owner/name", 12)) and loaded.removes_pr(PRKey("O/R", 3))
    assert loaded.comments == {"PRRC_x"}
    for bad in ({"prs": [{"pr": "no-number"}]}, {"comments": [{}]}, {"prs": "o/r#1"}, []):
        path.write_text(json.dumps(bad))
        with pytest.raises(RemovalsError):
            removals_file.load(path)


# ---- the stripped variant -------------------------------------------------------------------------------------


class FakeText:
    """The code host's current text: comment bodies by id, PR bodies by number (absent: deleted)."""

    def __init__(self, comments: Mapping[str, str], bodies: Mapping[int, str]) -> None:
        self.comments, self.bodies, self.asked = comments, bodies, []

    def comment_bodies(self, ids: Sequence[str]) -> Mapping[str, str | None]:
        self.asked.append(sorted(ids))
        return {i: self.comments.get(i) for i in ids}

    def pr_bodies(self, repo: str, numbers: Sequence[int]) -> Mapping[int, str | None]:
        return {n: self.bodies.get(n) for n in numbers}


def test_a_stripped_bundle_round_trips_through_rehydrate_comments_with_hash_checks(tmp_path):
    store, calls = store_with(tmp_path / "src")
    original = {k: store.get_pr(k) for k in store.pr_keys()}
    result = export(tmp_path / "b.jsonl.gz", store, calls, strip=True)
    raw = gzip.decompress((tmp_path / "b.jsonl.gz").read_bytes()).decode()
    assert result.manifest.comment_text == "stripped" and COMMENT not in raw and BODY not in raw
    assert result.report.stripped_texts == 5  # three comments, three bodies; less the clean PR's missing comment

    dest, dest_calls = empty_store(tmp_path / "dst")
    bundle.import_bundle(BundleFileReader(tmp_path / "b.jsonl.gz"), dest, dest, dest, dest, dest_calls)
    pr1_marks = marks.text_marks(dest.stripped(P1))
    exported_text = f"{COMMENT}; key [redacted:aws_access_key], ask [redacted:email]"
    expected = [marks.body_mark(sha256(BODY)), marks.comment_mark("t1-0", sha256(exported_text))]
    assert sorted(pr1_marks) == sorted(expected)
    assert dest.get_pr(P1).pr.threads[0].comments[0].body == "" and dest.get_pr(P1).pr.body == ""
    assert "rehydrate --comments" in marks.waiting_for(dest.stripped(P1))

    # Re-exported before anyone refetched it, the text stays stripped under the same hash.
    again = export(tmp_path / "again.jsonl.gz", dest, dest_calls)
    again_records = records(tmp_path / "again.jsonl.gz")
    pr1 = next(r for r in again_records if r["kind"] == "pr" and r["item"]["pr"]["number"] == 1)
    assert pr1["comments"][0]["stripped"] and pr1["comments"][0]["text_sha256"] == sha256(exported_text)
    assert again.manifest.comment_text == "full" and again.report.stripped_texts == 5

    # GitHub has PR 1's text as it was (the secret included), an edited comment on PR 3, and PR 2's body is gone.
    host = FakeText({"t1-0": original[P1].pr.threads[0].comments[0].body, "t3-0": COMMENT + " (edited)"},
                    {1: BODY, 3: BODY})  # fmt: skip
    keys = sorted(dest.pr_keys(), key=lambda k: k.number)
    report = TextRehydrator(dest, host).run(keys)
    assert (report.prs, report.restored, report.changed, report.missing, report.done) == (
        3,
        3,
        ["o/r#3 comment t3-0"],
        ["o/r#2 body"],
        ["o/r#1"],
    )
    restored = dest.get_pr(P1).pr
    assert restored.threads[0].comments[0].body == exported_text and restored.body == BODY  # redacted again
    assert marks.text_marks(dest.stripped(P1)) == [] and marks.code_marks(dest.stripped(P1))  # code still to come
    assert marks.text_marks(dest.stripped(P3)) == [marks.comment_mark("t3-0", sha256(COMMENT))]
    assert dest.get_pr(P3).pr.threads[0].comments[0].body == ""  # changed text is not stored by default

    accepted = TextRehydrator(dest, host, accept_changed=True).run(keys)
    assert accepted.accepted == 2 and sorted(accepted.done) == ["o/r#2", "o/r#3"]
    assert dest.get_pr(P3).pr.threads[0].comments[0].body == COMMENT + " (edited)"
    assert all(not marks.text_marks(dest.stripped(k)) for k in keys)
    close(store, calls, dest, dest_calls)


# ---- the command line -----------------------------------------------------------------------------------------


def test_the_cli_exports_with_a_report_and_the_yardstick_judge(tmp_path, capsys):
    (tmp_path / "src").mkdir()
    store, calls = store_with(tmp_path / "src")
    close(store, calls)
    (tmp_path / "src" / STORE).write_bytes((tmp_path / "src" / "db.sqlite").read_bytes())
    config = ["--config", str(ROOT / "honed.toml"), "--data-dir", str(tmp_path / "src")]
    out = tmp_path / "honed-dataset.jsonl.gz"
    assert cli.main([*config, "bundle", "export", str(out), "--no-licenses", "--strip-comments"]) == 0
    printed = capsys.readouterr().out
    assert "redactions (rules" in printed and "comment text: stripped" in printed
    assert "removals: listed_prs 0" in printed
    report = json.loads((tmp_path / "honed-dataset.report.json").read_text())
    assert report["redaction_counts"]["aws_access_key"] == 1 and report["comment_text"] == "stripped"
    assert SECRET_KEY not in json.dumps(report)
    manifest = BundleFileReader(out).manifest()
    prompts = {n: hashlib.sha256((ROOT / "yardstick" / "prompts" / f"{n}.md").read_bytes()).hexdigest()
               for n in PROMPTS}  # fmt: skip
    assert manifest.judge.prompts == prompts and manifest.judge.model == JUDGE
    assert manifest.judge.fingerprint.startswith(f"{JUDGE}:") and manifest.dataset_version == SETTINGS.dataset.version
    attribution = next(r for r in BundleFileReader(out).records() if isinstance(r, PRRecord)).comments[0]
    assert attribution.url.endswith("/files")  # the fixture's comment ids aren't GitHub node ids


def _exported(path: Path, store: SqliteStore, calls: SqliteCallStore) -> Path:
    export(path, store, calls)
    return path


def _pr_number(record: dict) -> int | None:
    for path in (("item", "pr", "number"), ("judgment", "number"), ("gold", "number"), ("pr", "number"),
                 ("defect", "pr", "number")):  # fmt: skip
        value = record
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, int):
            return value
    return None
