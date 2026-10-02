"""The portable dataset: `bundle export` holds no code, `bundle import` into an empty store reproduces the counts and
is idempotent, a newer bundle schema is refused, and `rehydrate` rebuilds patches and context packs from git. No
network: the git remote is a local repository."""

from __future__ import annotations

import gzip
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from evalkit import build_store
from gitrepo import GitRepo
from honed import cli
from honed.adapters.blobs import BlobStore
from honed.adapters.bundle_file import BundleFileReader, BundleFileWriter
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.git_reader import GitReader, parse_diff
from honed.adapters.sqlite_store import SCHEMA_VERSION, SqliteStore
from honed.core.evals import Decision
from honed.core.improve import PolicyRecord
from honed.core.types import (
    Actor,
    AuthorKind,
    Compare,
    Corpus,
    FilePatch,
    HarvestedPR,
    Judgment,
    JudgmentKind,
    PRKey,
    PullRequest,
)
from honed.learn import bundle
from honed.learn.rehydrate import Rehydrator
from honed.ports.bundle import BundleError
from honed.ports.llm import LLMCall, LLMResult, Usage
from reviewkit import ROOT, SETTINGS, STORE

JUDGE = SETTINGS.models.judge.online


def options() -> bundle.ExportOptions:
    return bundle.ExportOptions(SCHEMA_VERSION, JUDGE, "honed test", SETTINGS.eval.split_fractions,
                                SETTINGS.corpus.excluded, {"aacr": ("Apache-2.0", "https://x")})  # fmt: skip


def populated(path: Path) -> tuple[SqliteStore, SqliteCallStore]:
    store = build_store(path, humans=("ann", "bob"))
    calls = SqliteCallStore(path / "db.sqlite")
    store.save_judgment(Judgment("o/r", 1, "t1", JudgmentKind.ADDRESSED, "addressed", "fixed it", None, JUDGE))
    store.add_decision(Decision(None, "2026-10-01T00:00:00Z", "h", "c", "S=0.5", "S=0.6", "+0.1", "{}", "promoted"))
    store.save_policy_version(PolicyRecord("q" * 64, "p" * 64, "2026-10-01", "why", "diff", {}, False,
                                           {"config.toml": "x = 1\n"}))  # fmt: skip
    for model, user in ((JUDGE, "judge question"), ("claude-sonnet-5-5", "a reviewer's question")):
        call = LLMCall(model=model, system="s", user=user, stage="gold")
        calls.put(call.key, call, LLMResult('{"ok": true}', {"ok": True}, Usage(10, 5, cost_usd=0.01), model))
    return store, calls


def export_to(path: Path, store: SqliteStore, calls: SqliteCallStore):
    return bundle.export(store, store, store, store, calls, BundleFileWriter(path), options(),
                         license_of=lambda repo: "MIT").manifest  # fmt: skip


def counts(store: SqliteStore, calls: SqliteCallStore) -> dict[str, int]:
    return bundle.store_counts(store, store, store, store, calls, JUDGE)


