"""The gate (METRICS.md section 3): every rule a candidate must pass on the gate split before it is promoted, each
reported with the numbers it compared. Pure over stored runs and settings.

Precondition: the latest sensitivity check in the decision log says the eval separates a weakened policy from the
incumbent (`sensitivity`); otherwise the gate refuses to run. `--ignore-sensitivity` overrides that with a warning,
and anything promoted under the override is provisional. `min_gain` comes from the latest noise-floor row of the log
(max(floor, 2 sigma)), never from `honed.toml`.

Conventions the rules need and METRICS.md leaves open:
- Both runs must score the same PR-rounds: a candidate whose review failed on a PR-round the incumbent scored fails
  the real-gain rule (a paired comparison on fewer rounds could hide its failures).
- Per-language floors apply to languages with at least `min_prs_per_language` distinct PRs in the comparison.
- Well-formed output, two rates that must each reach `well_formed_min`: the parse rate, proposals that parsed over
  proposals that parsed plus malformed ones (`ReviewResult.proposed`, or the review's findings in runs stored before
  it); and the anchor rate, posted Important and Nit findings that anchor to a changed hunk over all of them
  (Pre-existing findings sit outside the diff by design). Each is 1.0 with nothing to count.
- Policy size counts active lessons (every confidence but `retired`) and `Policy.prompt_tokens`.
- The pure-removal exception needs exposure (`removal_exposure`): every lesson the candidate removes or shortens must
  have fired (cited by a finding, or a check's finding) on at least `removal_min_exposure` PR-rounds of the incumbent's
  run on the compared PR-rounds. Prompt passages and settings carry no citation, so removing one is never measured: a
  removal touching a prompt or `config.toml` is `unmeasured` unless it wins on score.
- Screening (a cost lever, not a rule): `screen` compares a candidate's run on a fixed, language-stratified share of
  the gate split (`learn/splits.py` `screen_keys`) with the incumbent's stored full run on the same PR-rounds; only
  a delta-S above 0, with no PR-round missing, goes on to the full split.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from honed.core import scoring
from honed.core.evals import Decision, EvalRun, RoundRecord
from honed.core.improve import GateVerdict, RuleResult, SelfReview
from honed.core.policy import LESSONS_FILE, Policy
from honed.core.types import PRKey, Severity
from honed.learn import eval_report
from honed.learn.eval_report import ReportSettings

log = logging.getLogger(__name__)

NOISE_FLOOR_VERDICT = "noise floor measured"
SEPARATES = "separates"


@dataclass(frozen=True)
class GateRules:
    """`[gate]` (METRICS.md section 8)."""

    min_gain_floor: float
    language_tolerance: float
    important_recall_tolerance: float
    min_prs_per_language: int
    clean_pr_alarm_rise_pp: float
    cost_cap_usd: float
    cost_growth_max: float
    latency_p90_max_s: float
    policy_max_lessons: int
    policy_max_prompt_tokens: int
    well_formed_min: float = 0.99
    removal_min_exposure: int = 5  # PR-rounds the removed content fired on, for the pure-removal exception
    screen_fraction: float = 0.0  # of the gate split, replayed first (0: no screening)


@dataclass(frozen=True)
class Sensitivity:
    separates: bool
    min_gain: float
    sigma: float | None
    noise_row: int | None  # decision-log ids the numbers come from
    separation_row: int | None


class GateRefused(RuntimeError):
    """The sensitivity precondition doesn't hold, so the gate won't judge anything."""


def latest_sensitivity(decisions: Sequence[Decision], floor: float) -> Sensitivity | None:
    """The newest noise-floor and separation rows of the decision log (`honed eval --sensitivity` writes them)."""
    noise = next((d for d in reversed(decisions) if d.verdict == NOISE_FLOOR_VERDICT), None)
    separation = next((d for d in reversed(decisions) if d.hypothesis.startswith("The eval separates")), None)
    if noise is None and separation is None:
        return None
    numbers = json.loads(noise.gate) if noise is not None else {}
    return Sensitivity(
        separates=separation is not None and separation.verdict == SEPARATES,
        min_gain=max(floor, float(numbers.get("min_gain", floor))), sigma=numbers.get("sigma"),
        noise_row=noise.id if noise else None, separation_row=separation.id if separation else None,
    )  # fmt: skip


def precondition(sensitivity: Sensitivity | None, *, floor: float, ignore: bool) -> tuple[float, bool, str]:
    """(min_gain, provisional, note). Raises `GateRefused` when the eval hasn't shown it separates, unless `ignore`."""
    if sensitivity is not None and sensitivity.separates:
        return sensitivity.min_gain, False, f"sensitivity check passed (decision {sensitivity.separation_row})"
    why = ("no sensitivity check in the decision log" if sensitivity is None else
           f"the latest sensitivity check (decision {sensitivity.separation_row}) does not separate the weakened "
           "policy from the incumbent")  # fmt: skip
    if not ignore:
        raise GateRefused(f"the gate's precondition fails: {why}. Run `honed eval --sensitivity`, or pass "
                          "--ignore-sensitivity to exercise the loop with every promotion provisional.")  # fmt: skip
    note = (f"WARNING: --ignore-sensitivity: {why}. The eval is not shown to tell good from bad, so gate verdicts are "
            "not trustworthy and every promotion is provisional.")  # fmt: skip
    log.warning(note)
    return (sensitivity.min_gain if sensitivity else floor), True, note


