"""Diagnostics (METRICS.md section 7), reported every round and never gated. Pure over an `EvalRun`.

Precision of a subset of findings (consensus ones, one evidence level, one panel member, one lesson, ...) is the
METRICS.md weighted precision restricted to those findings: (TPw + VUw) / (TPw + VUw + FPw), with each finding's part
decided over its whole PR-round (`scoring.score_findings`: a valid unlabeled Nit beyond the round's cap stays an FP in
any subset); `None` when the subset is empty.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from honed.core import scoring
from honed.core.evals import EvalRun, RoundRecord
from honed.core.scoring import Credit, ScoringParams, Tally
from honed.core.types import Bucket, Finding, FindingClass, Severity
from honed.ports.call_store import LedgerEntry


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def precision_of(records: Iterable[RoundRecord], params: ScoringParams,
                 keep: Callable[[Finding], bool]) -> tuple[float | None, int]:  # fmt: skip
    """Weighted precision of the posted findings `keep` selects, and how many there are."""
    total, n = Tally(), 0
    for record in records:
        n += sum(keep(f) for f in record.result.findings)
        total = total + scoring.tally(record.result, params, keep=keep)
    return (scoring.precision(total) if n else None), n


def _classes(records: Iterable[RoundRecord]) -> Counter[str]:
    return Counter(m.klass.value for r in records for m in r.result.matches)


def scored_counts(records: Iterable[RoundRecord], params: ScoringParams) -> dict[str, int]:
    """Posted findings by their part in the sums (METRICS.md section 1): TP, credited valid unlabeled Important,
    neutral valid unlabeled, valid unlabeled Nits beyond the per-round cap, valid unlabeled Importants the judge rates
    a Nit (severity inflation), FP and DUP."""
    out: Counter[str] = Counter()
    for record in records:
        for s in scoring.score_findings(record.result, params):
            if s.klass is FindingClass.VU:
                kind = ("vu_over_cap" if s.over_cap else "vu_inflated" if s.inflated else
                        "vu_credited" if s.credit is Credit.VU else "vu_neutral")  # fmt: skip
                out[kind] += 1
            else:
                out[s.klass.value] += 1
    return dict(sorted(out.items()))


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def diagnostics(run: EvalRun, params: ScoringParams, *, act_on_flag: int,
                ledger: Sequence[LedgerEntry] = ()) -> dict[str, Any]:  # fmt: skip
    records = run.records
    rounds = len(records)
    posted = [f for r in records for f in r.result.findings]
    everything = [f for r in records for f in r.review.findings]
    out: dict[str, Any] = {}

    out["findings"] = {
        "posted": len(posted),
        "per_pr_round": _round(len(posted) / rounds if rounds else None, 2),
        "nits_per_pr_round": _round(sum(f.severity is Severity.NIT for f in posted) / rounds if rounds else None, 2),
        "important_per_pr_round": _round(
            sum(f.severity is Severity.IMPORTANT for f in posted) / rounds if rounds else None, 2
        ),
        "classes": dict(_classes(records)),
        "scored": scored_counts(records, params),
        "buckets": dict(Counter(f.bucket.value if f.bucket else "none" for f in everything)),
    }

    consensus_p, consensus_n = precision_of(records, params, lambda f: f.consensus)
    single_p, single_n = precision_of(records, params, lambda f: not f.consensus)
    out["consensus"] = {
        "rate": _round(consensus_n / len(posted) if posted else None),
        "precision_consensus": _round(consensus_p), "n_consensus": consensus_n,
        "precision_single": _round(single_p), "n_single": single_n,
    }  # fmt: skip

    levels = sorted({f.evidence_level for f in posted})
    out["evidence_level"] = {
        "distribution": dict(Counter(f.evidence_level for f in posted)),
        "precision": {
            lvl: _round(precision_of(records, params, lambda f, lvl=lvl: f.evidence_level == lvl)[0]) for lvl in levels
        },
    }

    dismissed = [(m, r) for r in records for m in r.other_matches
                 if _bucket_of(r, m.finding_id) is Bucket.DISMISSED]  # fmt: skip
    noted = [(m, r) for r in records for m in r.other_matches if _bucket_of(r, m.finding_id) is Bucket.NOTED]
    judged_dismissed = len(dismissed)
    false_dismissals = sum(m.klass is FindingClass.TP for m, _ in dismissed)
    proposed = len(everything)
    out["verifier"] = {
        "proposed": proposed,
        "dismissed": sum(f.bucket is Bucket.DISMISSED for f in everything),
        "rejection_rate": _round(
            sum(f.bucket is Bucket.DISMISSED for f in everything) / proposed if proposed else None
        ),
        "dismissed_judged": judged_dismissed,
        "false_dismissals": false_dismissals,
        "false_dismissal_rate": _round(false_dismissals / judged_dismissed if judged_dismissed else None),
        "noted_judged": len(noted),
        "noted_matching_gold": sum(m.klass is FindingClass.TP for m, _ in noted),
    }

    act_on = [sum(f.bucket is Bucket.ACT_ON for f in r.review.findings) for r in records]
    out["act_on"] = {
        "per_pr_round": _round(statistics.mean(act_on) if act_on else None, 2),
        "max": max(act_on, default=0),
        "flag": act_on_flag,
        "rounds_over_flag": sum(n > act_on_flag for n in act_on),
    }

    lint_rules = Counter(v.rule for r in records for v in r.review.lint if _is_posted(r, v.finding_id))
    out["comment_lint"] = {
        "violations": sum(lint_rules.values()),
        "per_posted_finding": _round(sum(lint_rules.values()) / len(posted) if posted else None, 2),
        "by_rule": dict(lint_rules),
    }

    reads = sum(r.review.context.reads for r in records)
    served = sum(r.review.context.served for r in records)
    out["context"] = {
        "pack_hit_rate": _round(served / reads if reads else None),
        "reads": reads,
        "truncated_rounds": sum(r.review.context.truncated for r in records),
        "lines_mean": _round(statistics.mean([r.review.context.lines for r in records]) if records else None, 0),
    }

    malformed = sum(r.review.malformed for r in records)
    out["well_formed"] = {
        "malformed_proposals": malformed,
        "parse_rate": _round(1 - malformed / (malformed + proposed) if malformed + proposed else None),
        "anchored_rate": _round(sum(r.anchored for r in records) / len(posted) if posted else None),
        "member_failures": sum(len(r.review.failures) for r in records),
    }

    out["cost"] = _cost(records, run.llm_run, ledger)
    latencies = [r.review.latency_s for r in records]
    out["latency_s"] = {
        "mean": _round(statistics.mean(latencies) if latencies else None, 1),
        "p50": _round(_quantile(latencies, 0.5), 1),
        "p90": _round(_quantile(latencies, 0.9), 1),
    }

    out["by_category"] = _by_category(records, params)
    out["severity_confusion"] = _severity_confusion(records)
    out["by_finder"] = _by_source(records, params)
    out["by_lesson"] = _by_lesson(records, params)
    return out


def _bucket_of(record: RoundRecord, finding_id: str) -> Bucket | None:
    return next((f.bucket for f in record.review.findings if f.id == finding_id), None)


def _is_posted(record: RoundRecord, finding_id: str) -> bool:
    return any(f.id == finding_id for f in record.result.findings)


def _cost(records: Sequence[RoundRecord], llm_run: str, ledger: Sequence[LedgerEntry]) -> dict[str, Any]:
    per_review = [r.review.cost_usd for r in records]
    stages: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "cached": 0, "cost_usd": 0.0})
    for r in records:
        for u in r.review.usage:
            name = "finder" if u.stage.startswith("finder:") else u.stage
            stages[name]["calls"] += u.calls
            stages[name]["cached"] += u.cached
            stages[name]["cost_usd"] += u.cost_usd
    live: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    for e in ledger:
        if e.run_id == llm_run and not e.cached:
            name = "finder" if e.stage.startswith("finder:") else e.stage
            live[name]["calls"] += 1
            live[name]["cost_usd"] += e.cost_usd
    return {
        "review_mean_usd": _round(statistics.mean(per_review) if per_review else None, 4),
        "review_total_usd": _round(sum(per_review), 2),
        "review_by_stage": {k: {**v, "cost_usd": round(v["cost_usd"], 3)} for k, v in sorted(stages.items())},
        "this_run_live_by_stage": {k: {**v, "cost_usd": round(v["cost_usd"], 3)} for k, v in sorted(live.items())},
    }


def _by_category(records: Sequence[RoundRecord], params: ScoringParams) -> dict[str, Any]:
    categories = {f.category for r in records for f in r.result.findings} | {
        g.category for r in records for g in r.result.gold
    }
    out = {}
    for category in sorted(c for c in categories if c):
        p, n = precision_of(records, params, lambda f, c=category: f.category == c)
        gold_w = tp_w = 0.0
        for r in records:
            matched = {m.gold_id for m in r.result.matches if m.klass is FindingClass.TP}
            for g in r.result.gold:
                if g.category == category:
                    weight = params.severity_weights[g.severity] * g.conf
                    gold_w += weight
                    tp_w += weight if g.id in matched else 0.0
        out[category] = {"findings": n, "precision": _round(p), "gold_weight": round(gold_w, 2),
                         "recall": _round(tp_w / gold_w if gold_w else None)}  # fmt: skip
    return out


def _severity_confusion(records: Sequence[RoundRecord]) -> dict[str, int]:
    """Claimed vs gold severity for true positives."""
    out: Counter[str] = Counter()
    for r in records:
        gold = {g.id: g for g in r.result.gold}
        claimed = {f.id: f for f in r.result.findings}
        for m in r.result.matches:
            if m.klass is FindingClass.TP and m.gold_id in gold:
                out[f"{claimed[m.finding_id].severity.value}->{gold[m.gold_id].severity.value}"] += 1
    return dict(out)


def _by_source(records: Sequence[RoundRecord], params: ScoringParams) -> dict[str, Any]:
    sources = sorted({s for r in records for f in r.result.findings for s in f.raised_by})
    raised = Counter(s for r in records for f in r.review.findings for s in f.raised_by)
    out = {}
    for source in sources:
        p, n = precision_of(records, params, lambda f, s=source: s in f.raised_by)
        out[source] = {"posted": n, "raised": raised[source], "precision": _round(p)}
    return out


def _by_lesson(records: Sequence[RoundRecord], params: ScoringParams) -> dict[str, Any]:
    fired = Counter(lesson for r in records for f in r.review.findings for lesson in f.lessons_cited)
    out = {}
    for lesson in sorted(fired):
        p, n = precision_of(records, params, lambda f, x=lesson: x in f.lessons_cited)
        out[lesson] = {"fired": fired[lesson], "posted": n, "precision": _round(p),
                       "rounds_fired": sum(any(lesson in f.lessons_cited for f in r.review.findings)
                                           for r in records)}  # fmt: skip
    return out


def summary_counts(run: EvalRun) -> Mapping[str, Any]:
    records = run.records
    prs = {r.result.pr for r in records}
    return {
        "prs": len(prs),
        "pr_rounds": len(records),
        "clean_pr_rounds": sum(not r.result.gold for r in records),
        "approval_only_rounds": sum(r.corpus == "approval_only" for r in records),
        "gold_issues": sum(len(r.result.gold) for r in records),
        "important_gold": sum(g.severity is Severity.IMPORTANT for r in records for g in r.result.gold),
        "by_language": dict(Counter(r.result.language for r in records)),
        "skipped": len(run.skipped),
    }