def test_export_holds_no_code_and_an_import_into_an_empty_store_matches_it(tmp_path):
    store, calls = populated(tmp_path / "src")
    manifest = export_to(tmp_path / "b.jsonl.gz", store, calls)
    assert manifest.counts["pr"] == 3 and manifest.counts["cached_answer"] == 1  # only the judge's answers
    assert manifest.repos[0].repo == "o/r" and manifest.repos[0].license == "MIT" and manifest.bundle_schema == 2
    lines = gzip.decompress((tmp_path / "b.jsonl.gz").read_bytes()).decode().splitlines()
    assert json.loads(lines[0])["kind"] == "manifest" and len(lines) == 1 + sum(manifest.counts.values())
    prs = [json.loads(line) for line in lines if json.loads(line)["kind"] == "pr"]
    assert all(f["patch"] is None for r in prs for f in r["item"]["pr"]["reviewed_diff"]["files"])
    assert prs[0]["patched"] == ["reviewed:app/service.py"]
    assert "def parse" not in "\n".join(lines)  # the service's code is nowhere in the bundle

    dest = SqliteStore(tmp_path / "dst" / "db.sqlite", BlobStore(tmp_path / "dst" / "blobs"))
    dest_calls = SqliteCallStore(tmp_path / "dst" / "db.sqlite")
    report = bundle.import_bundle(BundleFileReader(tmp_path / "b.jsonl.gz"), dest, dest, dest, dest, dest_calls)
    assert report.added["pr"] == 3 and not report.kept
    expected = counts(store, calls)
    assert counts(dest, dest_calls) == expected
    assert len(dest.stripped_keys()) == 3 and dest.get_pack(PRKey("o/r", 1)) is None  # code comes from rehydrate
    made = {(f.repo, f.number): f.split for f in dest.pr_facts()}
    assert made == {("o/r", 1): "train", ("o/r", 2): "train", ("o/r", 3): "validation"}  # time-ordered, now fixed

    again = bundle.import_bundle(BundleFileReader(tmp_path / "b.jsonl.gz"), dest, dest, dest, dest, dest_calls)
    assert again.kept["pr"] == 3 and again.kept["decision"] == 1 and again.kept["cached_answer"] == 1
    assert counts(dest, dest_calls) == expected  # idempotent
    for s in (store, dest):
        s.close()
    calls.close()
    dest_calls.close()


def test_importing_into_the_source_store_keeps_its_code(tmp_path):
    store, calls = populated(tmp_path)
    export_to(tmp_path / "b.jsonl.gz", store, calls)
    report = bundle.import_bundle(BundleFileReader(tmp_path / "b.jsonl.gz"), store, store, store, store, calls)
    assert report.kept["pr"] == 3 and store.stripped_keys() == []
    assert store.get_pr(PRKey("o/r", 1)).pr.reviewed_diff.files[0].patch is not None
    store.close()
    calls.close()


def test_a_newer_bundle_schema_is_refused(tmp_path):
    path = tmp_path / "future.jsonl.gz"
    path.write_bytes(gzip.compress(json.dumps({"kind": "manifest", "bundle_schema": 99}).encode() + b"\n"))
    with pytest.raises(BundleError, match="newer than this code reads"):
        BundleFileReader(path)
    (tmp_path / "plain.jsonl").write_text("{}")
    with pytest.raises(BundleError, match="not a gzip"):
        BundleFileReader(tmp_path / "plain.jsonl")
    with pytest.raises(BundleError, match=r"\.jsonl\.gz"):
        BundleFileWriter(tmp_path / "x.json")


@pytest.mark.skipif(sys.version_info < (3, 14), reason="compression.zstd is in Python 3.14+")
def test_a_zstd_bundle_round_trips(tmp_path):
    store, calls = populated(tmp_path)
    manifest = export_to(tmp_path / "b.jsonl.zst", store, calls)
    reader = BundleFileReader(tmp_path / "b.jsonl.zst")
    assert reader.manifest().counts == manifest.counts and sum(1 for _ in reader.records()) == sum(
        manifest.counts.values())  # fmt: skip
    store.close()
    calls.close()


def test_the_cli_exports_and_imports(tmp_path, capsys):
    store, calls = populated(tmp_path / "src")
    store.close()
    calls.close()
    for name in ("src", "dst"):
        (tmp_path / name).mkdir(exist_ok=True)
    (tmp_path / "src" / STORE).write_bytes((tmp_path / "src" / "db.sqlite").read_bytes())
    config = ["--config", str(ROOT / "honed.toml")]
    assert cli.main([*config, "--data-dir", str(tmp_path / "src"), "bundle", "export", str(tmp_path / "b.jsonl.gz"),
                     "--no-licenses"]) == 0  # fmt: skip
    assert "pr 3" in capsys.readouterr().out
    bundle_file = str(tmp_path / "b.jsonl.gz")
    assert cli.main([*config, "--data-dir", str(tmp_path / "dst"), "bundle", "import", bundle_file]) == 0
    out = capsys.readouterr().out
    assert "pr 3" in out and "3 PRs wait for their code" in out


