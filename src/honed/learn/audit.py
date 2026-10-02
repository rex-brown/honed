"""Judge audit (METRICS.md section 5): does the judge agree with strong human outcomes, and with itself?

- Accuracy and Cohen's kappa: the judge rules, blind, whether a review comment is a valid issue. Human-strong
  outcomes are the known answers: a human 👎 means invalid; an applied suggestion or a fix the addressed check
  confirmed means valid.
- Human labels (`yardstick/human_labels/labels-*.json`, one file per labeler): an item's human answer is the
  majority of at least two labelers' valid or not-valid verdicts ("unsure" is left out, `core/consensus.py`). It is
  a judge-independent answer, source `human_majority`, and replaces a human-strong outcome on the same thread. An item
  only one labeler answered is judged too but reported apart (source `human_single`) and never counted.
- Consistency: a sample of items judged `repeats` times each (independent samples of the same prompt).
Thresholds are reported, not enforced, while n is small.
"""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from honed.core import agreement
from honed.core.consensus import AnswerStatus, ItemAnswer
from honed.core.labels import REVIEW_KINDS
from honed.core.types import Addressed, HarvestedPR, JudgedLabel, Outcome, PRKey, Thread
from honed.learn.evidence import EvidenceBuilder
from honed.learn.jobs import Job, ResetWait, RunReport, run_jobs
from honed.learn.label import STOP_ON
from honed.ports.code_reader import CodeReader
from honed.ports.judge import Judge
from honed.ports.llm import CallBatch


@dataclass(frozen=True)
class AuditItem:
    item: HarvestedPR
    thread: Thread
    truth: bool  # valid
    source: str  # thumbs_down, applied_suggestion, addressed_fix, human_majority, human_single
    counted: bool = True  # False: judged and reported by source, but outside the overall accuracy and kappa

    @property
    def id(self) -> str:
        return self.thread.id


def known_answer(label: JudgedLabel) -> tuple[bool, str] | None:
    if label.author_kind not in REVIEW_KINDS:
        return None
    if label.outcome is Outcome.THUMBS_DOWN:
        return False, "thumbs_down"
    if label.applied_suggestion and label.outcome is Outcome.FIXED:
        return True, "applied_suggestion"
    if label.outcome is Outcome.FIXED and label.addressed is Addressed.ADDRESSED:
        return True, "addressed_fix"
    return None


def _order(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode()).hexdigest()


def select_items(candidates: Sequence[AuditItem], n: int) -> list[AuditItem]:
    """Every invalid item (they are rare), then valid ones, in a fixed pseudo-random order, up to n."""
    ranked = sorted(candidates, key=lambda a: (a.truth, _order(a.id)))
    return ranked[:n]


HUMAN_MAJORITY = "human_majority"
HUMAN_SINGLE = "human_single"


def human_items(answers: Sequence[ItemAnswer], where: Callable[[str], PRKey | None],
                get_pr: Callable[[PRKey], HarvestedPR | None]) -> list[AuditItem]:  # fmt: skip
    """Audit items from people's answers (`core/consensus.py`): a majority is counted, a single labeler's answer is
    flagged and not counted; ties, unsure-only items and threads no longer in the store are left out. `where` gives a
    sample id's PR."""
    out = []
    for answer in answers:
        if answer.status not in (AnswerStatus.MAJORITY, AnswerStatus.SINGLE) or answer.answer is None:
            continue
        key = where(answer.thread_id)
        item = get_pr(key) if key is not None else None
        thread = next((t for t in item.pr.threads if t.id == answer.thread_id), None) if item else None
        if item is not None and thread is not None:
            majority = answer.status is AnswerStatus.MAJORITY
            out.append(AuditItem(item, thread, answer.answer, HUMAN_MAJORITY if majority else HUMAN_SINGLE, majority))
    return out


def with_human(selected: Sequence[AuditItem], human: Sequence[AuditItem]) -> list[AuditItem]:
    """Every human-labeled item, then the selected outcome items on other threads."""
    labeled = {a.id for a in human}
    return [*human, *(a for a in selected if a.id not in labeled)]


