"""Labeling end to end without the network: a real store and judge, a dictionary of file versions for the code, and
a scripted LLM behind the call cache. Covers the approval-only split, applied suggestions, the addressed check,
stances, high-risk dismissals, the reviewed-commit rule, deduplication, resuming, replay and the judge audit."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from builders import comment
from honed import config
from honed.adapters.blobs import BlobStore
from honed.adapters.cached_llm import CachedLLM
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.replay_llm import ReplayLLM
from honed.adapters.sqlite_store import SqliteStore
from honed.core import patches
from honed.core.types import (
    Actor,
    AuthorKind,
    Compare,
    Corpus,
    FilePatch,
    GoldProvenance,
    HarvestedPR,
    LineBasis,
    Outcome,
    Polarity,
    PRKey,
    PullRequest,
    Review,
    Severity,
    Thread,
    ThreadLabel,
)
from honed.learn.audit import JudgeAudit, audit_candidates, select_items
from honed.learn.judge import PROMPTS, JudgeOptions, LLMJudge
from honed.learn.label import Labeler, LabelOptions, mark_approval_only
from honed.ports.llm import LLMCall, LLMResult, Usage

ROOT = Path(__file__).resolve().parents[1]
R, A, H = "r" * 40, "a" * 40, "h" * 40  # reviewed commit, a later-round anchor, final head
AT_R = "import os\nvalue = compute()\nname = 'c'\nrun(value)\n"
AT_A = AT_R + "extra = 1\n"
AT_H = "import os\nvalue = compute(safe=True)\nlabel = 'c'\nrun(value)\nextra = 1\n"
FILES = {("app.py", R): AT_R, ("app.py", A): AT_A, ("app.py", H): AT_H}


class DictReader:
    def __init__(self, files: dict[tuple[str, str], str]) -> None:
        self.files = files

    def read(self, path: str, commit: str) -> str | None:
        return self.files.get((path, commit))

    def grep(self, *a, **k):
        return []

    def list_files(self, commit: str, prefix: str = "") -> list[str]:
        return sorted(p for p, c in self.files if c == commit)

    def prefetch(self, commit, paths) -> None:
        return None


class FakeFable:
    """Answers each judge question from the evidence text, like a judge that reads carefully."""

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []

    def complete(self, call: LLMCall) -> LLMResult:
        self.calls.append(call)
        text = call.user
        if call.stage == "addressed":
            verdict = "not_addressed" if "explain" in text else "addressed"
            data = {"reason": "compared the code", "verdict": verdict}
        elif call.stage == "classify":
            unsafe = "unsafe" in text
            data = {"reason": "r", "stance": "disagree" if unsafe else "agree",
                    "category": "security" if unsafe else "correctness"}  # fmt: skip
        elif call.stage == "gold":
            by_line: dict[str, list[str]] = {}
            for sid, line in re.findall(r"=== Thread (T\d+) ===\nFile: app.py, line (\d+)", text):
                by_line.setdefault(line, []).append(sid)
            data = {"issues": [{"thread_ids": ids, "severity": "important" if line == "2" else "nit",
                                "category": "correctness", "summary": f"line {line}"}
                               for line, ids in by_line.items()]}  # fmt: skip
        else:  # audit_validity
            data = {"reason": "r", "valid": "unsafe" not in text}
        return LLMResult(text="", data=data, usage=Usage(100, 50, 0, 0, 0.01, 1.0), model=call.model)


def human_thread(tid: str, line: int, body: str, *, anchor: str = R, replies: tuple[str, ...] = (),
                 resolved: bool = False, at: str = "2026-03-01T00:00:00Z") -> Thread:  # fmt: skip
    comments = (comment("rev", body=body, commit=anchor, line=line, cid=f"{tid}-0", at=at),)
    comments += tuple(comment("dev", body=r, commit=anchor, line=line, cid=f"{tid}-{i + 1}", at="2026-03-02T00:00:00Z")
                      for i, r in enumerate(replies))  # fmt: skip
    return Thread(id=tid, path="app.py", comments=comments, is_resolved=resolved, line=line, original_line=line,
                  comment_count=len(comments))  # fmt: skip


THREADS = [
    # (thread, outcome): t1 an applied suggestion; t7 the same issue raised again after a later push (anchor A)
    (human_thread("t1", 2, "Pass safe:\n```suggestion\nvalue = compute(safe=True)\n```"), Outcome.FIXED),
    (human_thread("t7", 2, "compute() must be safe here too", anchor=A, at="2026-03-03T00:00:00Z"), Outcome.FIXED),
    (human_thread("t2", 3, "rename name to label"), Outcome.FIXED),
    (human_thread("t5", 3, "please explain what 'c' means"), Outcome.FIXED),  # changed, but not addressed
    (human_thread("t3", 5, "extra should be a constant", anchor=A, replies=("good point, will do",)),
     Outcome.OPEN_AT_MERGE),  # later round: line 5 did not exist at the reviewed commit
    (human_thread("t4", 4, "run(value) is unsafe with user input", replies=("it is fine, input is trusted",),
                  resolved=True), Outcome.RESOLVED_NO_CHANGE),  # a high-risk dismissal
    (human_thread("t6", 1, "note to self"), Outcome.IGNORED),  # opened by the PR author below
]  # fmt: skip


def harvested() -> list[HarvestedPR]:
    threads = [t for t, _ in THREADS]
    labels = [
        ThreadLabel(t.id, AuthorKind.PR_AUTHOR if t.id == "t6" else AuthorKind.HUMAN, o, o is Outcome.FIXED,
                    LineBasis.COMPARE)
        for t, o in THREADS
    ]  # fmt: skip
    compares = (
        Compare(R, H, "ahead", (FilePatch("app.py", "modified", patches.unified_diff(AT_R, AT_H, "app.py")),)),
        Compare(A, H, "ahead", (FilePatch("app.py", "modified", patches.unified_diff(AT_A, AT_H, "app.py")),)),
    )
    pr = PullRequest(
        repo="o/r", number=1, title="Make compute safe", author=Actor("dev"), created_at="2026-03-01T00:00:00Z",
        landed_at="2026-03-05T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=H, threads=tuple(threads),
        reviews=(Review("rv", Actor("rev"), "COMMENTED", "2026-03-01T00:00:00Z", R),), compares=compares,
    )  # fmt: skip
    clean = PullRequest(
        repo="o/r", number=2, title="Docs", author=Actor("dev"), created_at="2026-03-01T00:00:00Z",
        landed_at="2026-03-02T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=H,
        reviews=(Review("rv2", Actor("rev"), "APPROVED", "2026-03-01T00:00:00Z", H),),
    )  # fmt: skip
    return [
        HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, R, tuple(labels)),
        HarvestedPR(clean, "python", Corpus.HUMAN, AuthorKind.HUMAN, H, ()),
    ]


@pytest.fixture
def rig(tmp_path):
    store = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    calls = SqliteCallStore(tmp_path / "db.sqlite")
    for item in harvested():
        store.upsert_pr(item)
    yield store, calls
    calls.close()
    store.close()


def make_labeler(store, llm, *, rebuild=False) -> Labeler:
    settings = config.load(ROOT / "honed.toml")
    prompts = {n: (ROOT / "yardstick" / "prompts" / f"{n}.md").read_text() for n in PROMPTS}
    judge = LLMJudge(llm, prompts, JudgeOptions("claude-fable-5-1", "medium", 16000, 32000, settings.label.categories))
    options = LabelOptions(concurrency=2, context_lines=3, high_risk=frozenset(settings.safety.high_risk_categories),
                           gold_conf=settings.metrics.gold_conf, rebuild_gold=rebuild)  # fmt: skip
    return Labeler(store, store, judge, lambda repo: DictReader(FILES), options)


def test_labeling_end_to_end(rig):
    store, calls = rig
    assert mark_approval_only(store)["moved_to_approval_only"] == 1
    assert store.get_pr(PRKey("o/r", 2)).corpus is Corpus.APPROVAL_ONLY
    assert store.get_pr(PRKey("o/r", 1)).corpus is Corpus.HUMAN

    fable = FakeFable()
    report = make_labeler(store, CachedLLM(fable, calls, run_id="run1")).run(store.pr_keys())
    assert report.prs == 1 and report.suggestions == {"applied": 1, "none": 5}
    assert report.judge_jobs.completed == 5 and report.judge_jobs.finished  # 3 addressed + 2 classify
    assert sorted(c.stage for c in fable.calls) == ["addressed"] * 3 + ["classify"] * 2 + ["gold"]

    labels = {lab.thread_id: lab for lab in store.judged_labels(PRKey("o/r", 1))}
    assert labels["t1"].applied_suggestion and labels["t1"].polarity is Polarity.POSITIVE
    assert labels["t5"].outcome is Outcome.CHANGED_UNADDRESSED and labels["t5"].polarity is Polarity.NEUTRAL
    assert labels["t4"].high_risk_dismissal and labels["t4"].polarity is Polarity.NEUTRAL
    assert labels["t3"].stance.value == "agree" and labels["t6"].polarity is Polarity.NEUTRAL

    gold = store.get_gold(PRKey("o/r", 1))
    assert gold.candidates == 4 and gold.excluded_later_round == ("t3",) and gold.excluded_unreadable == ()
    by_line = {g.start_line: g for g in gold.issues}
    assert set(by_line) == {2, 3}
    merged = by_line[2]  # t1 and t7 are one issue; t1 came first and is an applied suggestion
    assert merged.source_threads == ("t1", "t7") and merged.provenance is GoldProvenance.APPLIED_SUGGESTION
    assert merged.severity is Severity.IMPORTANT and merged.conf == 1.0 and merged.id == "o/r#1:t1"
    assert by_line[3].source_threads == ("t2",) and by_line[3].provenance is GoldProvenance.HUMAN_FIXED
    gold_call = next(c for c in fable.calls if c.stage == "gold")
    assert "rename name to label" in gold_call.user and "explain" not in gold_call.user

    # A second run resumes: every judgment and the gold set are saved, so nothing is asked again.
    again = FakeFable()
    second = make_labeler(store, CachedLLM(again, calls, run_id="run2")).run(store.pr_keys())
    assert again.calls == [] and second.judge_jobs.skipped == 5 and second.gold_jobs.skipped == 1

    # Rebuilding the gold set offline replays the cached answer.
    replayed = make_labeler(store, CachedLLM(ReplayLLM(calls), calls, run_id="run3"), rebuild=True)
    assert replayed.run(store.pr_keys()).gold_jobs.completed == 1
    assert store.get_gold(PRKey("o/r", 1)).issues == gold.issues
    ledger = calls.ledger()
    assert sum(not e.cached for e in ledger) == 6 and ledger[-1].cached and ledger[-1].stage == "gold"


def test_the_judge_audit_scores_against_human_strong_outcomes(rig):
    store, calls = rig
    fable = FakeFable()
    make_labeler(store, CachedLLM(fable, calls, run_id="run1")).run(store.pr_keys())
    pairs = [(store.get_pr(k), store.judged_labels(k)) for k in store.pr_keys()]
    candidates = audit_candidates(pairs)
    assert sorted((a.id, a.truth, a.source) for a in candidates) == [
        ("t1", True, "applied_suggestion"), ("t2", True, "addressed_fix"), ("t7", True, "addressed_fix"),
    ]  # fmt: skip
    settings = config.load(ROOT / "honed.toml")
    prompts = {n: (ROOT / "yardstick" / "prompts" / f"{n}.md").read_text() for n in PROMPTS}
    judge = LLMJudge(CachedLLM(fable, calls, run_id="audit"), prompts,
                     JudgeOptions("claude-fable-5-1", "medium", 16000, 32000, settings.label.categories))  # fmt: skip
    audit = JudgeAudit(judge, lambda repo: DictReader(FILES), context_lines=3, concurrency=2)
    report = audit.run(select_items(candidates, 40), consistency_items=2, repeats=3)
    assert report.n == 3 and report.accuracy == 1.0 and report.kappa is None  # one class only: undefined
    assert report.consistency_n == 2 and report.unanimous == 1.0
    samples = sorted(c.sample for c in fable.calls if c.stage == "audit_validity")
    assert samples == [0, 0, 0, 1, 1, 2, 2]