def test_parse_diff_splits_files_and_marks_binaries():
    text = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n--- a/old.py\n"
        "+++ b/new.py\n@@ -2 +2 @@\n-a\n+b\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n"
        "diff --git a/img.png b/img.png\nindex 1..2 100644\nBinary files a/img.png and b/img.png differ\n"
        "diff --git a/same.py b/moved.py\nsimilarity index 100%\nrename from same.py\nrename to moved.py\n"
    )
    assert parse_diff(text) == {"a.py": "@@ -1 +1 @@\n-x\n+y", "new.py": "@@ -2 +2 @@\n-a\n+b",
                                "gone.py": "@@ -1 +0,0 @@\n-z", "img.png": None, "moved.py": ""}  # fmt: skip


# ---- rehydrate -------------------------------------------------------------------------------------------------


def test_rehydrate_rebuilds_patches_and_packs_from_git(tmp_path):
    remote = GitRepo(tmp_path / "remote")
    use = "from app.core import area\nprint(area(2, 3))\n"
    base = remote.commit({"app/core.py": "def area(w, h):\n    return w * h\n", "app/use.py": use,
                          "tests/test_core.py": "from app.core import area\n"})  # fmt: skip
    head = remote.commit({"app/core.py": "def area(w, h):\n    return abs(w) * h\n"})
    later = remote.commit({"app/core.py": "def area(w, h):\n    return abs(w) * abs(h)\n"})
    reader = GitReader(remote.url, tmp_path / "clone.git")
    patch = reader.diff(base, head, ["app/core.py"])["app/core.py"]
    diff = Compare(base, head, "ahead", (FilePatch("app/core.py", "modified", patch),), merge_base=base)
    pr = PullRequest(repo="o/r", number=5, title="t", author=Actor("dev"), created_at="2026-03-01T00:00:00Z",
                     landed_at=None, base_ref="main", base_oid=base, head_oid=later, reviewed_diff=diff)  # fmt: skip
    src = SqliteStore(tmp_path / "src" / "db.sqlite", BlobStore(tmp_path / "src" / "blobs"))
    src.upsert_pr(HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, head))
    round_patch = reader.diff(base, later, ["app/core.py"])["app/core.py"]
    round_diff = Compare(base, later, "ahead", (FilePatch("app/core.py", "modified", round_patch),), merge_base=base)
    src.save_round_diff(PRKey("o/r", 5), round_diff)
    calls = SqliteCallStore(tmp_path / "src" / "db.sqlite")
    export_to(tmp_path / "b.jsonl.gz", src, calls)

    dst = SqliteStore(tmp_path / "dst" / "db.sqlite", BlobStore(tmp_path / "dst" / "blobs"))
    dst_calls = SqliteCallStore(tmp_path / "dst" / "db.sqlite")
    bundle.import_bundle(BundleFileReader(tmp_path / "b.jsonl.gz"), dst, dst, dst, dst, dst_calls)
    key = PRKey("o/r", 5)
    assert dst.get_pr(key).pr.reviewed_diff.files[0].patch is None
    assert dst.stripped(key) == ["reviewed:app/core.py", f"round:{later}:app/core.py"]

    (report,) = Rehydrator(dst, lambda repo: GitReader(remote.url, tmp_path / "clone2.git"), SETTINGS.harvest.pack
                           ).run([key])  # fmt: skip
    assert (report.patched, report.patches, report.packs, report.round_packs, report.failed) == (1, 1, 1, 1, {})
    assert dst.get_pr(key) == replace(src.get_pr(key))  # the patch came back as it was
    pack = dst.get_pack(key)
    assert {f.path for f in pack.files} >= {"app/core.py", "app/use.py", "tests/test_core.py"}
    round_pack = dst.get_round_pack(key, later)
    assert round_pack is not None and round_pack.diff.files[0].patch == round_patch
    assert dst.stripped_keys() == []
    for s in (src, dst):
        s.close()
    calls.close()
    dst_calls.close()
