"""The cross-family audit (METRICS.md section 5; ARCHITECTURE.md sections 6 and 8): Claude judging findings from Claude
reviewers risks self-preference, so a judge from another model family re-asks a sample of Fable's questions.

- Validity: the blind validity question Fable answered for a PR-round's unmatched posted findings, rebuilt from the
  stored run (the same evidence, labels and code excerpts, so the same prompt) and put to the other judge. Agreement
  and Cohen's kappa are over findings; kappa below `[judge] cross_family_alarm_kappa` raises the alarm.
- Matching: the match question of PR-rounds with gold issues, put to the other judge as the offline matcher. Its
  (finding, gold issue) pairs, for posted and for dismissed or noted findings, are scored against Fable's: precision,
  recall and F1 against `[judge] matcher_min_f1`.
Items are chosen in a fixed pseudo-random order, PR-rounds with an invalid verdict first (they are rare), until the
sample holds `n` findings. One question per PR-round, asked once, one at a time.

The alarm counts only once the local judge is itself trustworthy: its validity verdicts on the imported human labels
must reach `[judge] local_min_human_accuracy`. Until then a low kappa says the local judge is weak, not that Fable
prefers Claude (phase 4a: the local judge ruled 58 of 60 findings valid), and the alarm is report-only.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from honed.core import agreement
from honed.core.evals import EvalRun, RoundRecord
from honed.core.jobs import Job
from honed.core.types import ContextPack, FindingClass
from honed.learn.evaluate import RoundQuestions, round_questions
from honed.learn.jobs import run_jobs
from honed.learn.label import STOP_ON
from honed.ports.code_reader import CodeReader
from honed.ports.judge import Judge, JudgeError
from honed.ports.llm import LLMError
from honed.ports.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditOptions:
    validity_items: int  # findings in the validity sample, at least
    match_rounds: int  # PR-rounds whose match question is re-asked
    excerpt_lines: int
    max_findings_judged: int


@dataclass(frozen=True)
class AskedRound:
    """One stored PR-round, its rebuilt questions, and Fable's answers to them."""

    run_id: str
    unit: str  # owner/name#n@round
    questions: RoundQuestions
    validity: dict[str, bool]  # finding label -> Fable ruled it valid (VU) or not (FP)
    matches: dict[str, str | None]  # finding or other-note label -> the gold label Fable matched, or None

    @property
    def order(self) -> str:
        return hashlib.sha256(f"{self.unit}\0{sorted(self.validity)}".encode()).hexdigest()


@dataclass
class CrossFamilyReport:
    model: str
    validity_n: int = 0
    validity_rounds: int = 0
    confusion: Counter[str] = field(default_factory=Counter)  # "fable->local", valid / invalid
    agreement: float | None = None
    kappa: float | None = None
    alarm_kappa: float = 0.0
    match_rounds: int = 0
    match_decisions: int = 0
    decision_agreement: float | None = None
    pairs_fable: int = 0
    pairs_local: int = 0
    pairs_both: int = 0
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    min_f1: float = 0.0
    failures: dict[str, str] = field(default_factory=dict)  # question -> why the local judge gave no usable answer
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    human_n: int = 0  # imported human labels the local judge answered
    human_accuracy: float | None = None  # the local judge's validity accuracy on them
    min_human_accuracy: float = 0.0

    @property
    def alarm(self) -> bool | None:
        """kappa below the alarm level (whether or not the alarm counts)."""
        return None if self.kappa is None else self.kappa < self.alarm_kappa

    @property
    def alarm_counts(self) -> bool:
        """The alarm is acted on only when the local judge agrees with people at `min_human_accuracy` or better."""
        return self.human_accuracy is not None and self.human_accuracy >= self.min_human_accuracy

    def alarm_status(self) -> str:
        if self.alarm is None:
            return "undefined"
        if not self.alarm:
            return "ok"
        if self.alarm_counts:
            return "ALARM"
        why = ("no human labels yet" if not self.human_n else
               f"the local judge's accuracy on {self.human_n} human labels is {self.human_accuracy:.3f}, below "
               f"{self.min_human_accuracy}")  # fmt: skip
        return f"alarm level, report-only: {why}"

    @property
    def matcher_passes(self) -> bool | None:
        return None if self.f1 is None else self.f1 >= self.min_f1


def fable_answers(record: RoundRecord, q: RoundQuestions) -> tuple[dict[str, bool], dict[str, str | None]]:
    """Fable's verdicts on a stored PR-round, keyed by the questions' labels."""
    by_id = {m.finding_id: m for m in record.result.matches}
    gold_label = {gid: label for label, gid in q.gold_ids.items()}
    validity: dict[str, bool] = {}
    matches: dict[str, str | None] = {}
    for evidence, finding in zip(q.findings, q.posted, strict=True):
        m = by_id.get(finding.id)
        if m is None:
            continue
        if m.klass in (FindingClass.VU, FindingClass.FP):
            validity[evidence.label] = m.klass is FindingClass.VU
        matches[evidence.label] = gold_label.get(m.gold_id or "") if m.klass is FindingClass.TP else None
    others = {m.finding_id: m for m in record.other_matches}
    for evidence, finding in zip(q.other_findings, q.others, strict=True):
        m = others.get(finding.id)
        if m is not None:
            matches[evidence.label] = gold_label.get(m.gold_id or "") if m.klass is FindingClass.TP else None
    return validity, matches


