"""The evaluation report: the METRICS.md numbers for one run, the paired bootstrap against another run, and the
diagnostics. Pure over stored runs; `core/scoring.py` does every metric."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from honed.core import scoring
from honed.core.evals import EvalRun
from honed.core.scoring import BootstrapResult, ScoringParams
from honed.core.types import EvalResult, PRKey
from honed.learn import diagnostics
from honed.ports.call_store import LedgerEntry


@dataclass(frozen=True)
class ReportSettings:
    params: ScoringParams
    resamples: int
    seed: int
    ci_level: float
    act_on_flag: int  # `[review] act_on_flag`: the diagnostic's threshold
    min_prs_per_language: int


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def headline(results: Sequence[EvalResult], params: ScoringParams) -> dict[str, Any]:
    if not results:
        return {"S": None, "languages": {}, "important_recall": None, "clean_pr_alarm_rate": None,
                "mean_cost_usd": None, "latency_p90_s": None}  # fmt: skip
    languages = scoring.language_scores(results, params)
    return {
        "S": _r(scoring.headline(results, params)),
        "languages": {
            lang: {
                "pr_rounds": s.prs,
                "prs": len({r.pr for r in results if r.language == lang}),
                "precision": _r(s.precision),
                "recall": _r(s.recall),
                "f": _r(s.f),
            }
            for lang, s in sorted(languages.items())
        },
        "important_recall": _r(scoring.important_recall(results, params)),
        "clean_pr_alarm_rate": _r(scoring.clean_pr_alarm_rate(results)),
        "mean_cost_usd": _r(sum(r.cost_usd for r in results) / len(results)),
        "latency_p90_s": _r(sorted(r.latency_s for r in results)[min(len(results) - 1, int(0.9 * len(results)))], 1),
    }


def _units(results: Sequence[EvalResult]) -> dict[tuple[PRKey, int], EvalResult]:
    return {(r.pr, r.round): r for r in results}


def paired(candidate: EvalRun, incumbent: EvalRun, settings: ReportSettings) -> tuple[BootstrapResult, dict[str, Any]]:
    """The paired bootstrap of candidate vs incumbent on the PR-rounds both scored."""
    cand, inc = _units(candidate.results), _units(incumbent.results)
    common = sorted(cand.keys() & inc.keys(), key=lambda u: (u[0].repo, u[0].number, u[1]))
    c = [cand[u] for u in common]
    i = [inc[u] for u in common]
    boot = scoring.paired_bootstrap(c, i, settings.params, resamples=settings.resamples, seed=settings.seed,
                                    ci_level=settings.ci_level)  # fmt: skip
    info = {
        "incumbent_run": incumbent.id, "incumbent_policy": incumbent.policy_hash[:12],
        "common_pr_rounds": len(common), "only_candidate": len(cand) - len(common),
        "only_incumbent": len(inc) - len(common),
        "S_candidate": _r(scoring.headline(c, settings.params)),
        "S_incumbent": _r(scoring.headline(i, settings.params)),
        "delta_S": _r(boot.delta), "ci": [_r(boot.low), _r(boot.high)], "ci_level": settings.ci_level,
        "bootstrap_sd": _r(boot.sd), "resamples": boot.resamples,
        "important_recall_delta": _r(scoring.important_recall(c, settings.params)
                                     - scoring.important_recall(i, settings.params)),
        "clean_pr_alarm_rise_pp": _r(100 * (scoring.clean_pr_alarm_rate(c) - scoring.clean_pr_alarm_rate(i)), 2),
    }  # fmt: skip
    return boot, info


def report(run: EvalRun, settings: ReportSettings, *, incumbent: EvalRun | None = None,
           ledger: Sequence[LedgerEntry] = (), policy: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {
        "run": {
            "id": run.id,
            "policy": run.policy_hash,
            "split": run.split,
            "backend": run.backend,
            "rounds": run.rounds,
            "sample": run.sample,
            "created_at": run.created_at,
            "llm_run": run.llm_run,
            "stopped": run.stopped,
        },
        "counts": dict(diagnostics.summary_counts(run)),
        "metrics": headline(run.results, settings.params),
        "policy": dict(policy or {}),
        "diagnostics": diagnostics.diagnostics(run, settings.params, act_on_flag=settings.act_on_flag, ledger=ledger),
        "skipped": dict(run.skipped),
    }
    small = [lang for lang, v in out["metrics"]["languages"].items() if v["prs"] < settings.min_prs_per_language]
    if small:
        out["metrics"]["note"] = (f"languages under {settings.min_prs_per_language} PRs ({', '.join(small)}): the "
                                  "per-language gate would not apply to them")  # fmt: skip
    if incumbent is not None and incumbent.id != run.id:
        _, out["vs_incumbent"] = paired(run, incumbent, settings)
    return out
