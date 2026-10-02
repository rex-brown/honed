"""The replay evaluation (ARCHITECTURE.md section 6, METRICS.md): review each replayed PR-round of a split with one
policy, have the judge match the posted findings to the round's gold issues, rule unmatched findings valid or not,
and store the `EvalResult`s with everything the diagnostics need.

- Reviews read only the context pack saved at the round's commit, so replays run offline and look like production
  input. The reviewer gets no eval cues: the request is the PR as it stood at that commit.
- The judge sees findings under neutral labels (F1, F2, ... in file order; dismissed and noted findings as D1, ...),
  never the policy, model, member or bucket that produced them. Posted findings are matched one-to-one to gold
  issues (TP), or repeat another finding (DUP); the rest get a blind validity verdict (VU or FP). Dismissed and noted
  findings are matched too, without the one-to-one rule: one that matches a gold issue is a false dismissal.
- Every PR-round is a job: a stop for a plan limit or the call cap ends the run cleanly, and a re-run is served
  from the call cache up to where it stopped.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from honed.core import lines as line_map
from honed.core import marks, patches
from honed.core.evals import EvalRun, RoundRecord
from honed.core.jobs import Job, RunReport
from honed.core.reviews import ReviewRequest, ReviewResult
from honed.core.rounds import ReviewRound
from honed.core.types import (
    Bucket,
    ContextPack,
    Corpus,
    EvalResult,
    Finding,
    FindingClass,
    GoldIssue,
    HarvestedPR,
    Match,
    PRKey,
)
from honed.learn import replay
from honed.learn.jobs import ResetWait, run_jobs
from honed.learn.label import STOP_ON
from honed.learn.replay import RoundGold
from honed.ports.code_reader import CodeReader
from honed.ports.judge import FindingEvidence, GoldEvidence, Judge, JudgeError, MatchVerdict
from honed.ports.llm import CallBatch
from honed.ports.reviewer import Reviewer
from honed.ports.store import LabelStore, Store

log = logging.getLogger(__name__)
ANCHOR_SLACK = 3  # lines outside a hunk that still count as anchored to it


@dataclass(frozen=True)
class EvalOptions:
    split: str
    rounds: int  # review rounds replayed per PR, at most
    sample: int  # the reviewer's model sample index
    backend: str
    concurrency: int  # PR-rounds in flight
    code_excerpt_lines: int
    max_findings_judged: int
    llm_run: str = ""
    wait_for_reset: ResetWait | None = None
    judge: str = ""  # the judge's fingerprint (model and prompts): a stored run is reused only under the same judge
    batch: CallBatch | None = None  # send the calls as message batches (`[llm.anthropic] use_batches`)


@dataclass(frozen=True)
class Planned:
    item: HarvestedPR
    round: ReviewRound
    request: ReviewRequest
    pack: ContextPack
    gold: tuple[GoldIssue, ...]

    @property
    def unit(self) -> str:
        return f"{self.item.key}@{self.round.index}"


@dataclass
class EvalOutcome:
    run: EvalRun
    gold_jobs: RunReport = field(default_factory=RunReport)
    round_jobs: RunReport = field(default_factory=RunReport)


def anchored(finding: Finding, request: ReviewRequest) -> bool:
    """The finding's lines touch a hunk of a changed file (METRICS.md section 3, well-formed output)."""
    patch = next((f.patch for f in request.files if f.path == finding.path), None)
    return any(start - ANCHOR_SLACK <= finding.end_line and finding.start_line <= end + ANCHOR_SLACK
               for start, end in patches.new_ranges(patch or ""))  # fmt: skip


@dataclass(frozen=True)
class RoundQuestions:
    """What the judge is asked about one PR-round, under neutral labels: the posted findings (F1, ... in file order),
    the dismissed and noted ones (D1, ..., up to `max_findings_judged` in all) and the gold issues (G1, ...)."""

    pr: str
    title: str
    posted: tuple[Finding, ...]  # in label order
    others: tuple[Finding, ...]
    findings: tuple[FindingEvidence, ...]
    other_findings: tuple[FindingEvidence, ...]
    gold: tuple[GoldEvidence, ...]
    gold_ids: Mapping[str, str]  # gold label -> gold issue id


