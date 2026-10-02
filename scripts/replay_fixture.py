"""The fixture-PR replay (CI): a tiny PR with a planted bug, its context pack and the model's answers, replayed through
`honed review` on the `replay` backend; fails unless the known bug is reported.

    uv run python scripts/replay_fixture.py            # replay; exit 0 when the bug is reported, 1 when not
    uv run python scripts/replay_fixture.py --record   # re-record the answers through the fake `claude`

The fixture (`tests/fixtures/replay/`): `pr.json` (the stored PR, synthetic: `honed-fixtures/inventory#1`),
`pack.json` (its context pack, file texts inline), `calls.jsonl` (the call cache: one answer per review stage),
`policy/` (the policy the answers were recorded with) and `expected.json` (the planted bug). The replay uses the
fixture's own policy, so a change to `policy/` never breaks it; a change under `src/` that alters a review prompt
misses the cache and fails it, by design. When such a change is intended, re-record: `--record` copies the current
`policy/` into the fixture and asks the fake `claude` (`tests/fake_claude.py`) for scripted answers to the prompts
the code builds now. No network and no model calls either way.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "honed.toml"
FIXTURE = ROOT / "tests" / "fixtures" / "replay"
REF = "honed-fixtures/inventory#1"
BASE, HEAD = "5b1e7c0d2a9f4e6b8c3d1a0f9e8d7c6b5a4f3e2d", "c4f2a8e1b7d3960f5e2c8a4b1d7f3e9a6c2b8d40"

BASE_TEXT = '''"""Pagination helpers for the inventory listing."""


def page_count(total: int, size: int) -> int:
    """How many pages `total` items fill, `size` per page."""
    return (total + size - 1) // size
'''
HEAD_TEXT = (
    BASE_TEXT
    + '''

def page(items: list[str], number: int, size: int) -> list[str]:
    """The items on page `number` (pages are numbered from 1), `size` per page."""
    start = number * size
    return items[start : start + size]
'''
)
TEST_TEXT = """from inventory.pages import page_count


def test_page_count_rounds_up():
    assert page_count(5, 2) == 3
"""
BUG = {"path": "inventory/pages.py", "start_line": 11, "end_line": 11, "severity": "important",
       "category": "correctness"}  # fmt: skip


def _src() -> None:
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))


def _store(data: Path) -> Path:
    """The store the CLI opens under `--data-dir data`: the file name of `[paths] sqlite`."""
    _src()
    from honed import config

    return data / config.load(CONFIG).paths.sqlite.name


def _config(directory: Path, *, backend: str, binary: str | None = None) -> Path:
    """A copy of the project's honed.toml with another LLM backend (and `claude` binary)."""
    text = CONFIG.read_text().replace('backend = "claude_code"', f'backend = "{backend}"', 1)
    if binary is not None:
        text = text.replace('binary = "claude"', f'binary = "{binary}"', 1)
    path = directory / CONFIG.name
    path.write_text(text)
    return path


def load(fixture: Path, data: Path) -> None:
    """The fixture's PR, context pack and cached answers into a fresh store under `data`."""
    _src()
    from honed.adapters.blobs import BlobStore
    from honed.adapters.call_store import SqliteCallStore
    from honed.adapters.codec import from_json
    from honed.adapters.sqlite_store import SqliteStore
    from honed.core.types import ContextPack, HarvestedPR, PackFile, PackRole
    from honed.ports.call_store import CachedCall

    store = SqliteStore(_store(data), BlobStore(data / "blobs"))
    store.upsert_pr(from_json(HarvestedPR, json.loads((fixture / "pr.json").read_text())))
    raw = json.loads((fixture / "pack.json").read_text())
    files = tuple(PackFile(f["path"], f["commit"], PackRole(f["role"]), f["reason"], store.put_blob(f["text"].encode()),
                           len(f["text"].encode())) for f in raw["files"])  # fmt: skip
    store.save_pack(ContextPack(repo=raw["repo"], number=raw["number"], base_commit=raw["base_commit"],
                                head_commit=raw["head_commit"], files=files,
                                changed_paths=tuple(raw["changed_paths"])))  # fmt: skip
    store.close()
    calls = SqliteCallStore(_store(data))
    if (fixture / "calls.jsonl").exists():
        for line in (fixture / "calls.jsonl").read_text().splitlines():
            calls.add_cached(from_json(CachedCall, json.loads(line)))
    calls.close()


