"""Commands that review: `review` (one PR or diff), `eval` (a split, with the sensitivity check), `decisions`."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from honed import config
from honed.adapters import policy_dir
from honed.adapters.null_reader import NullReader
from honed.adapters.pack_reader import PackReader
from honed.adapters.sqlite_store import SqliteStore
from honed.cli.splits import split_keys
from honed.cli.wiring import (
    LLMWiring,
    git_reader,
    github_host,
    load_policy,
    make_judge,
    open_store,
    pack_reader,
    pipeline,
    readers,
    reset_wait,
)
from honed.core import marks, patches, symbols
from honed.core.evals import EvalRun
from honed.core.policy import Policy, PolicyError
from honed.core.reviews import PriorFinding, ReviewRequest
from honed.core.types import Bucket, Finding, PRKey, Severity
from honed.learn import eval_report, replay, sensitivity
from honed.learn.evaluate import EvalOptions, Evaluator
from honed.learn.jobs import ResetWait
from honed.learn.packs import PackBuilder
from honed.learn.replay import RoundGold, RoundGoldOptions
from honed.learn.stats import Table, render
from honed.ports.code_reader import CodeReader
from honed.ports.llm import CallBatch, StopRun
from honed.review import render as review_render

_PR_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


# ---- review ------------------------------------------------------------------------------------------------


def _language(settings: config.Settings, paths: list[str]) -> str:
    """The language group of a bare diff: its most common source language, or the fallback group."""
    found = Counter(lang for p in paths if (lang := symbols.language_of(p)) in settings.metrics.language_weights)
    return found.most_common(1)[0][0] if found else settings.corpus.fallback_language


def _prior(path: Path | None, dismissed: str | None) -> tuple[PriorFinding, ...]:
    """Re-review mode: the findings of an earlier `review --json`, with the ids humans dismissed."""
    if path is None:
        return ()
    data = json.loads(path.read_text())
    gone = {i.strip() for i in (dismissed or "").split(",") if i.strip()}
    out = []
    for raw in [*data.get("posted", []), *data.get("dismissed", [])]:
        finding = Finding(
            id=raw["id"],
            path=raw["path"],
            start_line=raw["start_line"],
            end_line=raw["end_line"],
            severity=Severity(raw["severity"]),
            category=raw["category"],
            title=raw["title"],
            body=raw.get("body", ""),
            bucket=Bucket(raw["bucket"]) if raw.get("bucket") else None,
        )
        out.append(PriorFinding(finding, dismissed=raw["id"] in gone or finding.bucket is Bucket.DISMISSED))
    return tuple(out)


def _stored_request(settings: config.Settings, store: SqliteStore, key: PRKey,
                    round_index: int) -> tuple[ReviewRequest, CodeReader]:  # fmt: skip
    item = store.get_pr(key)
    if item is None:
        raise SystemExit(f"error: {key} is not in the store")
    waiting = store.stripped(key)
    if waiting:
        raise SystemExit(f"error: {key} was {marks.waiting_for(waiting)}")
    rounds = replay.rounds_for(item)
    if not 1 <= round_index <= len(rounds):
        raise SystemExit(f"error: {key} has {len(rounds)} review round(s)")
    round_ = rounds[round_index - 1]
    pack = store.get_round_pack(key, round_.commit)
    if pack is None:
        if settings.offline:
            raise SystemExit(f"error: no context pack for {key} at {round_.commit[:10]}, and offline")
        builder = PackBuilder(git_reader(settings, key.repo), store, settings.harvest.pack)
        if round_.commit == item.reviewed_commit:
            pack = builder.build(item)
            store.save_pack(pack)
        else:
            host = github_host(settings)
            (pack,) = builder.build_rounds(item, [round_], lambda base, head: host.compare(key.repo, base, head))
    diff = replay.diff_for(item, round_, pack)
    if diff is None:
        raise SystemExit(f"error: no diff for {key} round {round_index}")
    return replay.request_for(item, round_, diff), PackReader(pack, store.get_blob)


def cmd_review(settings: config.Settings, args: argparse.Namespace) -> int:
    """Review a stored PR (at a review round) or a diff file. Nothing is posted."""
    store = open_store(settings)
    wiring = LLMWiring(settings, max_calls=args.max_calls)
    try:
        policy = load_policy(settings, args.policy)
        match = _PR_REF.match(args.target)
        if match:
            request, reader = _stored_request(settings, store, PRKey(match.group(1), int(match.group(2))), args.round)
        else:
            path = Path(args.target)
            if not path.is_file():
                print(f"error: {args.target} is neither owner/name#N nor a diff file", file=sys.stderr)
                return 2
            files = tuple(patches.parse_diff_file(path.read_text()))
            request = ReviewRequest(
                repo=args.repo_name or "local", number=None, title=args.title or path.name, body="", author="",
                language=_language(settings, [f.path for f in files]), base_commit="", head_commit="", files=files,
            )  # fmt: skip
            reader = NullReader()
        prior = _prior(args.prior, args.dismissed)
        if prior:
            from dataclasses import replace

            request = replace(request, prior_findings=prior)
        result = pipeline(settings, wiring.llm, policy, store).review(request, reader, sample=args.sample)
        print(json.dumps(review_render.to_json(result), indent=2) if args.json else review_render.markdown(result))
        print(wiring.status_line(), file=sys.stderr)
        return 0
    except PolicyError as error:
        print(f"error: policy: {error}", file=sys.stderr)
        return 2
    except StopRun as error:
        print(f"stopped: {error}\n{wiring.status_line()}", file=sys.stderr)
        return 3
    finally:
        wiring.close()
        store.close()


# ---- eval --------------------------------------------------------------------------------------------------


def _split_keys(settings: config.Settings, store: SqliteStore, args: argparse.Namespace) -> list[PRKey]:
    return split_keys(settings, store, args.split, args.repos, args.limit)


def report_settings(settings: config.Settings) -> eval_report.ReportSettings:
    gate = settings.gate
    return eval_report.ReportSettings(
        params=settings.metrics.scoring_params(), resamples=gate.bootstrap_resamples, seed=settings.eval.bootstrap_seed,
        ci_level=gate.ci_level, act_on_flag=settings.review.act_on_flag, min_prs_per_language=gate.min_prs_per_language,
    )  # fmt: skip


def policy_info(settings: config.Settings, policy: Policy) -> dict[str, Any]:
    return {"hash": policy.content_hash, "lessons_active": len(policy.active_lessons),
            "max_lessons": settings.gate.policy_max_lessons, "prompt_tokens": policy.prompt_tokens(),
            "max_prompt_tokens": settings.gate.policy_max_prompt_tokens,
            "panel": [f"{m.id}:{m.model}:{m.effort}" for m in policy.config.members],
            "verifier": f"{policy.config.verifier.model}:{policy.config.verifier.effort}"
                        + ("" if policy.config.verifier.enabled else " (off)")}  # fmt: skip


def _evaluate(settings: config.Settings, store: SqliteStore, wiring: LLMWiring, policy: Policy, keys: list[PRKey],
              args: argparse.Namespace, *, rounds: int, sample: int) -> EvalRun:  # fmt: skip
    return evaluate_policy(settings, store, wiring, policy, keys, split=args.split, rounds=rounds, sample=sample,
                           fresh=args.fresh, concurrency=args.concurrency,
                           wait=reset_wait(settings, wiring, args), batch=wiring.batch)  # fmt: skip


def evaluate_policy(settings: config.Settings, store: SqliteStore, wiring: LLMWiring, policy: Policy,
                    keys: list[PRKey], *, split: str, rounds: int, sample: int, fresh: bool = False,
                    concurrency: int | None = None, wait: ResetWait | None = None,
                    batch: CallBatch | None = None) -> EvalRun:  # fmt: skip
    """A complete stored run with this key, judged by the same judge, or a new one (saved even when it stops early).
    Reviews are served from the call cache where they can be, so a re-judged run costs only the judge's calls.
    With a `batch` (`[llm.anthropic] use_batches`), the calls go out as message batches."""
    wanted = tuple(str(k) for k in keys)
    judge = make_judge(settings, wiring.judge_llm)
    judge_id = wiring.judge_id(judge)
    stored = store.latest_eval_run(policy.content_hash, split, wiring.backend, rounds, sample)
    if stored is not None and not fresh and stored.prs == wanted:
        if stored.judge == judge_id:
            print(f"reusing stored run {stored.id} (policy {policy.short_hash}, sample {sample})")
            return stored
        print(f"not reusing stored run {stored.id}: judged by {stored.judge or 'an earlier judge'}, now {judge_id}")
    reviewer = pipeline(settings, wiring.llm, policy, store)
    gold = RoundGold(store, make_judge(settings, wiring.gold_llm), readers(settings), RoundGoldOptions(
        max_rounds=rounds, context_lines=settings.label.region_context_lines, gold_conf=settings.metrics.gold_conf,
        include_escaped_defects=settings.eval.include_escaped_defects,
    ))  # fmt: skip
    options = EvalOptions(
        split=split, rounds=rounds, sample=sample, backend=wiring.backend,
        concurrency=concurrency or settings.llm.concurrency, code_excerpt_lines=settings.eval.code_excerpt_lines,
        max_findings_judged=settings.eval.max_findings_judged, llm_run=wiring.run_id, wait_for_reset=wait,
        judge=judge_id, batch=batch,
    )  # fmt: skip
    print(f"evaluating policy {policy.short_hash} (sample {sample}) on {len(keys)} PRs, up to {rounds} rounds each")
    outcome = Evaluator(store, store, reviewer, judge, gold, pack_reader(store), options).run(keys)
    store.save_eval_run(outcome.run)
    print("gold jobs:", outcome.gold_jobs.summary())
    print("round jobs:", outcome.round_jobs.summary())
    print(wiring.status_line())
    return outcome.run


def write_report(settings: config.Settings, name: str, report: dict[str, Any]) -> Path:
    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    path = settings.paths.reports / f"{name}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return path


def _print_metrics(report: dict[str, Any]) -> None:
    m, c = report["metrics"], report["counts"]
    print(
        f"\nS = {m['S']}  ({c['prs']} PRs, {c['pr_rounds']} PR-rounds, {c['gold_issues']} gold issues, "
        f"{c['important_gold']} Important; {c['clean_pr_rounds']} clean PR-rounds; {c['skipped']} skipped)"
    )
    for lang, s in m["languages"].items():
        print(
            f"  {lang}: P {s['precision']}  R {s['recall']}  F0.5 {s['f']}  ({s['prs']} PRs, {s['pr_rounds']} "
            "PR-rounds)"
        )
    print(
        f"  Important recall {m['important_recall']}; clean-PR alarm rate {m['clean_pr_alarm_rate']}; "
        f"mean cost ${m['mean_cost_usd']} per review; latency p90 {m['latency_p90_s']}s"
    )
    vs = report.get("vs_incumbent")
    if vs:
        print(
            f"  vs incumbent {vs['incumbent_run']}: delta-S {vs['delta_S']} CI {vs['ci']} on "
            f"{vs['common_pr_rounds']} PR-rounds"
        )
    d = report["diagnostics"]
    print(f"  findings: {d['findings']['classes']}; consensus rate {d['consensus']['rate']}; evidence "
          f"{d['evidence_level']['distribution']}; false dismissals {d['verifier']['false_dismissals']}/"
          f"{d['verifier']['dismissed_judged']}; act-on per PR-round {d['act_on']['per_pr_round']}; lint "
          f"{d['comment_lint']['violations']}; pack hit rate {d['context']['pack_hit_rate']}")  # fmt: skip


def cmd_eval(settings: config.Settings, args: argparse.Namespace) -> int:
    store = open_store(settings)
    wiring = LLMWiring(settings, max_calls=args.max_calls)
    rounds = args.rounds or settings.eval.max_rounds
    try:
        policy = load_policy(settings, args.policy)
        keys = _split_keys(settings, store, args)
        if not keys:
            print(f"error: no eligible PRs in split {args.split!r}", file=sys.stderr)
            return 2
        if args.sensitivity:
            return _sensitivity(settings, store, wiring, policy, keys, args, rounds)
        run = _evaluate(settings, store, wiring, policy, keys, args, rounds=rounds, sample=args.sample)
        incumbent_id = args.incumbent or store.incumbent(args.split, wiring.backend, rounds)
        incumbent = store.get_eval_run(incumbent_id) if incumbent_id else None
        if incumbent is not None and incumbent.judge != run.judge:
            print(f"WARNING: the incumbent run {incumbent.id} was judged by {incumbent.judge or 'an earlier judge'}, "
                  f"this run by {run.judge}: the comparison mixes two yardsticks. Re-evaluate the incumbent's "
                  "policy to compare them.", file=sys.stderr)  # fmt: skip
        report = eval_report.report(run, report_settings(settings), incumbent=incumbent,
                                    ledger=wiring.calls.ledger(), policy=policy_info(settings, policy))  # fmt: skip
        path = write_report(settings, f"eval-{run.id}", report)
        _print_metrics(report)
        if args.set_incumbent and run.stopped is None:
            store.set_incumbent(args.split, wiring.backend, rounds, run.id)
            print(f"run {run.id} is now the incumbent for split {args.split}, {wiring.backend}, {rounds} rounds")
        print(f"report: {path}")
        return 3 if run.stopped else 0
    except PolicyError as error:
        print(f"error: policy: {error}", file=sys.stderr)
        return 2
    finally:
        wiring.close()
        store.close()


def _sensitivity(settings: config.Settings, store: SqliteStore, wiring: LLMWiring, policy: Policy,
                 keys: list[PRKey], args: argparse.Namespace, rounds: int) -> int:  # fmt: skip
    if args.samples < 2:
        print("error: the noise floor needs at least 2 samples", file=sys.stderr)
        return 2
    seeds = []
    for sample in range(args.samples):
        run = _evaluate(settings, store, wiring, policy, keys, args, rounds=rounds, sample=sample)
        if run.stopped:
            return 3
        seeds.append(run)
    weak_policy = sensitivity.weaken(policy)
    policy_dir.write(settings.paths.data / "policies" / f"weakened-{weak_policy.short_hash}", weak_policy)
    weak = _evaluate(settings, store, wiring, weak_policy, keys, args, rounds=rounds, sample=0)
    if weak.stopped:
        return 3
    gate, rs = settings.gate, report_settings(settings)
    result = sensitivity.analyze(seeds, weak, rs, floor=gate.min_gain_floor,
                                 multiplier=gate.min_gain_noise_multiplier)  # fmt: skip
    logged = {(d.change, d.delta, d.verdict) for d in store.decisions()}
    for row in sensitivity.decisions(seeds, weak, result):  # a re-analysis with other numbers adds rows
        if (row.change, row.delta, row.verdict) not in logged:
            store.add_decision(row)
    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(), "split": args.split, "rounds": rounds,
        "runs": {"seeds": [s.id for s in seeds], "weakened": weak.id},
        "policies": {"seed": policy_info(settings, policy), "weakened": policy_info(settings, weak_policy)},
        **result,
        "seed_metrics": [eval_report.headline(s.results, rs.params) for s in seeds],
        "weakened_metrics": eval_report.headline(weak.results, rs.params),
    }  # fmt: skip
    path = write_report(settings, f"sensitivity-{seeds[0].id}", report)
    noise = result["noise_floor"]
    print(f"\nnoise floor over {len(seeds)} samples: S {noise['S_samples']}; pairwise delta-S "
          f"{[p['delta_S'] for p in noise['pairs']]}; sigma {noise['sigma']}; min_gain = {result['min_gain_rule']} = "
          f"{result['min_gain']}")  # fmt: skip
    print(f"weakened: {result['verdict']}")
    print(f"report: {path}")
    return 0


# ---- decision log ------------------------------------------------------------------------------------------


def cmd_decisions(settings: config.Settings, args: argparse.Namespace) -> int:
    store = open_store(settings)
    try:
        rows = store.decisions()
    finally:
        store.close()
    table = Table("Decision log", ("id", "at", "verdict", "delta", "change", "hypothesis"),
                  tuple((str(d.id), d.at[:16], d.verdict, d.delta, d.change[:70], d.hypothesis[:70])
                        for d in rows))  # fmt: skip
    print(render(table))
    if args.json:
        print(json.dumps([d.__dict__ for d in rows], indent=2))
    return 0
