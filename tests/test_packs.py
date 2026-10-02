"""Context packs: built from a partial clone of a tiny local repo, then read back offline."""

from dataclasses import replace

import pytest

from gitrepo import GitRepo
from honed.adapters.blobs import BlobStore
from honed.adapters.git_reader import GitReader
from honed.adapters.pack_reader import PackReader
from honed.adapters.sqlite_store import SqliteStore
from honed.core.types import (
    Actor,
    AuthorKind,
    Compare,
    Corpus,
    FilePatch,
    HarvestedPR,
    PackBudget,
    PackRole,
    PullRequest,
)
from honed.learn.packs import PackBuilder, grep_scope, summarize

BUDGET = PackBudget(
    max_files=60, max_bytes=1_000_000, max_file_bytes=100_000, max_symbols=10, max_files_per_symbol=5,
    max_referencing=10, max_tests=5, grep_full_tree_max_files=1000, grep_scope_max_files=100, tree_listing=True,
)  # fmt: skip

MODEL_V1 = "def fit_model(x):\n    return x\n\n\ndef helper(y):\n    return y\n"
MODEL_V2 = "def fit_model(x):\n    return x + 1\n\n\ndef helper(y):\n    return y\n"


@pytest.fixture
def repo(tmp_path):
    return GitRepo(tmp_path / "remote")


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    yield s
    s.close()


def pr_on(repo: GitRepo) -> tuple[HarvestedPR, str, str]:
    """A PR changing `fit_model`, with a caller, a named test, an importer and unrelated files."""
    base = repo.commit({
        "pkg/model.py": MODEL_V1,
        "pkg/use.py": "from pkg.model import fit_model\n\nprint(fit_model(2))\n",
        "pkg/other.py": "def unrelated():\n    return 0\n",
        "tests/test_model.py": "from pkg import model\n\n\ndef test_it():\n    assert model.helper(1) == 1\n",
        "docs/big.txt": "x" * 5000,
        "docs/usage.md": "Call fit_model(x) to fit.\n",
        "logo.png": "\0PNG",
    })  # fmt: skip
    head = repo.commit({"pkg/model.py": MODEL_V2})
    diff = Compare(
        base=base, head=head, status="ahead", merge_base=base,
        files=(FilePatch("pkg/model.py", "modified", repo.diff(base, head, "pkg/model.py")),),
    )  # fmt: skip
    pr = PullRequest(
        repo="o/r", number=1, title="Fix fit", author=Actor("dev"), created_at="2026-06-01T00:00:00Z",
        landed_at="2026-06-02T00:00:00Z", base_ref="main", base_oid=base, head_oid=head, reviewed_diff=diff,
    )  # fmt: skip
    return HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, head), base, head


def build(repo, store, tmp_path, budget=BUDGET):
    item, base, head = pr_on(repo)
    store.upsert_pr(item)
    reader = GitReader(repo.url, tmp_path / "clone.git")
    report = PackBuilder(reader, store, budget).build_all([item.key])
    return report, reader, base, head


def test_pack_holds_changed_files_tests_and_referencing_files(repo, store, tmp_path):
    report, reader, base, head = build(repo, store, tmp_path)
    (pack,) = report.built
    assert store.get_pack(pack.key) == pack
    files = {(f.path, f.commit): f for f in pack.files}
    assert files[("pkg/model.py", head)].role is PackRole.CHANGED
    assert files[("pkg/model.py", base)].role is PackRole.CHANGED
    assert store.get_blob(files[("pkg/model.py", base)].blob).decode() == MODEL_V1
    assert files[("tests/test_model.py", head)].role is PackRole.TEST
    assert files[("tests/test_model.py", head)].reason == "test named after pkg/model.py"
    use = files[("pkg/use.py", head)]
    assert use.role is PackRole.REFERENCING and "fit_model" in use.reason
    assert ("pkg/other.py", head) not in files and ("docs/big.txt", head) not in files
    assert ("docs/usage.md", head) not in files  # mentions fit_model, but docs are not callers
    assert "fit_model" in pack.symbols and pack.grep_scope == ()  # a small tree is searched whole
    assert store.get_blob(pack.tree_blob).decode().split("\n") == sorted(
        [
            "pkg/model.py",
            "pkg/use.py",
            "pkg/other.py",
            "tests/test_model.py",
            "docs/big.txt",
            "docs/usage.md",
            "logo.png",
        ]
    )
    assert reader.disk_usage() > 0
    reader.remove()
    assert not reader.path.exists()