@dataclass(frozen=True)
class WellFormed:
    parsed: int
    malformed: int
    anchorable: int  # posted Important and Nit findings
    anchored: int

    @property
    def parse_rate(self) -> float:
        total = self.parsed + self.malformed
        return self.parsed / total if total else 1.0

    @property
    def anchor_rate(self) -> float:
        return self.anchored / self.anchorable if self.anchorable else 1.0


def well_formed(run: EvalRun) -> WellFormed:
    parsed = malformed = anchorable = anchored = 0
    for r in run.records:
        parsed += r.review.proposed or len(r.review.findings)
        malformed += r.review.malformed
        kept = [f for f in r.result.findings if f.severity is not Severity.PRE_EXISTING]
        anchorable += len(kept)
        if r.anchored_ids or not r.anchored:
            ids = set(r.anchored_ids)
            anchored += sum(f.id in ids for f in kept)
        else:  # stored before anchored_ids: only a count over every posted finding
            anchored += min(r.anchored, len(kept))
    return WellFormed(parsed, malformed, anchorable, anchored)


@dataclass(frozen=True)
class Exposure:
    """How often the gate split exercised what a pure removal deletes (METRICS.md section 3, rule 1)."""

    measurable: bool
    fired: Mapping[str, int] = field(default_factory=dict)  # lesson id -> PR-rounds it fired on
    why: str = ""

    def enough(self, minimum: int) -> bool:
        return self.measurable and bool(self.fired) and min(self.fired.values()) >= minimum


def fired(record: RoundRecord, lesson_id: str) -> bool:
    """A lesson fired on a PR-round: a finding (posted or not) cites it, or its check raised one."""
    return any(lesson_id in f.lessons_cited or f"check:{lesson_id}" in f.raised_by for f in record.review.findings)


def removal_exposure(incumbent: Policy, candidate: Policy, run: EvalRun,
                     units: Collection[tuple[PRKey, int]] | None = None) -> Exposure:  # fmt: skip
    """Exposure of the content `candidate` removes from `incumbent`, on `run` (the incumbent's run on the gate split,
    restricted to `units` when given)."""
    changed = sorted(p for p in set(incumbent.files) | set(candidate.files)
                     if incumbent.files.get(p) != candidate.files.get(p))  # fmt: skip
    other = [p for p in changed if p != LESSONS_FILE]
    if other:
        return Exposure(False, why=f"it changes {', '.join(other)}: prompt passages and settings carry no citations, "
                                   "so removing them is never measured")  # fmt: skip
    kept = {lesson.id: lesson for lesson in candidate.lessons}
    touched = [lesson.id for lesson in incumbent.lessons if kept.get(lesson.id) != lesson]
    if not touched:
        return Exposure(False, why="no lesson is removed or changed")
    records = [r for r in run.records if units is None or (r.result.pr, r.result.round) in units]
    counts = {lesson_id: sum(fired(r, lesson_id) for r in records) for lesson_id in touched}
    return Exposure(True, counts)


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def screen(candidate: EvalRun, incumbent: EvalRun, keys: Collection[PRKey], params: scoring.ScoringParams,
           ) -> dict[str, Any]:  # fmt: skip
    """The screen (METRICS.md section 3): the candidate's run on the screen PRs against the incumbent's full run on the
    same PR-rounds. It passes with a delta-S above 0 and no PR-round of the incumbent's missing."""
    wanted = set(keys)
    cand = {(r.pr, r.round): r for r in candidate.results if r.pr in wanted}
    inc = {(r.pr, r.round): r for r in incumbent.results if r.pr in wanted}
    common = sorted(cand.keys() & inc.keys(), key=lambda u: (u[0].repo, u[0].number, u[1]))
    s_cand = scoring.headline([cand[u] for u in common], params) if common else None
    s_inc = scoring.headline([inc[u] for u in common], params) if common else None
    delta = None if s_cand is None or s_inc is None else s_cand - s_inc
    missing = len(inc.keys() - cand.keys())
    return {"run": candidate.id, "prs": len(wanted), "pr_rounds": len(common), "missing_pr_rounds": missing,
            "S_candidate": _r(s_cand), "S_incumbent": _r(s_inc), "delta_S": _r(delta),
            "passed": delta is not None and delta > 0 and missing == 0}  # fmt: skip


