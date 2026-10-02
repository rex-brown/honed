"""Labeling (ARCHITECTURE.md sections 5, 6 and 11): from mechanical outcomes to judged labels and gold sets.

1. The approval-only split: PRs without a human inline thread move to the clean-PR set (no refetching).
2. Applied suggestions (mechanical): a ```suggestion in a thread's first comment that a later commit applied.
3. Judge jobs, one per thread: the addressed check for `fixed` findings that aren't applied suggestions; stance and
   category for unfixed findings with replies, and category for dismissed ones (the high-risk dismissal check).
4. Judged labels per PR (mechanical, from outcomes plus judgments).
5. Gold sets per human-review PR: qualifying human threads, minus later-round threads (the reviewed-commit rule),
   grouped into issues with severity and category by the judge.
Every step saves as it goes and skips what is already saved, so a stopped run resumes.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field

from honed.core import labels as core_labels
from honed.core import suggestions
from honed.core.outcomes import compare_for, has_reply
from honed.core.types import (
    AuthorKind,
    Corpus,
    GoldIssue,
    GoldProvenance,
    GoldSet,
    HarvestedPR,
    JudgedLabel,
    Judgment,
    JudgmentKind,
    PRKey,
    PRSource,
    Thread,
    ThreadLabel,
)
from honed.learn.evidence import EvidenceBuilder
from honed.learn.jobs import Job, ResetWait, RunReport, run_jobs
from honed.ports.code_reader import CodeReader, ReaderUnavailable
from honed.ports.judge import GoldGroup, Judge, JudgeError, ThreadEvidence
from honed.ports.llm import CallBatch, StopRun
from honed.ports.store import LabelStore, Store

log = logging.getLogger(__name__)

STOP_ON = (StopRun, ReaderUnavailable)


@dataclass(frozen=True)
class LabelOptions:
    concurrency: int
    context_lines: int
    high_risk: frozenset[str]
    gold_conf: Mapping[GoldProvenance, float]
    rebuild_gold: bool = False
    wait_for_reset: ResetWait | None = None  # wait out plan-window stops instead of ending the run
    batch: CallBatch | None = None  # send the judge's calls as message batches (`[llm.anthropic] use_batches`)


@dataclass
class LabelReport:
    prs: int = 0
    split: Counter[str] = field(default_factory=Counter)
    suggestions: Counter[str] = field(default_factory=Counter)
    judge_jobs: RunReport = field(default_factory=RunReport)
    gold_jobs: RunReport = field(default_factory=RunReport)
    gold_skipped: dict[str, str] = field(default_factory=dict)  # PR -> why its gold set was not built


def mark_approval_only(store: Store) -> Counter[str]:
    """Move stored human-review PRs without a human inline thread to the approval-only set (and back, should one
    gain a thread), from the stored thread labels: nothing is refetched."""
    kinds: dict[PRKey, list[AuthorKind]] = {}
    for fact in store.thread_facts():
        kinds.setdefault(PRKey(fact.repo, fact.number), []).append(fact.author_kind)
    moved: Counter[str] = Counter()
    for fact in store.pr_facts():
        key, corpus = PRKey(fact.repo, fact.number), fact.corpus
        if corpus not in (Corpus.HUMAN, Corpus.APPROVAL_ONLY) or fact.source is PRSource.BENCHMARK:
            continue  # a benchmark PR has no threads of ours: its gold is the benchmark's
        wanted = Corpus.APPROVAL_ONLY if core_labels.is_approval_only(kinds.get(key, ())) else Corpus.HUMAN
        moved[f"{wanted.value}"] += 1
        if wanted is not corpus:
            store.set_corpus(key, wanted)
            moved[f"moved_to_{wanted.value}"] += 1
    return moved


class Labeler:
    def __init__(
        self, store: Store, label_store: LabelStore, judge: Judge, reader_for: Callable[[str], CodeReader],
        options: LabelOptions,
    ) -> None:  # fmt: skip
        self._store = store
        self._labels = label_store
        self._judge = judge
        self._reader_for = reader_for
        self._o = options
        self._saving = threading.Lock()  # the store is shared by the job runner's worker threads

    def _save(self, judgment: Judgment) -> None:
        with self._saving:
            self._labels.save_judgment(judgment)

    def _builder(self, repo: str) -> EvidenceBuilder:
        return EvidenceBuilder(self._reader_for(repo), self._o.context_lines)

    def run(self, keys: Sequence[PRKey]) -> LabelReport:
        report = LabelReport()
        items = [item for item in (self._store.get_pr(k) for k in keys) if item is not None]
        items = [item for item in items if item.corpus is not Corpus.APPROVAL_ONLY]
        report.prs = len(items)
        jobs: list[Job] = []
        for item in items:
            report.suggestions += self._detect_suggestions(item)
            jobs += self._judge_jobs(item)
        report.judge_jobs = run_jobs(jobs, concurrency=self._o.concurrency, stop_on=STOP_ON,
                                     wait_for_reset=self._o.wait_for_reset, batch=self._o.batch)  # fmt: skip
        judged = {item.key: self._finalize(item) for item in items}
        if report.judge_jobs.stopped:
            return report
        gold_jobs = []
        for item in items:
            if item.corpus is not Corpus.HUMAN:
                continue
            pending = [lab.thread_id for lab in judged[item.key]
                       if lab.polarity is None and lab.author_kind is AuthorKind.HUMAN]  # fmt: skip
            if pending:
                report.gold_skipped[str(item.key)] = f"{len(pending)} human threads still await a judgment"
                continue
            gold_jobs.append(self._gold_job(item, judged[item.key]))
        report.gold_jobs = run_jobs(gold_jobs, concurrency=self._o.concurrency, stop_on=STOP_ON,
                                    wait_for_reset=self._o.wait_for_reset, batch=self._o.batch)  # fmt: skip
        return report

    # ---- applied suggestions -----------------------------------------------------------------------------

    def _detect_suggestions(self, item: HarvestedPR) -> Counter[str]:
        found: Counter[str] = Counter()
        kinds = {label.thread_id: label for label in item.labels}
        for thread in item.pr.threads:
            label = kinds.get(thread.id)
            if label is None or label.author_kind not in core_labels.REVIEW_KINDS:
                continue
            value, via = self._suggestion_state(item, thread, label)
            found[value] += 1
            self._labels.save_judgment(Judgment(item.pr.repo, item.pr.number, thread.id, JudgmentKind.SUGGESTION,
                                                value, via))  # fmt: skip
        return found

    @staticmethod
    def _suggestion_state(item: HarvestedPR, thread: Thread, label: ThreadLabel) -> tuple[str, str]:
        suggested = suggestions.suggestion(thread)
        if suggested is None:
            return core_labels.NO_SUGGESTION, ""
        if not label.lines_changed:
            return core_labels.NOT_APPLIED, "flagged lines unchanged"
        compare = compare_for(thread, item.pr.compares)
        changed = compare.file(thread.path) if compare else None
        flagged = thread.flagged_lines
        in_patch = (
            changed is not None
            and changed.patch
            and flagged is not None
            and thread.diff_side == "RIGHT"
            and suggestions.applied_in_patch(suggested, changed.patch, flagged[0], flagged[1])
        )
        if in_patch:
            return core_labels.APPLIED, "suggested text found in the change to the flagged lines"
        if suggestions.applied_by_commit(thread, item.pr.commits):
            return core_labels.APPLIED, "a suggestion commit after the comment changed the flagged lines"
        return core_labels.NOT_APPLIED, "suggested text not found in the change"

    # ---- judge jobs --------------------------------------------------------------------------------------

    def _judge_jobs(self, item: HarvestedPR) -> list[Job]:
        existing = {(j.thread_id, j.kind): j for j in self._labels.judgments(item.key)}
        labels = {label.thread_id: label for label in item.labels}
        jobs = []
        for thread in item.pr.threads:
            label = labels.get(thread.id)
            if label is None:
                continue
            suggestion = existing.get((thread.id, JudgmentKind.SUGGESTION))
            applied = suggestion is not None and suggestion.value == core_labels.APPLIED
            if core_labels.needs_addressed_check(label, applied):
                done = (thread.id, JudgmentKind.ADDRESSED) in existing
                jobs.append(Job(f"addressed:{thread.id}", "addressed", self._addressed_run(item, thread),
                                done=lambda d=done: d, pr=str(item.key)))  # fmt: skip
            if core_labels.needs_classification(label, has_reply(thread)):
                done = (thread.id, JudgmentKind.CLASSIFY) in existing
                jobs.append(Job(f"classify:{thread.id}", "classify", self._classify_run(item, thread),
                                done=lambda d=done: d, pr=str(item.key)))  # fmt: skip
        return jobs

    def _addressed_run(self, item: HarvestedPR, thread: Thread) -> Callable[[], None]:
        def run() -> None:
            evidence = self._builder(item.pr.repo).thread(item, thread, with_head=True)
            verdict = self._judge.addressed(evidence)
            self._save(
                Judgment(item.pr.repo, item.pr.number, thread.id, JudgmentKind.ADDRESSED, verdict.verdict.value,
                         verdict.reason, model=self._judge.model)
            )  # fmt: skip

        return run

    def _classify_run(self, item: HarvestedPR, thread: Thread) -> Callable[[], None]:
        def run() -> None:
            evidence = self._builder(item.pr.repo).thread(item, thread)
            verdict = self._judge.classify(evidence)
            stance = verdict.stance.value if verdict.stance else ""
            self._save(
                Judgment(item.pr.repo, item.pr.number, thread.id, JudgmentKind.CLASSIFY, stance, verdict.reason,
                         verdict.category, self._judge.model)
            )  # fmt: skip

        return run

    # ---- judged labels -----------------------------------------------------------------------------------

    def _finalize(self, item: HarvestedPR) -> list[JudgedLabel]:
        by_thread: dict[str, dict[JudgmentKind, Judgment]] = {}
        for j in self._labels.judgments(item.key):
            by_thread.setdefault(j.thread_id, {})[j.kind] = j
        threads = {t.id: t for t in item.pr.threads}
        judged = [
            core_labels.judged_label(label, by_thread.get(label.thread_id, {}),
                                     has_reply=has_reply(threads[label.thread_id]), high_risk=self._o.high_risk)
            for label in item.labels
            if label.thread_id in threads
        ]  # fmt: skip
        self._labels.save_judged_labels(item.key, judged)
        return judged

    # ---- gold sets ---------------------------------------------------------------------------------------

    def _gold_job(self, item: HarvestedPR, judged: Sequence[JudgedLabel]) -> Job:
        def done() -> bool:
            return not self._o.rebuild_gold and self._labels.get_gold(item.key) is not None

        def run() -> None:
            gold = build_gold(item, judged, self._builder(item.pr.repo), self._judge, self._o.gold_conf)
            with self._saving:
                self._labels.save_gold(gold)

        return Job(f"gold:{item.key}", "gold", run, done=done, pr=str(item.key))


def build_gold(item: HarvestedPR, judged: Sequence[JudgedLabel], builder: EvidenceBuilder, judge: Judge,
               gold_conf: Mapping[GoldProvenance, float]) -> GoldSet:  # fmt: skip
    threads = {t.id: t for t in item.pr.threads}
    candidates = [(threads[lab.thread_id], prov) for lab in judged
                  if (prov := core_labels.gold_provenance(lab)) is not None and lab.thread_id in threads]  # fmt: skip
    kept: list[tuple[Thread, GoldProvenance, tuple[int, int]]] = []
    later, unreadable = [], []
    for thread, provenance in candidates:
        location = builder.reviewed_location(item, thread)
        if not location.readable:
            unreadable.append(thread.id)
        elif location.lines is None:
            later.append(thread.id)
        else:
            kept.append((thread, provenance, location.lines))
    issues: list[GoldIssue] = []
    if kept:
        evidence = [builder.thread(item, thread) for thread, _, _ in kept]
        groups = groups_with_retry(judge, str(item.key), item.pr.title, evidence)
        info = {thread.id: (thread, provenance, lines) for thread, provenance, lines in kept}
        for group in groups:
            members = sorted(group.thread_ids, key=lambda tid: (info[tid][0].created_at or "", tid))
            primary, _, lines = info[members[0]]
            provenance = core_labels.strongest(info[tid][1] for tid in members)
            issues.append(GoldIssue(
                id=f"{item.key}:{primary.id}", path=primary.path, start_line=lines[0], end_line=lines[1],
                severity=group.severity, provenance=provenance, conf=gold_conf[provenance],
                description=group.summary, source_threads=tuple(members), category=group.category,
            ))  # fmt: skip
        issues.sort(key=lambda g: (g.path, g.start_line, g.id))
    return GoldSet(
        repo=item.pr.repo, number=item.pr.number, reviewed_commit=item.reviewed_commit, issues=tuple(issues),
        candidates=len(candidates), excluded_later_round=tuple(later), excluded_unreadable=tuple(unreadable),
    )  # fmt: skip


def groups_with_retry(judge: Judge, pr: str, title: str, evidence: Sequence[ThreadEvidence]) -> list[GoldGroup]:
    """One fresh sample when the first answer is unusable (threads left out); then give up on this PR."""
    try:
        return judge.gold_groups(pr, title, evidence)
    except JudgeError as error:
        log.warning("%s: %s; asking for a fresh sample", pr, error)
        return judge.gold_groups(pr, title, evidence, sample=1)


def label_summary(judged: Collection[JudgedLabel]) -> Counter[str]:
    """Counts for reports: judged outcomes, polarities and flags of review findings."""
    counts: Counter[str] = Counter()
    for lab in judged:
        if lab.author_kind not in core_labels.REVIEW_KINDS:
            continue
        counts[f"{lab.author_kind.value}:outcome:{lab.outcome.value}"] += 1
        counts[f"{lab.author_kind.value}:polarity:{lab.polarity.value if lab.polarity else 'pending'}"] += 1
        if lab.addressed:
            counts[f"{lab.author_kind.value}:addressed:{lab.addressed.value}"] += 1
        if lab.stance:
            counts[f"{lab.author_kind.value}:stance:{lab.stance.value}"] += 1
        if lab.applied_suggestion:
            counts[f"{lab.author_kind.value}:applied_suggestion"] += 1
        if lab.high_risk_dismissal:
            counts[f"{lab.author_kind.value}:high_risk_dismissal"] += 1
    return counts
