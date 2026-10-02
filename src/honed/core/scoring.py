"""Scoring, exactly as METRICS.md sections 1, 2 and 6 define it. Every weight is a parameter; nothing has a default.

Valid unlabeled (VU) findings (METRICS.md section 1): only one claimed Important that the judge independently rates
Important (`Match.judged_severity`) and the verifier traced (evidence level >= `vu_min_evidence`) earns credit,
`c_vu` x w(Important). One claimed Important that the judge rates a Nit is severity inflation: an FP at Nit weight. A
VU Nit is neutral, up to `max_neutral_vu_per_round` per PR-round; each one beyond that costs its claimed weight like
an FP. Each finding's part in the sums is decided once, by `score_findings`, and every sum is taken from it.

Conventions METRICS.md leaves open are fixed here and apply everywhere:
- precision with no findings is 1.0 (nothing wrong was claimed); recall with no gold issues is 1.0 (nothing to miss);
- F-beta is 0.0 when precision and recall are both 0;
- S averages over the language groups present in the results, renormalizing their weights `pi_L`;
- a second TP on an already-matched gold issue counts as a DUP (the judge matches one-to-one);
- a VU Pre-existing finding is neutral and doesn't count toward the VU Nit cap (the METRICS.md formula leaves it out of
  both VUw and FPw);
- a VU claimed Important that earns no credit and isn't inflated (the judge rates it Important but the evidence level
  is below `vu_min_evidence`, or the run predates judged severities) is neutral and outside the Nit cap: the judge
  agrees it is a real Important issue, so it costs nothing, but nothing traced it, so it earns nothing;
- the VU Nit cap takes a PR-round's VU Nits in the order the review posted them; the rest are the ones beyond it (they
  all weigh the same, so the order only decides which findings a diagnostic subset holds);
- a PR replayed over several review rounds contributes one result per round; the bootstrap resamples whole PRs,
  each bringing all of its rounds (ARCHITECTURE.md section 6).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from honed.core.types import EvalResult, Finding, FindingClass, GoldIssue, PRKey, Severity


@dataclass(frozen=True)
class ScoringParams:
    beta: float
    severity_weights: Mapping[Severity, float]
    valid_unlabeled_credit: float  # c_vu, for VU findings claimed Important only
    language_weights: Mapping[str, float]  # pi_L
    max_neutral_vu_per_round: int  # VU Nits per PR-round that are neutral; each one beyond costs like an FP
    vu_min_evidence: int  # the verifier's evidence level a VU Important needs to earn c_vu


class Credit(StrEnum):
    """A posted finding's part in the weighted sums (METRICS.md section 1)."""

    TP = "tp"  # earns its gold issue's weight x conf
    VU = "vu"  # a valid unlabeled Important the judge rates Important, traced: earns c_vu x w(Important)
    NEUTRAL = "neutral"  # a valid unlabeled Nit within the per-round cap, a valid unlabeled Pre-existing, or an
    # untraced valid unlabeled Important the judge rates Important
    FP = "fp"  # an FP, a DUP, a valid unlabeled Nit beyond the cap (claimed weight), or a valid unlabeled Important the
    # judge rates a Nit (Nit weight)


@dataclass(frozen=True)
class ScoredFinding:
    finding: Finding
    klass: FindingClass  # the judge's class; a second TP on the same gold issue becomes DUP
    credit: Credit
    weight: float  # what it earns (TP, VU) or costs (FP); 0 when neutral
    gold: GoldIssue | None = None  # the gold issue a TP matched

    @property
    def over_cap(self) -> bool:
        """A valid unlabeled Nit beyond the per-round cap."""
        unvalued = self.klass is FindingClass.VU and self.credit is Credit.FP
        return unvalued and self.finding.severity is not Severity.IMPORTANT

    @property
    def inflated(self) -> bool:
        """A valid unlabeled finding claimed Important that the judge rates a Nit (scored as an FP at Nit weight)."""
        unvalued = self.klass is FindingClass.VU and self.credit is Credit.FP
        return unvalued and self.finding.severity is Severity.IMPORTANT


@dataclass(frozen=True)
class Tally:
    """Weighted sums for a set of PRs (METRICS.md section 1)."""

    tp: float = 0.0  # TPw
    vu: float = 0.0  # VUw: c_vu x w(Important) per VU finding claimed and judged Important, traced
    fp: float = 0.0  # FPw: FPs, DUPs, VU Nits beyond the per-round cap, and inflated VU Importants at Nit weight
    gold: float = 0.0  # Gw
    tp_important: float = 0.0  # TPw restricted to Important gold issues
    gold_important: float = 0.0  # Gw restricted to Important gold issues

    def __add__(self, other: Tally) -> Tally:
        return Tally(
            self.tp + other.tp,
            self.vu + other.vu,
            self.fp + other.fp,
            self.gold + other.gold,
            self.tp_important + other.tp_important,
            self.gold_important + other.gold_important,
        )