def judge(candidate: EvalRun, incumbent: EvalRun, *, report: ReportSettings, rules: GateRules, min_gain: float,
          policy: Policy, self_review: SelfReview, pure_removal: bool, provisional: bool,
          exposure: Exposure | None = None) -> GateVerdict:  # fmt: skip
    """Every rule of METRICS.md section 3 for `candidate` against `incumbent` (both on the gate split). `exposure`:
    what a pure removal deletes was exercised (`removal_exposure`); without it a removal is unmeasured."""
    params = report.params
    boot, paired = eval_report.paired(candidate, incumbent, report)
    cand = {(r.pr, r.round): r for r in candidate.results}
    inc = {(r.pr, r.round): r for r in incumbent.results}
    common = sorted(cand.keys() & inc.keys(), key=lambda u: (u[0].repo, u[0].number, u[1]))
    c_res, i_res = [cand[u] for u in common], [inc[u] for u in common]
    missing = len(inc.keys() - cand.keys())
    out: list[RuleResult] = []

    gain = boot.delta >= min_gain and boot.low > 0
    holds = pure_removal and boot.low >= -min_gain
    exposed = exposure is not None and exposure.enough(rules.removal_min_exposure)
    removal = holds and exposed
    unmeasured = holds and not exposed and not gain and missing == 0
    passed = (gain or removal) and missing == 0
    numbers = f"delta-S {boot.delta:+.4f} vs min_gain {min_gain:.4f}, interval [{boot.low:+.4f}, {boot.high:+.4f}]"
    shown = dict(exposure.fired) if exposure is not None else {}
    if removal and not gain:
        why = (f"pure removal: the interval's lower bound holds above -min_gain ({numbers}), and the removed content "
               f"fired on {shown} PR-rounds (min {rules.removal_min_exposure})")  # fmt: skip
    elif unmeasured:
        why = (f"unmeasured: a pure removal whose interval holds above -min_gain ({numbers}), but "
               + (exposure.why if exposure is not None and exposure.why else
                  f"the removed content fired on {shown or 'no'} PR-rounds (min {rules.removal_min_exposure})")
               + "; not harmless, just not exercised")  # fmt: skip
    else:
        why = numbers
    if missing:
        why += f"; the candidate lacks {missing} PR-rounds the incumbent scored"
    out.append(RuleResult("real_gain", passed, why, {
        "delta_S": _r(boot.delta), "ci_low": _r(boot.low), "ci_high": _r(boot.high), "min_gain": _r(min_gain),
        "S_candidate": paired["S_candidate"], "S_incumbent": paired["S_incumbent"], "pure_removal": pure_removal,
        "pr_rounds": len(common), "missing_pr_rounds": missing,
        **({"removal_exposure": shown, "removal_min_exposure": rules.removal_min_exposure, "unmeasured": unmeasured}
           if pure_removal else {}),
    }))  # fmt: skip

    c_lang, i_lang = scoring.language_scores(c_res, params), scoring.language_scores(i_res, params)
    checked, failing = {}, []
    for lang in sorted(i_lang):
        prs = len({r.pr for r in i_res if r.language == lang})
        if prs < rules.min_prs_per_language or lang not in c_lang:
            continue
        floor = i_lang[lang].f - rules.language_tolerance
        checked[lang] = {"F_candidate": _r(c_lang[lang].f), "F_incumbent": _r(i_lang[lang].f), "prs": prs}
        if c_lang[lang].f < floor:
            failing.append(lang)
    skipped = sorted(set(i_lang) - set(checked))
    out.append(RuleResult("language_floors", not failing,
                          (f"F_L below incumbent - {rules.language_tolerance}: {failing}" if failing else
                           f"no language regresses beyond {rules.language_tolerance}")
                          + (f"; not applied (under {rules.min_prs_per_language} PRs): {skipped}" if skipped else ""),
                          {"checked": checked, "skipped": skipped}))  # fmt: skip

    c_rimp, i_rimp = scoring.important_recall(c_res, params), scoring.important_recall(i_res, params)
    out.append(RuleResult("important_recall", c_rimp >= i_rimp - rules.important_recall_tolerance,
                          f"R_imp {c_rimp:.4f} vs incumbent {i_rimp:.4f} - {rules.important_recall_tolerance}",
                          {"candidate": _r(c_rimp), "incumbent": _r(i_rimp)}))  # fmt: skip

    c_alarm, i_alarm = scoring.clean_pr_alarm_rate(c_res), scoring.clean_pr_alarm_rate(i_res)
    rise = 100 * (c_alarm - i_alarm)
    out.append(RuleResult("clean_pr_alarms", rise <= rules.clean_pr_alarm_rise_pp,
                          f"clean PR-rounds with an Important finding rose {rise:+.2f} pp (limit "
                          f"{rules.clean_pr_alarm_rise_pp})",
                          {"candidate": _r(c_alarm), "incumbent": _r(i_alarm), "rise_pp": _r(rise, 2)}))  # fmt: skip

    c_head, i_head = eval_report.headline(c_res, params), eval_report.headline(i_res, params)
    c_cost, i_cost = c_head["mean_cost_usd"] or 0.0, i_head["mean_cost_usd"] or 0.0
    growth_ok = c_cost <= rules.cost_growth_max * i_cost or boot.delta >= 2 * min_gain
    out.append(RuleResult("cost", c_cost <= rules.cost_cap_usd and growth_ok,
                          f"mean ${c_cost:.4f} per review (cap ${rules.cost_cap_usd}), incumbent ${i_cost:.4f} "
                          f"(at most x{rules.cost_growth_max} unless delta-S >= 2 x min_gain)",
                          {"candidate_usd": _r(c_cost), "incumbent_usd": _r(i_cost),
                           "growth": _r(c_cost / i_cost if i_cost else None)}))  # fmt: skip

    p90 = c_head["latency_p90_s"] or 0.0
    out.append(RuleResult("latency", p90 <= rules.latency_p90_max_s,
                          f"p90 review time {p90}s (limit {rules.latency_p90_max_s}s)",
                          {"p90_s": p90, "incumbent_p90_s": i_head["latency_p90_s"]}))  # fmt: skip

    lessons, tokens = len(policy.active_lessons), policy.prompt_tokens()
    small = lessons <= rules.policy_max_lessons and tokens <= rules.policy_max_prompt_tokens
    out.append(RuleResult("policy_size", small,
                          f"{lessons} active lessons (max {rules.policy_max_lessons}), {tokens} prompt tokens "
                          f"(max {rules.policy_max_prompt_tokens})",
                          {"lessons": lessons, "prompt_tokens": tokens}))  # fmt: skip

    wf, i_wf = well_formed(candidate), well_formed(incumbent)
    ok = wf.parse_rate >= rules.well_formed_min and wf.anchor_rate >= rules.well_formed_min
    out.append(RuleResult("well_formed", ok,
                          f"parse rate {wf.parse_rate:.4f} ({wf.parsed} parsed, {wf.malformed} malformed); anchor rate "
                          f"{wf.anchor_rate:.4f} ({wf.anchored} of {wf.anchorable} Important and Nit findings); min "
                          f"{rules.well_formed_min} each (incumbent {i_wf.parse_rate:.4f} and {i_wf.anchor_rate:.4f})",
                          {"parse_rate": _r(wf.parse_rate), "anchor_rate": _r(wf.anchor_rate),
                           "incumbent_parse_rate": _r(i_wf.parse_rate),
                           "incumbent_anchor_rate": _r(i_wf.anchor_rate)}))  # fmt: skip

    out.append(RuleResult("self_review", self_review.passed,
                          f"the incumbent posted {self_review.important} Important findings on the candidate's diff",
                          {"important": self_review.important}))  # fmt: skip
    return GateVerdict(all(r.passed for r in out), tuple(out), min_gain, provisional, unmeasured=unmeasured)


def verdict_json(verdict: GateVerdict) -> str:
    """The decision log's `gate` column."""
    return json.dumps({"passed": verdict.passed, "min_gain": verdict.min_gain, "provisional": verdict.provisional,
                       "unmeasured": verdict.unmeasured,
                       "rules": {r.rule: {"passed": r.passed, **r.values} for r in verdict.rules}},
                      default=str)  # fmt: skip