def round_questions(item: HarvestedPR, commit: str, gold: Sequence[GoldIssue], review: ReviewResult,
                    reader: CodeReader, *, excerpt_lines: int, max_findings_judged: int) -> RoundQuestions:  # fmt: skip
    """The judge's evidence for one reviewed PR-round, built the same way for the evaluation and for audits that
    re-ask its questions (the cross-family audit)."""
    posted = sorted(review.posted, key=lambda f: (f.path, f.start_line, f.id))
    room = max(0, max_findings_judged - len(posted))
    others = sorted((f for f in review.findings if f.bucket in (Bucket.DISMISSED, Bucket.NOTED)),
                    key=lambda f: (f.path, f.start_line, f.id))[:room]  # fmt: skip
    texts: dict[str, str | None] = {}

    def code(path: str, start: int, end: int) -> str:
        if not path:
            return "(no location given)"
        if path not in texts:
            texts[path] = reader.read(path, commit)
        text = texts[path]
        if text is None:
            return "(code not available)"
        if (start, end) == (0, 0):
            return "(a comment on the whole file)"
        return line_map.excerpt(text, start, end, excerpt_lines, max_lines=40)

    def evidence(label: str, f: Finding) -> FindingEvidence:
        return FindingEvidence(label, f.path, (f.start_line, f.end_line), f"{f.title}\n\n{f.body}".strip(),
                               code(f.path, f.start_line, f.end_line))  # fmt: skip

    threads = {t.id: t for t in item.pr.threads}
    g_ev = []
    for n, g in enumerate(gold, 1):
        primary = threads.get(g.source_threads[0]) if g.source_threads else None
        comment = primary.first.body if primary is not None and primary.first else ""
        g_ev.append(GoldEvidence(f"G{n}", g.path, (g.start_line, g.end_line), g.description, comment[:3000],
                                 code(g.path, g.start_line, g.end_line)))  # fmt: skip
    return RoundQuestions(
        pr=str(item.key), title=item.pr.title, posted=tuple(posted), others=tuple(others),
        findings=tuple(evidence(f"F{n}", f) for n, f in enumerate(posted, 1)),
        other_findings=tuple(evidence(f"D{n}", f) for n, f in enumerate(others, 1)), gold=tuple(g_ev),
        gold_ids={f"G{n}": g.id for n, g in enumerate(gold, 1)},
    )  # fmt: skip