def _review(config: Path, data: Path, policy: Path, *extra: str) -> tuple[int, str, str]:
    _src()
    from honed import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--config", str(config), "--data-dir", str(data), "review", REF, "--policy", str(policy),
                         "--json", *extra])  # fmt: skip
    return code, out.getvalue(), err.getvalue()


def reported(review: dict, bug: dict) -> bool:
    """A posted finding on the bug's file, overlapping its lines (2 lines of slack), at the bug's severity."""
    return any(f["path"] == bug["path"] and f["severity"] == bug["severity"]
               and f["start_line"] <= bug["end_line"] + 2 and bug["start_line"] - 2 <= f["end_line"]
               for f in review.get("posted", []))  # fmt: skip


def replay(fixture: Path = FIXTURE) -> tuple[bool, str]:
    """Replay the fixture PR on the replay backend; (passed, what happened)."""
    bug = json.loads((fixture / "expected.json").read_text())
    with tempfile.TemporaryDirectory(prefix="honed-replay-") as tmp:
        work = Path(tmp)
        load(fixture, work / "data")
        try:
            code, out, err = _review(_config(work, backend="replay"), work / "data", fixture / "policy")
        except Exception as error:  # a replay miss: a review prompt is not the one recorded
            hint = "if that is intended, re-record: uv run python scripts/replay_fixture.py --record"
            return False, f"{type(error).__name__}: {error}\nA review prompt changed since recording; {hint}"
    if code != 0:
        return False, f"honed review exited {code}:\n{err[-2000:]}"
    review = json.loads(out)
    posted = [f"{f['path']}:{f['start_line']} [{f['severity']}] {f['title']}" for f in review.get("posted", [])]
    if not reported(review, bug):
        return False, f"the planted bug ({bug['path']}:{bug['start_line']}) was not reported; posted: {posted}"
    return True, f"the planted bug was reported: {posted}"