@dataclass
class AuditReport:
    n: int = 0
    by_source: Counter[str] = field(default_factory=Counter)  # every judged item, counted or not
    accuracy_by_source: dict[str, float | None] = field(default_factory=dict)
    kappa_by_source: dict[str, float | None] = field(default_factory=dict)
    confusion: Counter[str] = field(default_factory=Counter)  # "truth->verdict"
    accuracy: float | None = None
    kappa: float | None = None
    consistency_n: int = 0
    unanimous: float | None = None
    pairwise: float | None = None
    jobs: RunReport = field(default_factory=RunReport)


class JudgeAudit:
    def __init__(self, judge: Judge, reader_for: Callable[[str], CodeReader], *, context_lines: int,
                 concurrency: int, wait_for_reset: ResetWait | None = None,
                 batch: CallBatch | None = None) -> None:  # fmt: skip
        self._judge = judge
        self._batch = batch
        self._reader_for = reader_for
        self._context = context_lines
        self._concurrency = concurrency
        self._wait = wait_for_reset

    def run(self, items: Sequence[AuditItem], *, consistency_items: int, repeats: int) -> AuditReport:
        verdicts: dict[tuple[str, int], bool] = {}
        lock = threading.Lock()
        repeated = {a.id for a in sorted(items, key=lambda a: _order(a.id))[:consistency_items]}

        def job(audit: AuditItem, sample: int) -> Job:
            def run() -> None:
                builder = EvidenceBuilder(self._reader_for(audit.item.pr.repo), self._context)
                verdict = self._judge.validity(builder.thread(audit.item, audit.thread), sample=sample)
                with lock:
                    verdicts[(audit.id, sample)] = verdict.valid

            return Job(f"validity:{audit.id}:{sample}", "audit_validity", run, pr=str(audit.item.key))

        jobs = [job(a, 0) for a in items]
        jobs += [job(a, s) for a in items if a.id in repeated for s in range(1, repeats)]
        report = AuditReport(jobs=run_jobs(jobs, concurrency=self._concurrency, stop_on=STOP_ON,
                                           wait_for_reset=self._wait, batch=self._batch))  # fmt: skip
        pairs = [(a.truth, verdicts[(a.id, 0)]) for a in items if a.counted and (a.id, 0) in verdicts]
        report.n = len(pairs)
        report.by_source = Counter(a.source for a in items if (a.id, 0) in verdicts)
        report.confusion = Counter(f"{'valid' if t else 'invalid'}->{'valid' if v else 'invalid'}" for t, v in pairs)
        report.accuracy = agreement.accuracy(pairs)
        report.kappa = agreement.cohen_kappa(pairs)
        for source in sorted(report.by_source):
            mine = [(a.truth, verdicts[(a.id, 0)]) for a in items if a.source == source and (a.id, 0) in verdicts]
            report.accuracy_by_source[source] = agreement.accuracy(mine)
            report.kappa_by_source[source] = agreement.cohen_kappa(mine)
        runs = [[verdicts[(i, s)] for s in range(repeats) if (i, s) in verdicts] for i in sorted(repeated)]
        runs = [r for r in runs if len(r) == repeats]
        report.consistency_n = len(runs)
        report.unanimous, report.pairwise = agreement.consistency(runs)
        return report


def audit_candidates(pairs: Sequence[tuple[HarvestedPR, Sequence[JudgedLabel]]]) -> list[AuditItem]:
    out = []
    for item, judged in pairs:
        threads = {t.id: t for t in item.pr.threads}
        for label in judged:
            answer = known_answer(label)
            if answer is not None and label.thread_id in threads:
                out.append(AuditItem(item, threads[label.thread_id], answer[0], answer[1]))
    return out


def keys_of(items: Sequence[AuditItem]) -> list[PRKey]:
    return sorted({a.item.key for a in items}, key=str)