class Evaluator:
    def __init__(
        self, store: Store, labels: LabelStore, reviewer: Reviewer, judge: Judge, round_gold: RoundGold,
        pack_reader: Callable[[ContextPack], CodeReader], options: EvalOptions,
    ) -> None:  # fmt: skip
        self._store = store
        self._labels = labels
        self._reviewer = reviewer
        self._judge = judge
        self._round_gold = round_gold
        self._pack_reader = pack_reader
        self._o = options

    # ---- planning ----------------------------------------------------------------------------------------

    def _replayed(self, item: HarvestedPR) -> tuple[list[ReviewRound], list[ReviewRound]]:
        return replay.rounds_for(item), replay.replayed_rounds(item, self._o.rounds)

    def _gold_jobs(self, items: Sequence[HarvestedPR], golds: dict[PRKey, dict[int, tuple[GoldIssue, ...]]],
                   lock: threading.Lock) -> list[Job]:  # fmt: skip
        jobs = []
        for item in items:
            if item.corpus is not Corpus.HUMAN:
                continue
            rounds, replayed = self._replayed(item)
            cached = self._round_gold.cached(item, replayed)
            if cached is not None:
                golds[item.key] = {i: g.issues for i, g in cached.items()}
                continue

            def run(item: HarvestedPR = item, rounds: list[ReviewRound] = rounds,
                    replayed: list[ReviewRound] = replayed) -> None:  # fmt: skip
                with lock:
                    judged = self._labels.judged_labels(item.key)
                built = self._round_gold.build(item, rounds, replayed, judged)
                with lock:
                    golds[item.key] = {i: g.issues for i, g in built.items()}

            jobs.append(Job(f"gold:{item.key}", "gold_round", run, pr=str(item.key)))
        return jobs

    def _plan(self, items: Sequence[HarvestedPR], golds: Mapping[PRKey, Mapping[int, tuple[GoldIssue, ...]]],
              skipped: dict[str, str]) -> list[Planned]:  # fmt: skip
        planned = []
        for item in items:
            _, replayed = self._replayed(item)
            for round_ in replayed:
                unit = f"{item.key}@{round_.index}"
                if item.corpus is Corpus.HUMAN and item.key not in golds:
                    skipped[unit] = "no gold issues for the round (the gold job failed or did not run)"
                    continue
                waiting = self._store.stripped(item.key)
                if waiting:
                    skipped[unit] = marks.waiting_for(waiting)
                    continue
                pack = self._store.get_round_pack(item.key, round_.commit)
                if pack is None:
                    skipped[unit] = f"no context pack at {round_.commit[:10]} (`honed pack --rounds N`)"
                    continue
                diff = replay.diff_for(item, round_, pack)
                if diff is None:
                    skipped[unit] = "no diff for the round"
                    continue
                gold = golds.get(item.key, {}).get(round_.index, ())
                planned.append(Planned(item, round_, replay.request_for(item, round_, diff), pack, tuple(gold)))
        return planned

    # ---- running -----------------------------------------------------------------------------------------

    def run(self, keys: Sequence[PRKey]) -> EvalOutcome:
        started = dt.datetime.now(dt.UTC)
        items = [item for item in (self._store.get_pr(k) for k in keys) if item is not None]
        lock = threading.Lock()
        golds: dict[PRKey, dict[int, tuple[GoldIssue, ...]]] = {}
        gold_report = run_jobs(self._gold_jobs(items, golds, lock), concurrency=self._o.concurrency, stop_on=STOP_ON,
                               wait_for_reset=self._o.wait_for_reset, batch=self._o.batch)  # fmt: skip
        skipped: dict[str, str] = {f"{job}": error for job, error in gold_report.failed.items()}
        planned = [] if gold_report.stopped else self._plan(items, golds, skipped)
        records: list[RoundRecord] = []

        def job(p: Planned) -> Job:
            def run() -> None:
                record = self.evaluate(p)
                with lock:
                    records.append(record)

            return Job(f"round:{p.unit}", "eval_round", run, pr=str(p.item.key))

        round_report = run_jobs([job(p) for p in planned], concurrency=self._o.concurrency, stop_on=STOP_ON,
                                wait_for_reset=self._o.wait_for_reset, batch=self._o.batch)  # fmt: skip
        for job_id, error in round_report.failed.items():
            skipped[job_id.removeprefix("round:")] = error
        records.sort(key=lambda r: (r.result.pr.repo, r.result.pr.number, r.result.round))
        run = EvalRun(
            id=started.strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6], policy_hash=self._reviewer.policy_hash,
            split=self._o.split, backend=self._o.backend, rounds=self._o.rounds, sample=self._o.sample,
            created_at=started.isoformat(), llm_run=self._o.llm_run, records=tuple(records), skipped=skipped,
            stopped=gold_report.stopped or round_report.stopped, prs=tuple(str(k) for k in keys), judge=self._o.judge,
        )  # fmt: skip
        return EvalOutcome(run, gold_report, round_report)

    def evaluate(self, p: Planned) -> RoundRecord:
        reader = self._pack_reader(p.pack)
        review = self._reviewer.review(p.request, reader, sample=self._o.sample)
        matches, others = self.judge_round(p, review, reader)
        ids = tuple(f.id for f in review.posted if anchored(f, p.request))
        result = EvalResult(
            pr=p.item.key, language=p.item.language, gold=p.gold, findings=review.posted, matches=tuple(matches),
            cost_usd=review.cost_usd, latency_s=review.latency_s, round=p.round.index,
        )  # fmt: skip
        return RoundRecord(
            result=result, review=review, corpus=p.item.corpus.value, head_commit=p.round.commit,
            other_matches=tuple(others), anchored=len(ids), anchored_ids=ids,
        )  # fmt: skip

    # ---- judging -----------------------------------------------------------------------------------------

    def judge_round(self, p: Planned, review: ReviewResult, reader: CodeReader) -> tuple[list[Match], list[Match]]:
        q = round_questions(p.item, p.round.commit, p.gold, review, reader, excerpt_lines=self._o.code_excerpt_lines,
                            max_findings_judged=self._o.max_findings_judged)  # fmt: skip
        posted, others, f_ev, d_ev, g_ev = q.posted, q.others, list(q.findings), list(q.other_findings), list(q.gold)
        pr, title = q.pr, q.title
        verdicts: list[MatchVerdict] = []
        if g_ev and (f_ev or d_ev):
            try:
                verdicts = self._judge.match(pr, title, f_ev, d_ev, g_ev)
            except JudgeError as error:
                log.warning("%s: %s; asking for a fresh sample", p.unit, error)
                verdicts = self._judge.match(pr, title, f_ev, d_ev, g_ev, sample=1)
        by_label = {v.label: v for v in verdicts}
        gold_id = q.gold_ids
        finding_id = {e.label: f.id for e, f in zip(f_ev, posted, strict=True)}
        matches: dict[str, Match] = {}
        unmatched = []
        for e, f in zip(f_ev, posted, strict=True):
            v = by_label.get(e.label)
            if v is not None and v.gold is not None:
                matches[f.id] = Match(f.id, FindingClass.TP, gold_id[v.gold], v.reason)
            elif v is not None and v.duplicate_of is not None:
                matches[f.id] = Match(f.id, FindingClass.DUP, None, f"repeats {finding_id[v.duplicate_of]}: {v.reason}")
            else:
                unmatched.append(e)
        if unmatched:
            try:
                validity = self._judge.validity_many(pr, title, unmatched)
            except JudgeError as error:
                log.warning("%s: %s; asking for a fresh sample", p.unit, error)
                validity = self._judge.validity_many(pr, title, unmatched, sample=1)
            for e in unmatched:
                v = validity[e.label]
                fid = finding_id[e.label]
                matches[fid] = Match(fid, FindingClass.VU if v.valid else FindingClass.FP, None, v.reason,
                                     judged_severity=v.severity if v.valid else None)  # fmt: skip
        other_matches = []
        for e, f in zip(d_ev, others, strict=True):
            v = by_label.get(e.label)
            hit = v is not None and v.gold is not None
            other_matches.append(Match(f.id, FindingClass.TP if hit else FindingClass.FP,
                                       gold_id[v.gold] if hit and v is not None and v.gold else None,
                                       v.reason if v is not None else ""))  # fmt: skip
        return [matches[f.id] for f in review.posted], other_matches  # in the findings' order
