"""`honed improve`: the improve loop (ARCHITECTURE.md section 7), wired to the store, the LLM stack, the review
pipeline and the policy directory."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from honed import config
from honed.adapters.policy_dir import PolicyDirectory, PolicyFiles
from honed.cli.reviews import evaluate_policy, policy_info, report_settings, write_report
from honed.cli.splits import ai_feedback_keys, split_keys, validation_cuts
from honed.cli.wiring import LLMWiring, open_store, pipeline, policy_rules, reset_wait
from honed.core.evals import EvalRun
from honed.core.improve import GeneratorKind
from honed.core.policy import POLICY_CHANGE_LENS, Policy, PolicyError
from honed.core.types import PRKey
from honed.learn import eval_report, gate, lessons, propose, splits
from honed.learn.feed import FeedOptions, FeedSelector
from honed.learn.improve import INTERRUPTED, ImproveLoop, ImproveOptions, RoundReport, Services
from honed.ports.llm import StopRun

_BAR = "!" * 100
WARNING = (
    f"\n{_BAR}\n!! --ignore-sensitivity: the eval has NOT been shown to separate a weakened policy from the "
    f"incumbent.\n!! Gate verdicts may be noise; every promotion this run makes is PROVISIONAL.\n{_BAR}\n"
)


def _prefix(settings: config.Settings) -> str:
    """The policy directory as the promote settings name it (relative to the project root)."""
    try:
        return settings.paths.policy.relative_to(settings.paths.root).as_posix().rstrip("/") + "/"
    except ValueError:
        return "policy/"


def _print_round(report: RoundReport) -> None:
    print(
        f"\n=== round {report.index}: incumbent {report.incumbent[:12]}, S {report.s_incumbent}, min_gain "
        f"{report.min_gain}{' (PROVISIONAL)' if report.provisional else ''}; generators {report.generators}"
    )
    for note in report.notes:
        print(f"  note: {note}")
    for a in report.attempts:
        r = a.record
        print(f"- {r.id} [{r.proposal.generator.value}/{r.proposal.edit.kind.value}] {r.outcome.value}: {r.note}")
        print(f"  hypothesis: {r.proposal.hypothesis[:300]}")
        print(f"  change: {r.proposal.change}")
        if r.self_review is not None:
            print(f"  self-review: {r.self_review.important} Important of {len(r.self_review.findings)} findings")
            for f in r.self_review.findings:
                print(f"    [{f['severity']}/{f['bucket']}] {f['path']}:{f['line']} {f['title'][:150]}")
        if r.screen is not None:
            print(f"  screen: delta-S {r.screen['delta_S']} (S {r.screen['S_candidate']} vs {r.screen['S_incumbent']}) "
                  f"on {r.screen['pr_rounds']} PR-rounds of {r.screen['prs']} PRs: "
                  f"{'pass' if r.screen['passed'] else 'screened out'}")  # fmt: skip
        if r.gate is not None:
            for rule in r.gate.rules:
                print(f"    {'PASS' if rule.passed else 'FAIL'} {rule.rule}: {rule.detail}")
    if report.promoted:
        print(f"promoted {report.promoted[:12]}")
    if report.stopped:
        print(f"STOPPED: {report.stopped}")
    print(f"live calls used so far: {report.calls_used}")


def cmd_improve(settings: config.Settings, args: argparse.Namespace) -> int:
    improve = settings.improve
    gate_split = args.split
    feed_split = gate_split if gate_split == settings.eval.dev_split else improve.feed_split
    if args.ignore_sensitivity:
        print(WARNING, file=sys.stderr)
    store = open_store(settings)
    try:  # refuse an unusable split (the dev split on a store with stored splits) before anything starts
        for split in {gate_split, feed_split}:
            split_keys(settings, store, split)
    except splits.SplitError:
        store.close()
        raise
    wiring = LLMWiring(settings, max_calls=args.max_calls)
    review_rounds = args.review_rounds or settings.eval.max_rounds
    directory = PolicyDirectory(args.policy or settings.paths.policy)
    codec = PolicyFiles(policy_rules(settings))
    wait = reset_wait(settings, wiring, args)

    def keys(split: str) -> list:
        chosen = split_keys(settings, store, split, args.repos, args.limit)
        return [k for k in chosen if str(k) in args.prs] if args.prs else chosen

    def evaluate(policy: Policy, split: str, subset: Sequence[PRKey] | None = None,
                 suffix: str = splits.SCREEN_SPLIT_SUFFIX) -> EvalRun:  # fmt: skip
        """The policy's run on the split, or on a subset of it (the screen or the feed sample), stored under the
        split's name plus `suffix`."""
        chosen = keys(split) if subset is None else list(subset)
        label = split if subset is None else split + suffix
        run = evaluate_policy(settings, store, wiring, policy, chosen, split=label, rounds=review_rounds,
                              sample=0, wait=wait)  # fmt: skip
        if run.stopped:
            raise StopRun(f"evaluation stopped: {run.stopped}")
        report = eval_report.report(run, report_settings(settings), ledger=wiring.calls.ledger(),
                                    policy=policy_info(settings, policy))  # fmt: skip
        write_report(settings, f"eval-{run.id}", report)
        return run

    proposer_model = settings.models.proposer
    g, m = settings.gate, settings.models
    services = Services(
        store=store, evals=store, records=store, codec=codec, directory=directory,
        proposer=propose.Proposer(wiring.llm, propose.ProposerOptions(
            model=proposer_model.online, effort=proposer_model.effort, max_tokens=improve.proposer_max_tokens,
            categories=settings.label.categories, min_prs=settings.lessons.min_prs,
            min_authors=settings.lessons.min_authors, removal_min_exposure=g.removal_min_exposure)),
        evaluate=evaluate,
        feed=FeedSelector(store, store, FeedOptions(
            sample_rounds=improve.feed_sample_rounds, review_rounds=review_rounds,
            cuts=validation_cuts(settings, store), ai_feedback=tuple(ai_feedback_keys(settings, store)))),
        reviewer_for=lambda policy: pipeline(settings, wiring.llm, policy, store, focus=POLICY_CHANGE_LENS),
        split_keys=keys, report=report_settings(settings),
        gate_rules=gate.GateRules(
            min_gain_floor=g.min_gain_floor, language_tolerance=g.language_tolerance,
            important_recall_tolerance=g.important_recall_tolerance, min_prs_per_language=g.min_prs_per_language,
            clean_pr_alarm_rise_pp=g.clean_pr_alarm_rise_pp, cost_cap_usd=g.cost_cap_usd,
            cost_growth_max=g.cost_growth_max, latency_p90_max_s=g.latency_p90_max_s,
            policy_max_lessons=g.policy_max_lessons, policy_max_prompt_tokens=g.policy_max_prompt_tokens,
            well_formed_min=g.well_formed_min, removal_min_exposure=g.removal_min_exposure,
            screen_fraction=g.screen_fraction),
        lesson_rules=lessons.LessonRules(settings.lessons.min_prs, settings.lessons.min_authors),
        rules=propose.Rules(
            categories=settings.label.categories, high_risk=settings.safety.high_risk_categories,
            models={stage: (getattr(m, stage).online, *getattr(m, stage).allowed)
                    for stage in ("intent", "finders", "verifier")},
            max_lessons=g.policy_max_lessons, max_prompt_tokens=g.policy_max_prompt_tokens,
            min_prs=settings.lessons.min_prs, min_authors=settings.lessons.min_authors),
        permits=settings.promote.permits,
        calls_used=lambda: wiring.live_calls,
    )  # fmt: skip
    options = ImproveOptions(
        rounds=args.rounds, min_attempts=args.min_attempts, candidates_per_round=improve.candidates_per_round,
        plateau_rejects=improve.plateau_rejects, target_s=improve.target_s,
        generators=tuple(GeneratorKind(gen) for gen in improve.generators), feed_split=feed_split,
        gate_split=gate_split, eval_rounds=review_rounds, backend=wiring.backend, offline=wiring.provisional,
        ignore_sensitivity=args.ignore_sensitivity, max_failure_cases=improve.max_failure_cases,
        prefix=_prefix(settings), language=settings.corpus.fallback_language,
    )  # fmt: skip
    rounds: list[dict[str, Any]] = []
    loop = ImproveLoop(services, options)

    def on_round(report: RoundReport) -> None:
        _print_round(report)
        rounds.append(report.to_json())
        write_report(settings, f"improve-{wiring.run_id}", {
            "run": wiring.run_id, "gate_split": gate_split, "feed_split": feed_split,
            "feed_sample_rounds": None if feed_split == gate_split else improve.feed_sample_rounds,
            "review_rounds": review_rounds, "rounds": rounds})  # fmt: skip

    try:
        feed = "the gate split's run" if feed_split == gate_split else (
            f"{improve.feed_sample_rounds} PR-rounds of {feed_split!r}")  # fmt: skip
        print(f"improve: {args.rounds} rounds on gate split {gate_split!r} (feed: {feed}), {review_rounds} review "
              f"rounds per PR, policy {directory.path}, backend {wiring.backend}, up to "
              f"{args.max_calls or settings.llm.run_call_cap} live calls")  # fmt: skip
        result = loop.run(on_round=on_round)
    except PolicyError as error:
        print(f"error: policy: {error}", file=sys.stderr)
        return 2
    finally:
        print(wiring.status_line(), file=sys.stderr)
        wiring.close()
        store.close()
    print(f"\n{result.stop_reason}")
    final = {"stop_reason": result.stop_reason, "attempts": result.attempts}
    print(json.dumps(final))
    stopped = any(r.stopped for r in result.rounds)
    refused = any((r.stopped or "").startswith("refused") for r in result.rounds)
    interrupted = any(INTERRUPTED in (r.stopped or "") for r in result.rounds)
    return 2 if refused else 130 if interrupted else 3 if stopped else 0