@dataclass(frozen=True)
class LanguageScore:
    language: str
    prs: int
    precision: float
    recall: float
    f: float


@dataclass(frozen=True)
class BootstrapResult:
    delta: float  # S(candidate) - S(incumbent) on the full set
    low: float  # lower bound of the confidence interval of delta
    high: float
    resamples: int
    sd: float = 0.0  # standard deviation of delta over the resamples


def _gold_weight(gold: GoldIssue, params: ScoringParams) -> float:
    return params.severity_weights[gold.severity] * gold.conf


def score_findings(result: EvalResult, params: ScoringParams) -> list[ScoredFinding]:
    """Each posted finding's part in one PR-round's sums, in the order the review posted them. Every finding needs
    exactly one `Match`."""
    gold = {g.id: g for g in result.gold}
    verdicts = {m.finding_id: m for m in result.matches}
    matched: set[str] = set()
    neutral_nits = 0
    out = []
    for finding in result.findings:
        match = verdicts.get(finding.id)
        if match is None:
            raise ValueError(f"{result.pr}: finding {finding.id} has no match verdict")
        claimed = params.severity_weights[finding.severity]
        klass = match.klass
        if klass is FindingClass.TP:
            target = gold.get(match.gold_id or "")
            if target is None:
                raise ValueError(f"{result.pr}: TP {finding.id} matches unknown gold issue {match.gold_id}")
            if target.id not in matched:
                matched.add(target.id)
                out.append(ScoredFinding(finding, klass, Credit.TP, _gold_weight(target, params), target))
                continue
            klass = FindingClass.DUP
        if klass is FindingClass.VU:
            if finding.severity is Severity.IMPORTANT:
                out.append(_vu_important(finding, match.judged_severity, params))
            elif finding.severity is Severity.NIT and neutral_nits >= params.max_neutral_vu_per_round:
                out.append(ScoredFinding(finding, klass, Credit.FP, claimed))
            else:
                neutral_nits += finding.severity is Severity.NIT
                out.append(ScoredFinding(finding, klass, Credit.NEUTRAL, 0.0))
        else:  # FP or DUP
            out.append(ScoredFinding(finding, klass, Credit.FP, claimed))
    return out


def _vu_important(finding: Finding, judged: Severity | None, params: ScoringParams) -> ScoredFinding:
    """A valid unlabeled finding claimed Important (METRICS.md section 1)."""
    if judged is Severity.IMPORTANT and finding.evidence_level >= params.vu_min_evidence:
        credit = params.valid_unlabeled_credit * params.severity_weights[Severity.IMPORTANT]
        return ScoredFinding(finding, FindingClass.VU, Credit.VU, credit)
    if judged is Severity.NIT:  # severity inflation: it costs what a false Nit costs
        return ScoredFinding(finding, FindingClass.VU, Credit.FP, params.severity_weights[Severity.NIT])
    return ScoredFinding(finding, FindingClass.VU, Credit.NEUTRAL, 0.0)


def tally(result: EvalResult, params: ScoringParams, keep: Callable[[Finding], bool] | None = None) -> Tally:
    """One PR-round's weighted sums. `keep` restricts the finding sums to some findings (a diagnostic subset), after
    every finding's part was decided; the gold sums always cover the whole round."""
    tp = vu = fp = tp_imp = 0.0
    for scored in score_findings(result, params):
        if keep is not None and not keep(scored.finding):
            continue
        if scored.credit is Credit.TP:
            tp += scored.weight
            if scored.gold is not None and scored.gold.severity is Severity.IMPORTANT:
                tp_imp += scored.weight
        elif scored.credit is Credit.VU:
            vu += scored.weight
        elif scored.credit is Credit.FP:
            fp += scored.weight
    gold_total = sum(_gold_weight(g, params) for g in result.gold)
    gold_imp = sum(_gold_weight(g, params) for g in result.gold if g.severity is Severity.IMPORTANT)
    return Tally(tp=tp, vu=vu, fp=fp, gold=gold_total, tp_important=tp_imp, gold_important=gold_imp)


def precision(t: Tally) -> float:
    denominator = t.tp + t.vu + t.fp
    return (t.tp + t.vu) / denominator if denominator else 1.0


def recall(t: Tally) -> float:
    return t.tp / t.gold if t.gold else 1.0


def f_beta(p: float, r: float, beta: float) -> float:
    b2 = beta * beta
    denominator = b2 * p + r
    return (1 + b2) * p * r / denominator if denominator else 0.0


