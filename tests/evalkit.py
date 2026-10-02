"""Shared rig for evaluation-based tests: a store with human-review PRs (gold sets, context packs) and a clean PR, the
seed policy's pipeline and the judge on a scripted model, and an evaluation over them."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from builders import comment
from honed.adapters.blobs import BlobStore
from honed.adapters.pack_reader import PackReader
from honed.adapters.sqlite_store import SqliteStore
from honed.core import patches
from honed.core.types import (
    Actor,
    AuthorKind,
    Compare,
    ContextPack,
    Corpus,
    FilePatch,
    GoldIssue,
    GoldProvenance,
    GoldSet,
    HarvestedPR,
    LineBasis,
    Outcome,
    PackFile,
    PackRole,
    PRKey,
    PullRequest,
    Review,
    Severity,
    Thread,
    ThreadLabel,
)
from honed.learn.evaluate import EvalOptions, EvalOutcome, Evaluator
from honed.learn.judge import PROMPTS, JudgeOptions, LLMJudge
from honed.learn.replay import RoundGold, RoundGoldOptions
from reviewkit import (
    BASE,
    HEAD,
    ROOT,
    SERVICE_BASE,
    SERVICE_HEAD,
    SETTINGS,
    DictReader,
    ScriptedLLM,
    finding,
    make_pipeline,
)

INDEX = "Indexing `data['items'][0]` raises IndexError on an empty list"
NAMING = "Rename `data` to something clearer"
GOLD = GoldIssue("o/r#1:t1", "app/service.py", 11, 11, Severity.IMPORTANT, GoldProvenance.HUMAN_FIXED, 1.0,
                 description="parse() crashes with IndexError when items is empty", source_threads=("t1",),
                 category="correctness")  # fmt: skip
BANNED = re.compile(r"\b(eval\w*|judge\w*|experiment\w*|rubric\w*|scor(e|es|ed|ing)|compar\w*|benchmark\w*|"
                    r"candidate\w*|arena\w*|gold|replay\w*|dataset|ground truth|held[- ]out)\b", re.I)  # fmt: skip
TEST_LEAK = re.compile(r"\btest (run|set|split|harness|pr)\b|\bthis is a test\b|\bbeing tested\b", re.I)


def _pr(number: int, *, threads: tuple[Thread, ...] = (), author: str = "dev") -> PullRequest:
    patch = patches.unified_diff(SERVICE_BASE, SERVICE_HEAD, "app/service.py")
    diff = Compare(BASE, HEAD, "ahead", (FilePatch("app/service.py", "modified", patch),), merge_base=BASE)
    return PullRequest(
        repo="o/r", number=number, title="Add a parser for item files", author=Actor(author),
        created_at=f"2026-03-0{number}T00:00:00Z", landed_at="2026-03-09T00:00:00Z", base_ref="main", base_oid=BASE,
        head_oid=HEAD, body="Parse the first item.", threads=threads, reviewed_diff=diff,
        reviews=(Review("r", Actor("rev"), "COMMENTED", "2026-03-02T00:00:00Z", HEAD),),
    )  # fmt: skip


def build_store(path: Path, *, humans: Sequence[str] = ("dev",), name: str = "db.sqlite") -> SqliteStore:
    """PR 1 is human-reviewed with one gold issue (the IndexError); PR 2 is the clean PR. Each further author in
    `humans` adds a human-review PR (3, 4, ...) with the same change and gold issue."""
    store = SqliteStore(path / name, BlobStore(path / "blobs"))
    numbers = [1, *range(3, 2 + len(humans))]
    for number, author in zip(numbers, humans, strict=True):
        tid = f"t{number}"
        thread = Thread(tid, "app/service.py", (comment("rev", body="items can be empty here", commit=HEAD, line=11,
                                                        cid=f"{tid}-0", at="2026-03-02T00:00:00Z"),),
                        line=11, original_line=11, comment_count=1)  # fmt: skip
        label = ThreadLabel(tid, AuthorKind.HUMAN, Outcome.FIXED, True, LineBasis.COMPARE)
        store.upsert_pr(HarvestedPR(_pr(number, threads=(thread,), author=author), "python", Corpus.HUMAN,
                                    AuthorKind.HUMAN, HEAD, (label,)))  # fmt: skip
        gold = GOLD if number == 1 else GoldIssue(f"o/r#{number}:{tid}", "app/service.py", 11, 11, Severity.IMPORTANT,
                                                  GoldProvenance.HUMAN_FIXED, 1.0, description=GOLD.description,
                                                  source_threads=(tid,), category="correctness")  # fmt: skip
        store.save_gold(GoldSet("o/r", number, HEAD, (gold,), candidates=1))
    store.upsert_pr(HarvestedPR(_pr(2), "python", Corpus.APPROVAL_ONLY, AuthorKind.HUMAN, HEAD))
    for number in (*numbers, 2):
        files = []
        for path, commit, text in (("app/service.py", HEAD, SERVICE_HEAD), ("app/service.py", BASE, SERVICE_BASE)):
            files.append(PackFile(path, commit, PackRole.CHANGED, "changed", store.put_blob(text.encode()), len(text)))
        store.save_pack(ContextPack("o/r", number, BASE, HEAD, tuple(files), changed_paths=("app/service.py",)))
    return store


def keys_of(store: SqliteStore) -> list[PRKey]:
    return sorted(store.pr_keys(), key=lambda k: k.number)


def judge_for(llm) -> LLMJudge:
    prompts = {n: (ROOT / "yardstick" / "prompts" / f"{n}.md").read_text() for n in PROMPTS}
    return LLMJudge(llm, prompts, JudgeOptions("claude-fable-5-1", "medium", 16000, 32000, SETTINGS.label.categories))


def verdict(label: str, title: str) -> dict:
    if "IndexError" in title:
        return {"bucket": "act_on", "severity": "important", "evidence_level": 3, "confidence": 0.9}
    if "Rename" in title:
        return {"bucket": "consider", "severity": "nit", "evidence_level": 2, "confidence": 0.7}
    return {"bucket": "dismissed", "severity": "nit", "evidence_level": 1, "confidence": 0.2}


def scripted(**overrides) -> ScriptedLLM:
    members = {
        "a": [finding(title=INDEX), finding(start=10, title=NAMING, severity="nit", category="style")],
        "b": [finding(title=INDEX)],
    }
    values = dict(match=lambda text, gold: "IndexError" in text and "IndexError" in gold,
                  valid=lambda text: "Rename" not in text)  # fmt: skip
    values.update(overrides)
    return ScriptedLLM(members, verdict, **values)


def evaluate(store, *, sample: int = 0, policy=None, llm: ScriptedLLM | None = None,
             keys: Sequence[PRKey] | None = None) -> tuple[EvalOutcome, ScriptedLLM]:  # fmt: skip
    llm = llm or scripted()
    judge = judge_for(llm)
    gold = RoundGold(store, judge, lambda repo: DictReader({}), RoundGoldOptions(1, 3, SETTINGS.metrics.gold_conf,
                                                                                  False))  # fmt: skip
    options = EvalOptions(split="dev", rounds=1, sample=sample, backend="scripted", concurrency=2,
                          code_excerpt_lines=2, max_findings_judged=20, llm_run="run-x")  # fmt: skip
    evaluator = Evaluator(store, store, make_pipeline(llm, policy, store), judge, gold,
                          lambda pack: PackReader(pack, store.get_blob), options)  # fmt: skip
    return evaluator.run(list(keys) if keys is not None else [PRKey("o/r", 1), PRKey("o/r", 2)]), llm
