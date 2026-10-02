"""Commands for the yardstick audits and the local model: `audit-judge --cross-family`, `fetch-local-model`
(METRICS.md section 5; ARCHITECTURE.md sections 6 and 8)."""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from honed import config
from honed.adapters.local_llm import download, model_dir
from honed.cli.human_labels import load_people
from honed.cli.reviews import write_report
from honed.cli.wiring import LLMWiring, make_judge, open_store, pack_reader, readers
from honed.learn.audit import JudgeAudit, human_items
from honed.learn.crossfamily import AuditOptions, CrossFamilyAudit, collect, select_matching, select_validity


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3f}"


def cross_family(settings: config.Settings, args: argparse.Namespace) -> int:
    """The local model re-asks a sample of Fable's validity and match questions from stored evaluation runs."""
    store = open_store(settings)
    wiring = LLMWiring(settings, max_calls=args.max_calls)
    try:
        run_ids = args.runs or store.eval_run_ids(split=args.split, backend="claude_code")
        runs = [run for rid in run_ids if (run := store.get_eval_run(rid)) is not None]
        if not runs:
            print(f"error: no complete claude_code evaluation runs on split {args.split!r}", file=sys.stderr)
            return 2
        options = AuditOptions(validity_items=args.n or settings.judge.audit_items, match_rounds=args.match_rounds,
                               excerpt_lines=settings.eval.code_excerpt_lines,
                               max_findings_judged=settings.eval.max_findings_judged)  # fmt: skip
        asked = collect(runs, store, pack_reader(store), options)
        validity = select_validity(asked, options.validity_items)
        matching = select_matching(asked, options.match_rounds)
        judge = make_judge(settings, wiring.local_judge_llm(settings, max_calls=args.max_calls))
        local = settings.llm.local.model
        print(f"cross-family audit: {local} re-asks Fable's questions from {len(runs)} runs: "
              f"{sum(len(r.validity) for r in validity)} validity verdicts in {len(validity)} PR-rounds, "
              f"{len(matching)} match questions")  # fmt: skip
        j = settings.judge
        report = CrossFamilyAudit(judge, alarm_kappa=j.cross_family_alarm_kappa, min_f1=j.matcher_min_f1).run(
            validity, matching
        )
        report.min_human_accuracy = j.local_min_human_accuracy
        people = load_people(settings)  # majority answers only: a single labeler's answer is not counted
        human = [a for a in human_items(people.answers, people.where, store.get_pr) if a.counted]
        if human:  # the local judge against people: does its alarm count?
            print(f"the local judge answers {len(human)} human-labeled comments (does the alarm count?)")
            audit = JudgeAudit(judge, readers(settings), context_lines=settings.label.region_context_lines,
                               concurrency=1)  # fmt: skip
            human_report = audit.run(human, consistency_items=0, repeats=1)
            report.human_n, report.human_accuracy = human_report.n, human_report.accuracy
        print("local judge vs human labels: "
              + (f"accuracy {_fmt(report.human_accuracy)} on {report.human_n} (the alarm counts at "
                 f"{j.local_min_human_accuracy} or better)" if human else "no human labels yet"))  # fmt: skip
        print(f"validity: {report.validity_n} findings, agreement {_fmt(report.agreement)}, Cohen's kappa "
              f"{_fmt(report.kappa)} (alarm below {j.cross_family_alarm_kappa}: {report.alarm_status()}); "
              f"confusion fable->local {dict(report.confusion)}")  # fmt: skip
        print(
            f"matcher: {report.match_rounds} PR-rounds, {report.match_decisions} decisions, agreement "
            f"{_fmt(report.decision_agreement)}; pairs fable {report.pairs_fable}, local {report.pairs_local}, both "
            f"{report.pairs_both}: precision {_fmt(report.precision)}, recall {_fmt(report.recall)}, F1 "
            f"{_fmt(report.f1)} (min {j.matcher_min_f1}: "
            f"{'pass' if report.matcher_passes else 'FAIL' if report.matcher_passes is not None else 'undefined'})"
        )
        for what, why in sorted(report.failures.items()):
            print(f"  no usable local answer for {what}: {why[:200]}")
        print(wiring.local_line())
        stats = wiring.local.stats if wiring.local else None
        payload = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(), "local_model": local, "runs": [r.id for r in runs],
            "local_vs_human": {"n": report.human_n, "accuracy": report.human_accuracy,
                               "min_accuracy": j.local_min_human_accuracy, "alarm_counts": report.alarm_counts},
            "validity": {"n": report.validity_n, "pr_rounds": report.validity_rounds, "agreement": report.agreement,
                         "kappa": report.kappa, "alarm_below": j.cross_family_alarm_kappa, "alarm": report.alarm,
                         "alarm_status": report.alarm_status(),
                         "confusion_fable_to_local": dict(report.confusion),
                         "disagreements": report.disagreements},
            "matcher": {"pr_rounds": report.match_rounds, "decisions": report.match_decisions,
                        "decision_agreement": report.decision_agreement, "pairs_fable": report.pairs_fable,
                        "pairs_local": report.pairs_local, "pairs_both": report.pairs_both,
                        "precision": report.precision, "recall": report.recall, "f1": report.f1,
                        "min_f1": j.matcher_min_f1},
            "failures": report.failures,
            "local_stats": None if stats is None else {
                "calls": stats.calls, "structured": stats.structured, "first_try_failures": stats.first_try_failures,
                "retries": stats.retries, "failures": stats.failures, "parse_failure_rate": stats.parse_failure_rate,
                "prompt_tokens": stats.prompt_tokens, "output_tokens": stats.output_tokens,
                "seconds": round(stats.seconds, 1),
                "prefill_tps": stats.prompt_tps and round(sum(stats.prompt_tps) / len(stats.prompt_tps), 1),
                "decode_tps": stats.generation_tps and round(sum(stats.generation_tps) / len(stats.generation_tps), 1),
            },
        }  # fmt: skip
        path = write_report(settings, f"crossfamily-{wiring.run_id}", payload)
        print(f"report: {path}")
        return 3 if "stopped" in report.failures else 0
    finally:
        wiring.close()
        store.close()


def cmd_fetch_local_model(settings: config.Settings, args: argparse.Namespace) -> int:
    repo = args.model or settings.llm.local.model
    target = model_dir(settings.paths.models, repo)
    path = download(repo, target)
    size = sum(f.stat().st_size for f in path.rglob("*.safetensors"))
    print(f"{repo}: {size / 1e9:.1f} GB of weights in {path}")
    if repo != settings.llm.local.model:
        print(f"note: [llm.local] model is {settings.llm.local.model!r}; set it to {repo!r} to use this one")
    return 0