def test_budget_caps_are_recorded_as_skips(repo, store, tmp_path):
    report, *_ = build(repo, store, tmp_path, replace(BUDGET, max_files=2))
    (pack,) = report.built
    assert len(pack.files) == 2
    assert {f.path for f in pack.files} == {"pkg/model.py"}  # head and base versions come first
    assert any(s.path == "tests/test_model.py" and "pack full" in s.reason for s in pack.skipped)


def test_file_and_referencing_caps(repo, store, tmp_path):
    report, *_ = build(repo, store, tmp_path, replace(BUDGET, max_file_bytes=40, max_referencing=0))
    (pack,) = report.built
    assert any("max_file_bytes" in s.reason for s in pack.skipped)
    assert any(s.path == "pkg/use.py" and "max_referencing" in s.reason for s in pack.skipped)


def test_too_common_symbols_are_dropped(repo, store, tmp_path):
    report, *_ = build(repo, store, tmp_path, replace(BUDGET, max_files_per_symbol=0))
    (pack,) = report.built
    assert "fit_model" in pack.common_symbols and not any(f.role is PackRole.REFERENCING for f in pack.files)


def missing_blobs(reader: GitReader, commit: str) -> int:
    listing = reader._git(["ls-tree", "-r", commit]).stdout.split("\n")
    oids = "\n".join(line.split()[2] for line in listing if " blob " in line) + "\n"
    return reader._git(["cat-file", "--batch-check"], stdin=oids).stdout.count(" missing")


def test_git_reader_prefetches_only_what_is_asked(repo, tmp_path):
    _, _, head = pr_on(repo)
    reader = GitReader(repo.url, tmp_path / "clone.git")
    reader.ensure_commit(head)
    total = missing_blobs(reader, head)
    reader.prefetch(head, [])
    assert missing_blobs(reader, head) == total  # nothing asked, nothing fetched
    reader.prefetch(head, ["pkg"])
    assert missing_blobs(reader, head) == total - 3
    reader.grep("anything", head)  # a whole-tree grep fetches everything it searches
    assert missing_blobs(reader, head) == 0


def test_git_reader_reads_lists_and_greps(repo, tmp_path):
    _, base, head = pr_on(repo)
    reader = GitReader(repo.url, tmp_path / "clone.git")
    assert reader.read("pkg/model.py", base) == MODEL_V1
    assert reader.read("missing.py", head) is None
    assert reader.read("logo.png", head) is None  # binary
    assert reader.list_files(head, "pkg") == ["pkg/model.py", "pkg/other.py", "pkg/use.py"]
    hits = reader.grep("fit_model", head, ["pkg"], word=True, max_per_file=1)
    assert sorted((h.path, h.line) for h in hits) == [("pkg/model.py", 1), ("pkg/use.py", 1)]
    assert reader.grep("fit_mod", head, ["pkg"], word=True) == []  # whole words only


