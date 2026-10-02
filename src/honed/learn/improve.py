"""The improve loop (ARCHITECTURE.md section 7; METRICS.md sections 3 and 6): propose, self-review, evaluate, gate,
promote, round after round.

A round:
1. Loads the incumbent from the policy directory and its evaluation on the gate split (a stored run is reused), and
   on the feed sample (`learn/feed.py`: `[improve] feed_sample_rounds` PR-rounds of the feed split, stratified by
   language, drawn by the incumbent's hash, so the stored run is reused until a promotion redraws it), whose
   failures the proposer reads (with one dev split, the gate run serves both). The round report lists the sample's
   PR-rounds and composition.
2. Checks the gate's precondition (the latest sensitivity check separates), else refuses, unless overridden.
3. Asks `candidates_per_round` generators in parallel for one proposal each, every one against the incumbent.
4. Takes each candidate through: apply the edit and load the policy; refuse paths the promote step may not write;
   lesson acceptance; skip a policy identical to one already rejected; self-review by the incumbent (with the
   policy-change lens); the screen (a fixed, language-stratified `[gate] screen_fraction` of the gate split, compared
   with the incumbent's stored run on the same PR-rounds; a delta-S not above 0 is `screened_out`); evaluation on
   the gate split; the gate (a pure removal whose removed lessons barely fired is `unmeasured`, not promoted).
5. Promotes the best candidate that passed (largest delta-S); others that passed are superseded (one change per
   round, never stacked). Every candidate gets a decision-log row and a stored record.
Plateaus: after `plateau_rejects` candidates in a row without a promotion, the next round starts its rotation at the
least-tried generator and gives one slot to combining near-misses. The loop stops after `rounds`, or once the
incumbent reaches `target_s` after at least `min_attempts` candidates; the target is never relaxed. A plan limit or
the call cap stops it cleanly, with the candidate in flight recorded as incomplete, and so does an interrupt (SIGINT,
Ctrl-C): the candidate in flight is recorded as `incomplete` ("interrupted"), the round's bookkeeping (records,
decision-log rows, a promotion already earned) is finished, and the loop stops.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from honed.core.evals import Decision, EvalRun
from honed.core.improve import (
    CandidateRecord,
    EditKind,
    GeneratorKind,
    Outcome,
    PolicyEdit,
    Proposal,
    is_pure_removal,
    policy_diff,
)
from honed.core.jobs import Job, RunReport
from honed.core.policy import CONFIG_FILE, Policy
from honed.core.types import PRKey
from honed.learn import eval_report, gate, lessons, policy_edit, propose, selfreview, splits
from honed.learn.eval_report import ReportSettings
from honed.learn.feed import FeedSelector
from honed.learn.jobs import run_jobs
from honed.learn.label import STOP_ON
from honed.learn.promote import IncumbentKey, Promoter, PromotionRefused, refused_paths
from honed.ports.llm import LLMError, StopRun
from honed.ports.policy import PolicyDirectory, PolicyFiles
from honed.ports.reviewer import Reviewer
from honed.ports.store import EvalStore, ImproveStore, Store

log = logging.getLogger(__name__)

INTERRUPTED = "interrupted (SIGINT)"


@dataclass(frozen=True)
class ImproveOptions:
    rounds: int
    min_attempts: int
    candidates_per_round: int
    plateau_rejects: int
    target_s: float | None
    generators: tuple[GeneratorKind, ...]  # the usual rotation
    feed_split: str  # the proposer mines failures on a sample of this split (`learn/feed.py`)
    gate_split: str  # the gate decides on this split
    eval_rounds: int  # review rounds replayed per PR
    backend: str
    offline: bool  # every promotion is provisional
    ignore_sensitivity: bool
    max_failure_cases: int
    prefix: str  # the policy directory relative to the project root ("policy/")
    language: str  # the language group of the self-review's synthetic PR


class Evaluate(Protocol):
    def __call__(self, policy: Policy, split: str, keys: Sequence[PRKey] | None = None,
                 suffix: str = splits.SCREEN_SPLIT_SUFFIX) -> EvalRun:  # fmt: skip
        """The policy's run on the split, or on `keys` of it, stored under the split's name plus `suffix` (the
        screen's `/screen`, the feed sample's `/feed`); a complete stored run with the same key and judge is
        reused."""
        ...


@dataclass
class Services:
    store: Store
    evals: EvalStore
    records: ImproveStore
    codec: PolicyFiles
    directory: PolicyDirectory
    proposer: propose.Proposer
    evaluate: Evaluate
    feed: FeedSelector  # the feed sample the proposer mines, and the PRs a lesson may cite (with the time cut)
    reviewer_for: Callable[[Policy], Reviewer]  # the self-review pipeline: the policy with its policy-change lens
    split_keys: Callable[[str], list[PRKey]]
    report: ReportSettings
    gate_rules: gate.GateRules
    lesson_rules: lessons.LessonRules
    rules: propose.Rules
    permits: Callable[[str], bool]
    calls_used: Callable[[], int] = lambda: 0


@dataclass
class Attempt:
    record: CandidateRecord
    files: dict[str, str] = field(default_factory=dict)
    run: EvalRun | None = None

    @property
    def delta(self) -> float | None:
        if self.record.gate is None:
            return None
        return self.record.gate.rules[0].values.get("delta_S")


@dataclass
class RoundReport:
    index: int
    incumbent: str
    s_incumbent: float | None
    min_gain: float
    provisional: bool
    notes: list[str]
    generators: list[str]
    attempts: list[Attempt] = field(default_factory=list)
    promoted: str | None = None
    stopped: str | None = None
    calls_used: int = 0
    feed: dict[str, Any] | None = None  # the feed sample: its PR-rounds, composition and run (none: the gate run)

    def to_json(self) -> dict[str, Any]:
        return {
            "round": self.index, "incumbent": self.incumbent, "S_incumbent": self.s_incumbent,
            "min_gain": self.min_gain, "provisional": self.provisional, "notes": self.notes,
            "generators": self.generators, "promoted": self.promoted, "stopped": self.stopped,
            "live_calls_used": self.calls_used, "feed": self.feed,
            "candidates": [_attempt_json(a) for a in self.attempts],
        }  # fmt: skip


def _attempt_json(a: Attempt) -> dict[str, Any]:
    r = a.record
    out: dict[str, Any] = {
        "id": r.id, "generator": r.proposal.generator.value, "hypothesis": r.proposal.hypothesis,
        "change": r.proposal.change, "edit": r.proposal.edit.kind.value, "evidence": list(r.proposal.evidence),
        "policy": r.policy_hash[:12], "pure_removal": r.pure_removal, "outcome": r.outcome.value, "note": r.note,
        "eval_run": r.eval_run,
    }  # fmt: skip
    if r.screen is not None:
        out["screen"] = dict(r.screen)
    if r.self_review is not None:
        out["self_review"] = {"important": r.self_review.important, "passed": r.self_review.passed,
                              "findings": list(r.self_review.findings)}  # fmt: skip
    if r.gate is not None:
        out["gate"] = {"passed": r.gate.passed, "min_gain": r.gate.min_gain, "provisional": r.gate.provisional,
                       "rules": [{"rule": x.rule, "passed": x.passed, "detail": x.detail, **dict(x.values)}
                                 for x in r.gate.rules]}  # fmt: skip
    return out


@dataclass
class LoopReport:
    rounds: list[RoundReport] = field(default_factory=list)
    stop_reason: str = ""
    attempts: int = 0


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class ImproveLoop:
    def __init__(self, services: Services, options: ImproveOptions) -> None:
        self._s = services
        self._o = options
        self._promoter = Promoter(services.records, services.evals, services.directory, services.permits)
        self._run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:4]
        self._streak = 0  # candidates in a row without a promotion

    # ---- the loop ----------------------------------------------------------------------------------------

    def run(self, on_round: Callable[[RoundReport], None] | None = None) -> LoopReport:
        report = LoopReport()
        for index in range(1, self._o.rounds + 1):
            round_report = self._round(index)
            report.rounds.append(round_report)
            report.attempts += len(round_report.attempts)
            if on_round is not None:
                on_round(round_report)
            if round_report.stopped:
                report.stop_reason = f"stopped in round {index}: {round_report.stopped}"
                return report
            s_now = self._s_incumbent()
            target = self._o.target_s
            if target is not None and s_now is not None and s_now >= target and report.attempts >= self._o.min_attempts:
                report.stop_reason = (f"target reached: S {s_now:.4f} >= {target} after {report.attempts} attempts "
                                      f"(minimum {self._o.min_attempts})")  # fmt: skip
                return report
        report.stop_reason = f"{self._o.rounds} rounds done ({report.attempts} attempts)"
        if self._o.target_s is not None:
            report.stop_reason += f"; target S {self._o.target_s} not reached"
        return report

    def _s_incumbent(self) -> float | None:
        try:
            policy = self._s.codec.parse(self._s.directory.files())
            run = self._s.evaluate(policy, self._o.gate_split)
        except (StopRun, LLMError):
            return None
        return eval_report.headline(run.results, self._s.report.params)["S"]

    def schedule(self, index: int, near_misses: int) -> list[GeneratorKind]:
        """The round's generators: the rotation, starting one further each round; on a plateau, starting at the
        least-tried generator, with one slot combining near-misses when there are at least two."""
        gens, n = list(self._o.generators), self._o.candidates_per_round
        if self._streak >= self._o.plateau_rejects:
            tried = Counter(c.proposal.generator for c in self._s.records.candidates())
            start = min(range(len(gens)), key=lambda i: (tried[gens[i]], i))
            out = [gens[(start + k) % len(gens)] for k in range(n)]
            if near_misses >= 2:
                out[-1] = GeneratorKind.COMBINE
            return out
        return [gens[(index - 1 + k) % len(gens)] for k in range(n)]

    def near_misses(self, limit: int = 5) -> list[dict[str, Any]]:
        """Recent candidates that raised S but failed the gate, or passed and lost their round."""
        out = []
        for c in reversed(self._s.records.candidates()):
            if c.outcome not in (Outcome.REJECTED, Outcome.SUPERSEDED, Outcome.UNMEASURED) or c.gate is None:
                continue
            if c.backend != self._o.backend:  # a verdict from another backend's judge doesn't carry over
                continue
            delta = c.gate.rules[0].values.get("delta_S")
            if delta is None or delta <= 0:
                continue
            out.append({"candidate": c.id, "hypothesis": c.proposal.hypothesis, "change": c.proposal.change,
                        "edit": c.proposal.edit.kind.value, "delta_S": delta, "failed": list(c.gate.failed()),
                        "diff": c.diff[:3000]})  # fmt: skip
            if len(out) >= limit:
                break
        return out

    # ---- one round ---------------------------------------------------------------------------------------

    def _round(self, index: int) -> RoundReport:
        s, o = self._s, self._o
        incumbent = s.codec.parse(s.directory.files())
        decisions = s.evals.decisions()
        try:
            min_gain, provisional, note = gate.precondition(
                gate.latest_sensitivity(decisions, s.gate_rules.min_gain_floor),
                floor=s.gate_rules.min_gain_floor, ignore=o.ignore_sensitivity,
            )  # fmt: skip
        except gate.GateRefused as error:
            return RoundReport(index, incumbent.content_hash, None, 0.0, False, [str(error)], [],
                               stopped=f"refused: {error}")  # fmt: skip
        notes = [note]
        if o.offline:
            provisional = True
            notes.append("offline: every stage answered by the local model; promotions are provisional until an "
                         "online run re-scores them with Fable")  # fmt: skip
        if o.feed_split == o.gate_split:
            notes.append(f"the feed and gate splits are both {o.gate_split!r}: the proposer mines the split the gate "
                         "decides on, so gains may not generalize")  # fmt: skip
        items = {k: item for k in s.split_keys(o.feed_split) if (item := s.store.get_pr(k)) is not None}
        sample = None if o.feed_split == o.gate_split else s.feed.sample(items, salt=incumbent.content_hash)
        drawn = None if sample is None else {"split": o.feed_split, "eval_run": None, **sample.to_json()}
        if sample is not None and not sample.keys:
            return RoundReport(index, incumbent.content_hash, None, min_gain, provisional, notes, [], feed=drawn,
                               stopped=f"the feed sample is empty: no PR-round of {o.feed_split!r} has a context pack "
                               f"(`honed pack --split {o.feed_split} --rounds {o.eval_rounds}`)")  # fmt: skip
        try:
            inc_run = s.evaluate(incumbent, o.gate_split)
            feed_run = inc_run if sample is None else s.evaluate(incumbent, o.feed_split, sample.keys,
                                                                 splits.FEED_SPLIT_SUFFIX)  # fmt: skip
        except StopRun as error:
            return RoundReport(index, incumbent.content_hash, None, min_gain, provisional, notes, [], feed=drawn,
                               stopped=f"{type(error).__name__}: {error}")  # fmt: skip
        except KeyboardInterrupt:
            return RoundReport(index, incumbent.content_hash, None, min_gain, provisional, notes, [], feed=drawn,
                               stopped=f"{INTERRUPTED} while evaluating the incumbent")  # fmt: skip
        if drawn is not None:
            drawn["eval_run"] = feed_run.id
        if inc_run.stopped or feed_run.stopped:
            why = inc_run.stopped or feed_run.stopped
            return RoundReport(index, incumbent.content_hash, None, min_gain, provisional, notes, [], feed=drawn,
                               stopped=f"the incumbent's evaluation stopped: {why}")  # fmt: skip
        s_inc = eval_report.headline(inc_run.results, s.report.params)["S"]
        near = self.near_misses()
        generators = self.schedule(index, len(near))
        report = RoundReport(index, incumbent.content_hash, s_inc, min_gain, provisional, notes,
                             [g.value for g in generators], feed=drawn)  # fmt: skip
        if sample is not None:
            report.notes.append(f"feed: {sample.summary()}, sampled from {o.feed_split!r} for incumbent "
                                f"{incumbent.content_hash[:12]} (run {feed_run.id})")  # fmt: skip

        diagnostics = eval_report.report(feed_run, s.report)
        cases = propose.failure_cases(feed_run, items, s.report.params, o.max_failure_cases)
        user = propose.context(incumbent, {"metrics": diagnostics["metrics"], "counts": diagnostics["counts"],
                                           "diagnostics": diagnostics["diagnostics"]}, cases, decisions, s.rules,
                               near_misses=near if GeneratorKind.COMBINE in generators else ())  # fmt: skip
        proposals = self._propose(user, generators, index, report)
        if report.stopped:  # proposals that came back are recorded, not evaluated
            for n, item in enumerate(proposals, 1):
                report.attempts.append(item if isinstance(item, Attempt) else Attempt(CandidateRecord(
                    id=f"{self._run_id}-r{index}c{n}", round=index, proposal=item, parent_hash=incumbent.content_hash,
                    policy_hash="", diff="", outcome=Outcome.INCOMPLETE, note=f"not evaluated: {report.stopped}",
                    created_at=_now())))  # fmt: skip
            self._finish(report, incumbent)
            return report
        feed = s.feed.evidence(items)
        rejected = {c.policy_hash for c in s.records.candidates() if c.backend == o.backend and c.policy_hash
                    and c.outcome in (Outcome.REJECTED, Outcome.SELF_REVIEW, Outcome.SCREENED_OUT)}  # fmt: skip
        screen = self.screen_keys(incumbent, index)
        if screen:
            report.notes.append(f"screen: {len(screen)} of {len(s.split_keys(o.gate_split))} PRs of the gate split "
                                f"({s.gate_rules.screen_fraction:.0%} of each language)")  # fmt: skip
        for n, item in enumerate(proposals, 1):
            cid = f"{self._run_id}-r{index}c{n}"
            if isinstance(item, Attempt):
                report.attempts.append(item)
                continue
            try:
                attempt = self._candidate(cid, index, item, incumbent, inc_run, feed, rejected, min_gain, provisional,
                                          screen)  # fmt: skip
            except KeyboardInterrupt:  # between the stages that catch it themselves
                attempt = Attempt(CandidateRecord(
                    id=cid, round=index, proposal=item, parent_hash=incumbent.content_hash, policy_hash="", diff="",
                    outcome=Outcome.INCOMPLETE, note=INTERRUPTED, created_at=_now()))  # fmt: skip
            report.attempts.append(attempt)
            if attempt.record.outcome is Outcome.INCOMPLETE:
                report.stopped = attempt.record.note
                break
        self._finish(report, incumbent)
        return report

    def screen_keys(self, incumbent: Policy, index: int) -> list[PRKey]:
        """The round's screen subset of the gate split (none when `screen_fraction` is 0 or 1): fixed for the round,
        so every candidate is screened on the same PRs, and drawn afresh each round."""
        fraction = self._s.gate_rules.screen_fraction
        if not 0 < fraction < 1:
            return []
        languages = {k: item.language for k in self._s.split_keys(self._o.gate_split)
                     if (item := self._s.store.get_pr(k)) is not None}  # fmt: skip
        return splits.screen_keys(languages, fraction, salt=f"{incumbent.content_hash}:{index}")

    def _propose(self, user: str, generators: Sequence[GeneratorKind], index: int,
                 report: RoundReport) -> list[Proposal | Attempt]:  # fmt: skip
        """One proposal per generator, asked in parallel; a failed one becomes an invalid attempt."""
        out: list[Proposal | Attempt | None] = [None] * len(generators)
        lock = threading.Lock()
        seen: Counter[GeneratorKind] = Counter()
        jobs = []
        for n, generator in enumerate(generators):
            sample = seen[generator]
            seen[generator] += 1

            def run(n: int = n, generator: GeneratorKind = generator, sample: int = sample) -> None:
                proposal = self._s.proposer.propose(user, generator, sample=sample)
                with lock:
                    out[n] = proposal

            jobs.append(Job(f"propose:{n}:{generator.value}", "propose", run))
        try:
            result = run_jobs(jobs, concurrency=len(jobs), stop_on=STOP_ON)
        except KeyboardInterrupt:  # the proposals already back are kept; the others were not made
            result = RunReport(total=len(jobs), stopped=INTERRUPTED)
        if result.stopped:
            report.stopped = f"proposing: {result.stopped}"
        final: list[Proposal | Attempt] = []
        for n, generator in enumerate(generators):
            value = out[n]
            if value is not None:
                final.append(value)
                continue
            error = result.failed.get(f"propose:{n}:{generator.value}")
            if error is None:
                continue  # not run: the round stopped
            placeholder = Proposal(generator, "(no proposal)", "(none)", _NO_EDIT)
            record = CandidateRecord(id=f"{self._run_id}-r{index}c{n + 1}", round=index, proposal=placeholder,
                                     parent_hash="", policy_hash="", diff="", outcome=Outcome.INVALID,
                                     note=f"the proposer gave no usable proposal: {error[:300]}",
                                     created_at=_now())  # fmt: skip
            final.append(Attempt(record))
        return final

    def _candidate(self, cid: str, index: int, proposal: Proposal, incumbent: Policy, inc_run: EvalRun,
                   feed: dict[PRKey, lessons.EvidencePR], rejected: set[str], min_gain: float,
                   provisional: bool, screen: Sequence[PRKey] = ()) -> Attempt:  # fmt: skip
        s, o = self._s, self._o
        base = CandidateRecord(id=cid, round=index, proposal=proposal, parent_hash=incumbent.content_hash,
                               policy_hash="", diff="", outcome=Outcome.INVALID, created_at=_now())  # fmt: skip
        try:
            files, policy = policy_edit.apply(incumbent, proposal.edit, s.codec)
        except policy_edit.EditError as error:
            return Attempt(replace(base, note=f"invalid edit: {error}"))
        diff = policy_diff(incumbent.files, files, o.prefix)
        base = replace(base, policy_hash=policy.content_hash, diff=diff,
                       pure_removal=is_pure_removal(incumbent.files, files, CONFIG_FILE))  # fmt: skip
        refused = refused_paths(diff, s.permits)
        if refused:
            return Attempt(replace(base, outcome=Outcome.REFUSED, note=f"touches paths it may not: {refused}"), files)
        problems = lessons.problems(proposal, policy, incumbent, feed, s.lesson_rules)
        if problems:
            return Attempt(replace(base, note="lesson acceptance: " + "; ".join(problems)), files)
        if policy.content_hash in rejected:
            return Attempt(replace(base, outcome=Outcome.REPEAT,
                                   note="the same policy as a candidate already rejected"), files)  # fmt: skip
        try:
            pr = selfreview.synthetic_pr(
                proposal,
                incumbent.files,
                files,
                parent_hash=incumbent.content_hash,
                candidate_hash=policy.content_hash,
                prefix=o.prefix,
                language=o.language,
            )
            review = selfreview.self_review(s.reviewer_for(incumbent), pr)
        except StopRun as error:
            return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"self-review stopped: {error}"), files)
        except KeyboardInterrupt:
            return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"self-review {INTERRUPTED}"), files)
        except LLMError as error:
            return Attempt(replace(base, note=f"self-review failed: {error}"), files)
        base = replace(base, self_review=review)
        if not review.passed:  # the titles reach the decision log, so the proposer learns what was wrong
            titles = "; ".join(f["title"] for f in review.findings
                               if f["severity"] == "important" and f["bucket"] in ("act_on", "consider"))  # fmt: skip
            note = f"the incumbent posted {review.important} Important findings: {titles[:400]}"
            return Attempt(replace(base, outcome=Outcome.SELF_REVIEW, note=note), files)
        if screen:
            try:
                screened = s.evaluate(policy, o.gate_split, screen)
            except StopRun as error:
                return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"screen stopped: {error}"), files)
            except KeyboardInterrupt:
                return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"screen {INTERRUPTED}"), files)
            if screened.stopped:
                return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"screen stopped: {screened.stopped}"),
                               files)  # fmt: skip
            numbers = gate.screen(screened, inc_run, screen, s.report.params)
            base = replace(base, screen=numbers)
            if not numbers["passed"]:
                missing = numbers["missing_pr_rounds"]
                return Attempt(replace(base, outcome=Outcome.SCREENED_OUT, note=(
                    f"screen delta-S {numbers['delta_S']} on {numbers['pr_rounds']} PR-rounds of {numbers['prs']} "
                    "PRs: not above 0" + (f"; {missing} PR-rounds missing" if missing else ""))), files)  # fmt: skip
        try:
            run = s.evaluate(policy, o.gate_split)
        except StopRun as error:
            return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"evaluation stopped: {error}"), files)
        except KeyboardInterrupt:
            return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"evaluation {INTERRUPTED}"), files)
        base = replace(base, eval_run=run.id)
        if run.stopped:
            return Attempt(replace(base, outcome=Outcome.INCOMPLETE, note=f"evaluation stopped: {run.stopped}"),
                           files, run)  # fmt: skip
        exposure = None
        if base.pure_removal:
            exposure = gate.removal_exposure(incumbent, policy, inc_run, {(r.pr, r.round) for r in run.results})
        verdict = gate.judge(
            run,
            inc_run,
            report=s.report,
            rules=s.gate_rules,
            min_gain=min_gain,
            policy=policy,
            self_review=review,
            pure_removal=base.pure_removal,
            provisional=provisional,
            exposure=exposure,
        )
        if verdict.passed:
            outcome, note = Outcome.PROMOTED, "passed every rule"  # PROMOTED is provisional until _finish
        elif verdict.unmeasured and verdict.failed() == ("real_gain",):
            outcome, note = Outcome.UNMEASURED, "unmeasured: " + verdict.rules[0].detail
        else:
            outcome, note = Outcome.REJECTED, "failed: " + ", ".join(verdict.failed())
        return Attempt(replace(base, gate=verdict, outcome=outcome, note=note), files, run)

    def _finish(self, report: RoundReport, incumbent: Policy) -> None:
        """Promote the round's best passing candidate; record every candidate and its decision-log row."""
        passing = [a for a in report.attempts if a.record.outcome is Outcome.PROMOTED]
        winner = max(passing, key=lambda a: (a.delta or 0.0, a.record.pure_removal), default=None)
        for attempt in passing:
            if attempt is not winner:
                attempt.record = replace(
                    attempt.record,
                    outcome=Outcome.SUPERSEDED,
                    note=f"passed the gate; {winner.record.id if winner else '?'} won the round",
                )
        if winner is not None and winner.run is not None and winner.record.gate is not None:
            provisional = report.provisional or winner.record.gate.provisional
            try:
                self._promoter.promote(winner.record, incumbent.files, winner.files, winner.run,
                                       IncumbentKey(self._o.gate_split, self._o.backend, self._o.eval_rounds),
                                       winner.record.gate, provisional=provisional)  # fmt: skip
                report.promoted = winner.record.policy_hash
                note = "promoted" + (" (PROVISIONAL: " + "; ".join(report.notes) + ")" if provisional else "")
                winner.record = replace(winner.record, note=note)
            except PromotionRefused as error:  # refused before anything is written
                winner.record = replace(winner.record, outcome=Outcome.REFUSED, note=f"promotion refused: {error}")
        for attempt in report.attempts:
            attempt.record = replace(attempt.record, backend=self._o.backend)
            self._s.records.save_candidate(attempt.record)
            self._s.evals.add_decision(self._decision(attempt, report))
            if attempt.record.outcome is Outcome.PROMOTED:
                self._streak = 0
            elif attempt.record.outcome is not Outcome.INCOMPLETE:
                self._streak += 1
        report.calls_used = self._s.calls_used()

    def _decision(self, attempt: Attempt, report: RoundReport) -> Decision:
        r = attempt.record
        g = r.gate
        values = g.rules[0].values if g is not None and g.rules else {}
        before = f"S={values.get('S_incumbent', report.s_incumbent)}"
        after = f"S={values['S_candidate']}" if "S_candidate" in values else "not evaluated"
        delta = (f"{values['delta_S']:+.4f} [{values['ci_low']:+.4f}, {values['ci_high']:+.4f}]"
                 if values.get("delta_S") is not None else "")  # fmt: skip
        if g is None and r.screen is not None:
            before, after = f"screen S={r.screen['S_incumbent']}", f"screen S={r.screen['S_candidate']}"
            delta = f"{r.screen['delta_S']} (screen, {r.screen['pr_rounds']} PR-rounds)"
        if g is not None:
            gate_json = gate.verdict_json(g)
        else:
            gate_json = json.dumps({**({"self_review": r.self_review.important} if r.self_review else {}),
                                    **({"screen": dict(r.screen)} if r.screen is not None else {})})  # fmt: skip
        verdict = r.outcome.value + (f": {', '.join(g.failed())}" if g is not None and g.failed() else "")
        notes = [r.note, f"candidate {r.id}, round {r.round}, generator {r.proposal.generator.value}",
                 f"policy {r.policy_hash[:12] or '-'} from {r.parent_hash[:12] or '-'}"]  # fmt: skip
        if report.provisional:
            notes.append("PROVISIONAL: " + "; ".join(report.notes))
        return Decision(None, _now(), hypothesis=r.proposal.hypothesis,
                        change=f"[{r.proposal.generator.value}/{r.proposal.edit.kind.value}] {r.proposal.change}",
                        before=before, after=after, delta=delta, gate=gate_json, verdict=verdict,
                        note=" | ".join(n for n in notes if n))  # fmt: skip


_NO_EDIT = PolicyEdit(kind=EditKind.CONFIG_SET)