def record(fixture: Path = FIXTURE) -> None:
    """Rebuild the fixture: the PR and pack from the texts above, the current policy, and answers to the prompts the
    code builds now, scripted through the fake `claude`."""
    _src()
    sys.path.insert(0, str(ROOT / "tests"))
    import claude_events as ev
    from honed import config
    from honed.adapters.call_store import SqliteCallStore
    from honed.adapters.codec import to_json
    from honed.core import patches
    from honed.core.types import (
        Actor,
        AuthorKind,
        CommitInfo,
        Compare,
        Corpus,
        FilePatch,
        HarvestedPR,
        PullRequest,
        Review,
    )

    path = BUG["path"]
    patch = patches.unified_diff(BASE_TEXT, HEAD_TEXT, path)
    diff = Compare(BASE, HEAD, "ahead", (FilePatch(path, "modified", patch),), merge_base=BASE)
    pr = PullRequest(
        repo="honed-fixtures/inventory", number=1, title="Add page() to slice the inventory listing",
        author=Actor("dev"), created_at="2026-01-15T10:00:00Z", landed_at="2026-01-16T09:00:00Z", base_ref="main",
        base_oid=BASE, head_oid=HEAD, url="https://example.invalid/honed-fixtures/inventory/pull/1",
        body="Adds a helper that returns one page of the inventory listing. Pages are numbered from 1.",
        additions=6, deletions=0, changed_files=1, reviewed_diff=diff, commit_count=1,
        commits=(CommitInfo(HEAD, "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z", "Add page()"),),
        reviews=(Review("r1", Actor("rev"), "APPROVED", "2026-01-16T08:00:00Z", HEAD),),
    )  # fmt: skip
    item = HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, HEAD)
    if fixture.exists():
        shutil.rmtree(fixture)
    fixture.mkdir(parents=True)
    (fixture / "pr.json").write_text(json.dumps(to_json(item), indent=2, sort_keys=True) + "\n")
    pack = {"repo": pr.repo, "number": 1, "base_commit": BASE, "head_commit": HEAD, "changed_paths": [path],
            "files": [{"path": path, "commit": HEAD, "role": "changed", "reason": "changed by the PR, at head",
                       "text": HEAD_TEXT},
                      {"path": path, "commit": BASE, "role": "changed", "reason": "changed by the PR, before it",
                       "text": BASE_TEXT},
                      {"path": "tests/test_pages.py", "commit": HEAD, "role": "test", "reason": "the module's tests",
                       "text": TEST_TEXT}]}  # fmt: skip
    (fixture / "pack.json").write_text(json.dumps(pack, indent=2) + "\n")
    (fixture / "expected.json").write_text(json.dumps(BUG, indent=2) + "\n")
    shutil.copytree(ROOT / "policy", fixture / "policy")

    title = "`page(items, 1, size)` skips the first page: `start = number * size` treats the 1-based number as 0-based"
    body = "Pages are numbered from 1, so page 1 must start at index 0. Use `start = (number - 1) * size`."
    trace = "page(['a', 'b', 'c'], 1, 2): start = 1 * 2 = 2 -> ['c']; ['a', 'b'] is on no page."
    finding = {**BUG, "title": title, "body": body, "trace": trace, "lessons": []}
    verdict = {"id": "P1", "bucket": "act_on", "reason": "page 1 starts at index size, so the first page is lost",
               "evidence_level": 3, "checked": "line 11 computes start = number * size; the docstring says pages "
               "are numbered from 1", "severity": "important", "confidence": 0.95, "duplicate_of": "", "title": title,
               "body": body, "lessons": []}  # fmt: skip
    runs = [
        {"events": ev.success({"intent": "Add page(), which returns the items on one 1-based page of the listing."})},
        {"events": ev.success({"findings": [finding]})},
        {"events": ev.success({"findings": [finding]})},
        {"events": ev.success({"verdicts": [verdict]})},
    ]
    with tempfile.TemporaryDirectory(prefix="honed-record-") as tmp:
        work = Path(tmp)
        binary = work / "claude"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_claude.py"}" "$@"\n')
        binary.chmod(0o755)
        (work / "scenario.json").write_text(json.dumps({"runs": runs}))
        os.environ.update(FAKE_CLAUDE_SCENARIO=str(work / "scenario.json"), FAKE_CLAUDE_LOG=str(work / "log.jsonl"))
        load(fixture, work / "data")
        code, _, err = _review(_config(work, backend="claude_code", binary=str(binary)), work / "data",
                               fixture / "policy", "--max-calls", "4")  # fmt: skip
        if code != 0:
            raise SystemExit(f"recording failed ({code}):\n{err[-3000:]}")
        calls = SqliteCallStore(_store(work / "data"))
        settings = config.load(CONFIG)
        stages = (settings.models.intent, settings.models.finders, settings.models.verifier)
        rows = calls.cached({model for stage in stages for model in (stage.online, *stage.allowed)})
        calls.close()
    (fixture / "calls.jsonl").write_text("".join(json.dumps(to_json(r), sort_keys=True) + "\n" for r in rows))
    print(f"recorded {len(rows)} answers into {fixture}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--record", action="store_true", help="re-record the fixture through the fake claude")
    args = parser.parse_args(argv)
    if args.record:
        record()
    passed, message = replay()
    print(("PASS: " if passed else "FAIL: ") + message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