def _by_language(results: Sequence[EvalResult], params: ScoringParams) -> dict[str, tuple[int, Tally]]:
    pooled: dict[str, tuple[int, Tally]] = {}
    for result in results:
        if result.language not in params.language_weights:
            raise ValueError(f"{result.pr}: language {result.language!r} has no weight")
        count, total = pooled.get(result.language, (0, Tally()))
        pooled[result.language] = (count + 1, total + tally(result, params))
    return pooled


def _score(t: Tally, params: ScoringParams) -> float:
    return f_beta(precision(t), recall(t), params.beta)


def _headline(tallies: Mapping[str, Tally], params: ScoringParams) -> float:
    weights = {lang: params.language_weights[lang] for lang in tallies}
    total = sum(weights.values())
    if not total:
        return 0.0
    return sum(weights[lang] * _score(t, params) for lang, t in tallies.items()) / total


def language_scores(results: Sequence[EvalResult], params: ScoringParams) -> dict[str, LanguageScore]:
    """P_L, R_L and F_L, pooled (micro-averaged) over each language group's PRs."""
    return {
        lang: LanguageScore(lang, count, precision(t), recall(t), _score(t, params))
        for lang, (count, t) in _by_language(results, params).items()
    }


def headline(results: Sequence[EvalResult], params: ScoringParams) -> float:
    """S = sum over L of pi_L * F_L."""
    return _headline({lang: t for lang, (_, t) in _by_language(results, params).items()}, params)


def important_recall(results: Sequence[EvalResult], params: ScoringParams) -> float:
    """R_imp: recall on Important gold issues, pooled over all PRs."""
    total = sum((tally(r, params) for r in results), Tally())
    return total.tp_important / total.gold_important if total.gold_important else 1.0


def clean_pr_alarm_rate(results: Sequence[EvalResult]) -> float:
    """The share of clean PRs (no gold issues) that received at least one Important finding; 0.0 with none."""
    clean = [r for r in results if not r.gold]
    if not clean:
        return 0.0
    alarmed = sum(any(f.severity is Severity.IMPORTANT for f in r.findings) for r in clean)
    return alarmed / len(clean)


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of an ascending sequence."""
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def paired_bootstrap(
    candidate: Sequence[EvalResult],
    incumbent: Sequence[EvalResult],
    params: ScoringParams,
    *,
    resamples: int,
    seed: int,
    ci_level: float,
) -> BootstrapResult:
    """Confidence interval for delta-S (METRICS.md section 6): resample PRs with replacement within each language
    group, apply the same resample to both policies, and recompute S for each. Both sides must hold the same
    PR-rounds; a resampled PR brings all of its rounds."""
    cand = {(r.pr, r.round): r for r in candidate}
    inc = {(r.pr, r.round): r for r in incumbent}
    if cand.keys() != inc.keys() or len(cand) != len(candidate) or len(inc) != len(incumbent):
        raise ValueError("a paired bootstrap needs both policies scored on the same PRs and rounds, each once")
    per_pr: dict[PRKey, tuple[str, Tally, Tally]] = {}
    for unit in cand:
        c, i = cand[unit], inc[unit]
        if c.language != i.language:
            raise ValueError(f"{unit[0]}: the two results disagree on the language")
        language, c_sum, i_sum = per_pr.get(unit[0], (c.language, Tally(), Tally()))
        if language != c.language:
            raise ValueError(f"{unit[0]}: its rounds disagree on the language")
        per_pr[unit[0]] = (language, c_sum + tally(c, params), i_sum + tally(i, params))

    strata: dict[str, list[tuple[Tally, Tally]]] = defaultdict(list)
    for key in sorted(per_pr, key=lambda k: (per_pr[k][0], k.repo, k.number)):
        language, c_sum, i_sum = per_pr[key]
        strata[language].append((c_sum, i_sum))

    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        cand_t: dict[str, Tally] = {}
        inc_t: dict[str, Tally] = {}
        for lang, pairs in strata.items():
            c_sum, i_sum = Tally(), Tally()
            for _ in range(len(pairs)):
                c, i = pairs[rng.randrange(len(pairs))]
                c_sum, i_sum = c_sum + c, i_sum + i
            cand_t[lang], inc_t[lang] = c_sum, i_sum
        deltas.append(_headline(cand_t, params) - _headline(inc_t, params))
    deltas.sort()
    alpha = 1.0 - ci_level
    point = headline(candidate, params) - headline(incumbent, params)
    if not deltas:
        return BootstrapResult(point, point, point, 0)
    mean = sum(deltas) / len(deltas)
    sd = math.sqrt(sum((d - mean) ** 2 for d in deltas) / len(deltas))
    return BootstrapResult(point, _quantile(deltas, alpha / 2), _quantile(deltas, 1 - alpha / 2), resamples, sd)


def min_gain(sigma: float, *, floor: float, multiplier: float) -> float:
    """METRICS.md section 6: min_gain = max(0.01, 2 sigma), with the floor and multiplier from `[gate]`."""
    return max(floor, multiplier * sigma)