def collect(runs: Sequence[EvalRun], store: Store, pack_reader: Callable[[ContextPack], CodeReader],
            options: AuditOptions) -> list[AskedRound]:  # fmt: skip
    """Every stored PR-round of `runs` with its questions rebuilt; a question asked in several runs once."""
    out, seen = [], set()
    for run in runs:
        for record in run.records:
            key = record.result.pr
            item = store.get_pr(key)
            pack = store.get_round_pack(key, record.head_commit) if item else None
            if item is None or pack is None:
                continue
            q = round_questions(item, record.head_commit, record.result.gold, record.review, pack_reader(pack),
                                excerpt_lines=options.excerpt_lines,
                                max_findings_judged=options.max_findings_judged)  # fmt: skip
            fingerprint = (str(key), record.result.round, tuple(e.text for e in (*q.findings, *q.other_findings)))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            validity, matches = fable_answers(record, q)
            out.append(AskedRound(run.id, f"{key}@{record.result.round}", q, validity, matches))
    return out


def select_validity(rounds: Sequence[AskedRound], n: int) -> list[AskedRound]:
    """PR-rounds with an invalid verdict first, then the rest, each group in a fixed pseudo-random order, until the
    sample holds `n` findings."""
    ranked = sorted((r for r in rounds if r.validity), key=lambda r: (all(r.validity.values()), r.order))
    chosen, total = [], 0
    for r in ranked:
        if total >= n:
            break
        chosen.append(r)
        total += len(r.validity)
    return chosen


def select_matching(rounds: Sequence[AskedRound], n: int) -> list[AskedRound]:
    """PR-rounds with gold issues: those where Fable matched something first (so recall is defined), each group in a
    fixed pseudo-random order."""
    ranked = sorted((r for r in rounds if r.questions.gold and r.matches),
                    key=lambda r: (not any(r.matches.values()), r.order))  # fmt: skip
    return ranked[:n]


class CrossFamilyAudit:
    def __init__(self, judge: Judge, *, alarm_kappa: float, min_f1: float) -> None:
        self._judge = judge
        self._alarm = alarm_kappa
        self._min_f1 = min_f1

    def run(self, validity_rounds: Sequence[AskedRound], match_rounds: Sequence[AskedRound]) -> CrossFamilyReport:
        report = CrossFamilyReport(self._judge.model, alarm_kappa=self._alarm, min_f1=self._min_f1)
        lock = threading.Lock()
        local_valid: dict[tuple[str, str], bool] = {}
        local_match: dict[tuple[str, str], str | None] = {}

        def validity_job(r: AskedRound) -> Job:
            def run() -> None:
                q = r.questions
                asked = [e for e in q.findings if e.label in r.validity]
                try:
                    verdicts = self._judge.validity_many(q.pr, q.title, asked)
                except (JudgeError, LLMError) as error:
                    with lock:
                        report.failures[f"validity:{r.unit}"] = str(error)[:300]
                    return
                with lock:
                    local_valid.update({(r.unit, label): v.valid for label, v in verdicts.items()})

            return Job(f"validity:{r.unit}", "crossfamily_validity", run, pr=r.questions.pr)

        def match_job(r: AskedRound) -> Job:
            def run() -> None:
                q = r.questions
                try:
                    verdicts = self._judge.match(q.pr, q.title, q.findings, q.other_findings, q.gold)
                except (JudgeError, LLMError) as error:
                    with lock:
                        report.failures[f"match:{r.unit}"] = str(error)[:300]
                    return
                with lock:
                    local_match.update({(r.unit, v.label): v.gold for v in verdicts})

            return Job(f"match:{r.unit}", "crossfamily_match", run, pr=r.questions.pr)

        jobs = [validity_job(r) for r in validity_rounds] + [match_job(r) for r in match_rounds]
        jobs_report = run_jobs(jobs, concurrency=1, stop_on=STOP_ON)
        for job_id, error in jobs_report.failed.items():
            report.failures.setdefault(job_id, error[:300])

        pairs = []
        for r in validity_rounds:
            for label, fable in sorted(r.validity.items()):
                local = local_valid.get((r.unit, label))
                if local is None:
                    continue
                pairs.append((fable, local))
                report.confusion[f"{_word(fable)}->{_word(local)}"] += 1
                if fable != local:
                    finding = next(e for e in r.questions.findings if e.label == label)
                    report.disagreements.append({"unit": r.unit, "label": label, "fable": _word(fable),
                                                 "local": _word(local), "finding": finding.text[:200]})  # fmt: skip
        report.validity_n = len(pairs)
        report.validity_rounds = sum(any((r.unit, lab) in local_valid for lab in r.validity) for r in validity_rounds)
        report.agreement = agreement.accuracy(pairs)
        report.kappa = agreement.cohen_kappa(pairs)

        fable_pairs: set[tuple[str, str, str]] = set()
        local_pairs: set[tuple[str, str, str]] = set()
        decisions = []
        for r in match_rounds:
            if not any((r.unit, label) in local_match for label in r.matches):
                continue
            report.match_rounds += 1
            for label, fable_gold in r.matches.items():
                local_gold = local_match.get((r.unit, label))
                decisions.append((fable_gold, local_gold))
                if fable_gold is not None:
                    fable_pairs.add((r.unit, label, fable_gold))
                if local_gold is not None:
                    local_pairs.add((r.unit, label, local_gold))
        report.match_decisions = len(decisions)
        report.decision_agreement = agreement.accuracy(decisions)
        report.pairs_fable, report.pairs_local = len(fable_pairs), len(local_pairs)
        report.pairs_both = len(fable_pairs & local_pairs)
        report.precision, report.recall, report.f1 = agreement.set_f1(fable_pairs, local_pairs)
        if jobs_report.stopped:
            report.failures["stopped"] = jobs_report.stopped
        return report


def _word(valid: bool) -> str:
    return "valid" if valid else "invalid"