def test_git_reader_survives_non_utf8_and_bare_carriage_returns(repo, tmp_path):
    # A Latin-1 file (0x95 is a cp1252 bullet, 0xe9 an e-acute) has no NUL byte, so git treats it as text and
    # `git grep -I` still prints it: the output must be decoded with replacement, not crash. A bare CR inside a
    # matched line must not split the record either.
    legacy = b"# \x95 caf\xe9 calls fit_model\nx = fit_model(1)\rfit_model(2)\n"
    head = repo.commit({"pkg/model.py": MODEL_V1, "pkg/legacy.py": legacy})
    reader = GitReader(repo.url, tmp_path / "clone.git")
    assert reader.read("pkg/legacy.py", head) == legacy.decode("utf-8", errors="replace")
    hits = reader.grep("fit_model", head, ["pkg"], word=True)
    assert sorted((h.path, h.line) for h in hits) == [("pkg/legacy.py", 1), ("pkg/legacy.py", 2), ("pkg/model.py", 1)]
    assert hits[0].text == "# \ufffd caf\ufffd calls fit_model"
    assert hits[1].text == "x = fit_model(1)\rfit_model(2)"
    assert [h.path for h in reader.grep("caf", head)] == ["pkg/legacy.py"]  # a whole-tree grep


# ---- scope ---------------------------------------------------------------------------------------------------


def test_grep_scope_widens_toward_the_root_within_the_cap():
    tree = [f"a/b/c/f{i}.py" for i in range(5)] + [f"a/b/g{i}.py" for i in range(5)] + [f"z/{i}.py" for i in range(50)]
    budget = replace(BUDGET, grep_full_tree_max_files=10, grep_scope_max_files=12)
    assert grep_scope(tree[:10], ["a/b/c/f0.py"], replace(BUDGET, grep_full_tree_max_files=10)) == ()
    assert grep_scope(tree, ["a/b/c/f0.py"], budget) == ("a",)  # a/ has 10 files; the root would have 60
    assert grep_scope(tree, ["a/b/c/f0.py"], replace(budget, grep_scope_max_files=6)) == ("a/b/c",)


def test_grep_scope_falls_back_to_direct_children_when_a_directory_is_too_big():
    tree = [f"top{i}.py" for i in range(30)] + [f"src/{i}.py" for i in range(30)]
    budget = replace(BUDGET, grep_full_tree_max_files=10, grep_scope_max_files=100)
    scope = grep_scope(tree, ["top0.py"], budget)
    assert scope == tuple(sorted(f"top{i}.py" for i in range(30)))


# ---- offline reading -----------------------------------------------------------------------------------------


def test_pack_reader_serves_hits_and_logs_misses(repo, store, tmp_path):
    report, _, base, head = build(repo, store, tmp_path)
    (pack,) = report.built
    reader = PackReader(pack, store.get_blob)
    assert reader.read("pkg/model.py", head) == MODEL_V2
    assert reader.read("pkg/model.py", base) == MODEL_V1
    assert reader.read("pkg/use.py", base).startswith("from pkg.model")  # unchanged: base equals head
    assert reader.read("pkg/other.py", head) is None  # outside the pack
    assert reader.read("pkg/model.py", "0" * 40) is None  # another commit
    assert reader.stats.hits == 3
    assert reader.stats.misses == [("pkg/other.py", head), ("pkg/model.py", "0" * 40)]
    assert reader.stats.hit_rate == pytest.approx(0.6)
    assert {h.path for h in reader.grep("fit_model", head, word=True)} == {"pkg/model.py", "pkg/use.py"}
    assert reader.grep("helper", head, ["pkg"], word=True) == [
        h for h in reader.grep("helper", head, word=True) if h.path.startswith("pkg/")
    ]
    assert "pkg/other.py" in reader.list_files(head, "pkg")  # from the stored tree listing


def test_summary_statistics(repo, store, tmp_path):
    report, *_ = build(repo, store, tmp_path)
    summary = summarize(report.built)
    assert summary["packs"] == 1 and summary["files_median"] == len(report.built[0].files)
    assert summary["roles"]["changed"] == 2


def test_git_network_failures_are_unavailable_not_missing_files():
    from honed.adapters.git_reader import _raise_if_offline
    from honed.ports.code_reader import ReaderUnavailable

    with pytest.raises(ReaderUnavailable):
        _raise_if_offline("fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host", "fetch")
    _raise_if_offline("fatal: path 'x.py' does not exist in 'abc'", "read")  # a missing file is not an outage
