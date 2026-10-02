"""Commands over the harvested corpus: harvest, pack, stats, label, audit-judge, export-audit-sample, usage."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from collections.abc import Sequence

from honed import config
from honed.adapters import human_labels
from honed.adapters.blobs import BlobStore
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.github import GitHubCodeHost
from honed.adapters.github_parse import discussion_url
from honed.adapters.sqlite_store import SqliteStore
from honed.cli import audits
from honed.cli.human_labels import load_people
from honed.cli.splits import split_keys
from honed.cli.wiring import (
    LLMWiring,
    git_reader,
    github_host,
    label_keys,
    make_judge,
    open_store,
    readers,
    require_online,
    reset_wait,
)
from honed.core import redaction, sampling
from honed.core.filters import is_excluded
from honed.core.types import ContextPack, Corpus, HarvestedPR, PRKey, Thread
from honed.learn import audit_sample, label_report, replay, stats, usage
from honed.learn.audit import (
    HUMAN_MAJORITY,
    HUMAN_SINGLE,
    JudgeAudit,
    audit_candidates,
    human_items,
    select_items,
    with_human,
)
from honed.learn.harvest import Harvester, HarvestOptions
from honed.learn.label import Labeler, LabelOptions, label_summary, mark_approval_only
from honed.learn.packs import PackBuilder, summarize
from honed.learn.plan import ExcludedRepo, UnknownRepo, corpus_plans, select


def cmd_harvest(settings: config.Settings, args: argparse.Namespace) -> int:
    require_online(settings, "harvest")
    corpus, harvest = settings.corpus, settings.harvest
    plans = corpus_plans(
        groups={g.key: g.repos for g in corpus.groups},
        weights=settings.metrics.language_weights,
        target=harvest.target_prs,
        ai_repos=corpus.ai_feedback,
        ai_target=harvest.ai_feedback_target_prs,
    )
    try:
        chosen = select(plans, args.repos, corpus.excluded)
    except (ExcludedRepo, UnknownRepo) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    options = HarvestOptions(
        slices=tuple(sampling.split_window(harvest.window, harvest.window_slices)),
        page_size=harvest.search_page_size,
        line_slack=harvest.fixed_line_slack,
        excluded=corpus.excluded,
        github_languages=corpus.github_languages,
        fallback_language=corpus.fallback_language,
        limit=args.limit,
        approval_only_share=harvest.approval_only_share,
        bug_targeted_share=harvest.bug_targeted_share,
    )
    host, store = github_host(settings), open_store(settings)
    try:
        reports = Harvester(host, store, options).run(chosen)
    finally:
        store.close()
    for r in reports:
        skipped = ", ".join(f"{k} {v}" for k, v in r.skipped.most_common())
        print(f"{r.repo} [{r.corpus}]: +{r.added} PRs ({r.targeted} targeted, +{r.approval_only} approval-only),"
              f" {r.threads} threads; skipped: {skipped or 'none'}"
              + (f"; STOPPED: {r.stopped}" if r.stopped else ""))  # fmt: skip
    print(f"GraphQL points used: {host.points_used}; REST calls: {host.rest_calls}")
    return 3 if any(r.stopped for r in reports) else 0  # 3: the run's budget is spent; running again resumes


def cmd_pack(settings: config.Settings, args: argparse.Namespace) -> int:
    """Build context packs for harvested PRs (or those of `--split`), one repo at a time."""
    require_online(settings, "pack")
    store = open_store(settings)
    blobs = BlobStore(settings.paths.blobs)
    in_splits = {k for split in args.splits for k in split_keys(settings, store, split)} if args.splits else None
    by_repo: dict[str, list[PRKey]] = {}
    for key in store.pr_keys():
        if in_splits is None or key in in_splits:
            by_repo.setdefault(key.repo, []).append(key)
    wanted = {r.lower() for r in args.repos} if args.repos else None
    peak_clone = 0
    blobs_before = blobs.disk_usage()
    all_packs = []
    host = None
    try:
        for repo, keys in sorted(by_repo.items()):
            if (wanted and repo.lower() not in wanted) or is_excluded(repo, settings.corpus.excluded):
                continue
            reader = git_reader(settings, repo)
            builder = PackBuilder(reader, store, settings.harvest.pack)
            chosen = keys[: args.limit] if args.limit else keys
            report = builder.build_all(chosen, rebuild=args.rebuild)
            if args.rounds > 1:
                host = host or github_host(settings)
                rounds_built = _round_packs(builder, store, chosen, args.rounds, host, repo, rebuild=args.rebuild)
                all_packs += rounds_built
                print(f"{repo}: {len(rounds_built)} round packs built")
            clone = reader.disk_usage()
            peak_clone = max(peak_clone, clone)
            if not settings.harvest.keep_clones:
                reader.remove()
            all_packs += report.built
            print(f"{repo}: {len(report.built)} packs built, {len(report.failed)} failed; clone {clone / 2**20:.0f} MB"
                  + ("" if settings.harvest.keep_clones else " (removed)"))  # fmt: skip
            for pr, error in report.failed.items():
                print(f"  failed {pr}: {error}")
    finally:
        store.close()
    summary = summarize(all_packs)
    print("pack summary:", ", ".join(f"{k}={v}" for k, v in summary.items()))
    blobs_after = blobs.disk_usage()
    print(f"disk: largest clone {peak_clone / 2**20:.0f} MB; blobs {blobs_before / 2**20:.0f} -> "
          f"{blobs_after / 2**20:.0f} MB")  # fmt: skip
    return 0


def _round_packs(builder: PackBuilder, store: SqliteStore, keys: Sequence[PRKey], rounds: int, host: GitHubCodeHost,
                 repo: str, *, rebuild: bool) -> list[ContextPack]:  # fmt: skip
    """Packs at the later replayed rounds' commits (`--rounds N`), from the code host's compare for each round."""
    built = []
    for key in keys:
        item = store.get_pr(key)
        if item is None or item.corpus is not Corpus.HUMAN:
            continue
        replayed = replay.replayed_rounds(item, rounds)
        try:
            built += builder.build_rounds(item, replayed, lambda base, head: host.compare(repo, base, head),
                                          rebuild=rebuild)  # fmt: skip
        except Exception as error:
            print(f"  round packs failed for {key}: {error}")
    return built


def cmd_stats(settings: config.Settings, args: argparse.Namespace) -> int:
    store = open_store(settings)
    try:
        tables = stats.tables(store.pr_facts(), store.thread_facts())
        packs = [p for p in (store.get_pack(k) for k in store.pr_keys()) if p is not None]
    finally:
        store.close()
    print("\n\n".join(stats.render(t) for t in tables))
    print("\n## Context packs\n" + ", ".join(f"{k}={v}" for k, v in summarize(packs).items()))
    return 0


def cmd_label(settings: config.Settings, args: argparse.Namespace) -> int:
    """Approval-only split, applied suggestions, judge jobs, judged labels and gold sets."""
    store = open_store(settings)
    wiring = LLMWiring(settings, max_calls=args.max_calls)
    try:
        split = mark_approval_only(store)
        print("approval-only split:", ", ".join(f"{k} {v}" for k, v in sorted(split.items())))
        keys = label_keys(store, settings, args.repos, args.limit)
        options = LabelOptions(
            concurrency=settings.llm.concurrency, context_lines=settings.label.region_context_lines,
            high_risk=frozenset(settings.safety.high_risk_categories), gold_conf=settings.metrics.gold_conf,
            rebuild_gold=args.rebuild_gold, wait_for_reset=reset_wait(settings, wiring, args), batch=wiring.batch,
        )  # fmt: skip
        labeler = Labeler(store, store, make_judge(settings, wiring.judge_llm), readers(settings), options)
        report = labeler.run(keys)
        print(f"PRs labeled: {report.prs}; suggestions: {dict(report.suggestions)}")
        print("judge jobs:", report.judge_jobs.summary())
        for job, error in sorted(report.judge_jobs.failed.items()):
            print(f"  failed {job}: {error[:200]}")
        print("gold jobs:", report.gold_jobs.summary())
        for job, error in sorted(report.gold_jobs.failed.items()):
            print(f"  failed {job}: {error[:200]}")
        for pr, why in sorted(report.gold_skipped.items()):
            print(f"  gold not built for {pr}: {why}")
        judged = {k: store.judged_labels(k) for k in keys}
        gold = {k: store.get_gold(k) for k in keys if (item := store.get_pr(k)) and item.corpus is Corpus.HUMAN}
        print("\n" + "\n\n".join(stats.render(t) for t in label_report.tables(judged, gold)))
        counts = label_summary([lab for labs in judged.values() for lab in labs])
        print("\nlabel counts:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        print(wiring.status_line())
    finally:
        wiring.close()
        store.close()
    stopped = report.judge_jobs.stopped or report.gold_jobs.stopped
    return 3 if stopped else 0


def cmd_audit_judge(settings: config.Settings, args: argparse.Namespace) -> int:
    """METRICS.md section 5: judge vs human-strong outcomes and human labels (accuracy, kappa) and judge
    consistency; with --cross-family, the local model against Fable instead."""
    if args.cross_family:
        return audits.cross_family(settings, args)
    store = open_store(settings)
    wiring = LLMWiring(settings, max_calls=args.max_calls)
    try:
        pairs = [(item, store.judged_labels(key)) for key in store.pr_keys() if (item := store.get_pr(key))]
        candidates = audit_candidates(pairs)
        people = load_people(settings)
        for error in people.errors:
            print(f"warning: labels file left out: {error} (honed human-labels check)")
        human = human_items(people.answers, people.where, store.get_pr)
        items = with_human(select_items(candidates, settings.judge.audit_items), human)
        audit = JudgeAudit(
            make_judge(settings, wiring.judge_llm),
            readers(settings),
            context_lines=settings.label.region_context_lines,
            concurrency=settings.llm.concurrency,
            wait_for_reset=reset_wait(settings, wiring, args),
            batch=wiring.batch,
        )
        j = settings.judge
        report = audit.run(items, consistency_items=j.consistency_items, repeats=j.consistency_repeats)

        def fmt(value: float | None) -> str:
            return "undefined" if value is None else f"{value:.3f}"

        truth = {"valid": sum(a.truth for a in candidates), "invalid": sum(not a.truth for a in candidates)}
        counted = sum(a.counted for a in human)
        print(f"known-answer items available: {len(candidates)} {truth}, plus {counted} human majority answers; "
              f"judged: {report.n} counted ({dict(report.by_source)}); accuracy by source "
              f"{ {k: None if v is None else round(v, 3) for k, v in report.accuracy_by_source.items()} }")  # fmt: skip
        print("jobs:", report.jobs.summary())
        print(people.summary())
        print(people.agreement_line())
        majority, single = report.by_source.get(HUMAN_MAJORITY, 0), report.by_source.get(HUMAN_SINGLE, 0)
        print(f"judge vs human majority: accuracy {fmt(report.accuracy_by_source.get(HUMAN_MAJORITY))}, Cohen's "
              f"kappa {fmt(report.kappa_by_source.get(HUMAN_MAJORITY))}, n={majority}")  # fmt: skip
        if single:
            print(f"FLAGGED single-labeler items (not counted anywhere above): judge accuracy "
                  f"{fmt(report.accuracy_by_source.get(HUMAN_SINGLE))}, n={single}; each needs a second "
                  "labeler")  # fmt: skip

        print(
            f"accuracy {fmt(report.accuracy)} (threshold {j.min_accuracy}), Cohen's kappa {fmt(report.kappa)} "
            f"(threshold {j.min_kappa}), n={report.n}; confusion truth->verdict {dict(report.confusion)}"
        )
        print(
            f"consistency over {report.consistency_n} items x {j.consistency_repeats}: unanimous "
            f"{fmt(report.unanimous)} (threshold {j.min_consistency}), pairwise {fmt(report.pairwise)}"
        )
        truths = {k.split("->")[0] for k in report.confusion}
        if len(truths) < 2:
            print(
                f"kappa is not meaningful here: every judged known answer is {''.join(truths) or 'absent'} "
                "(kappa is 0 or undefined by construction). It needs human-strong negatives (a human 👎)."
            )
        print("thresholds are reported, not enforced, while n is small.")
        print(wiring.status_line())
    finally:
        wiring.close()
        store.close()
    return 3 if report.jobs.stopped else 0


def cmd_export_audit_sample(settings: config.Settings, args: argparse.Namespace) -> int:
    """A blind, stratified sample of review comments for human labeling (METRICS.md section 5), in the yardstick's
    committed shape: no code, every string through the redaction scan."""
    out = args.out or settings.paths.human_labels / human_labels.SAMPLE_NAME
    if out.exists() and not args.force:
        print(f"error: {out} exists and people's labels are keyed by its ids; --force replaces it", file=sys.stderr)
        return 2

    def link(item: HarvestedPR, thread: Thread) -> str:
        return discussion_url(item.pr.url, thread.first.id if thread.first else "")

    store = open_store(settings)
    try:
        if args.ids_from:
            earlier = json.loads(args.ids_from.read_text()).get("items", [])
            refs = [(str(e["repo"]), int(e["pr"]), str(e["id"])) for e in earlier]
            items = audit_sample.rebuild(store, refs, link=link)
        else:
            items = audit_sample.export(store, store, n=args.n or settings.judge.human_label_items,
                                        excluded=settings.corpus.excluded, link=link)  # fmt: skip
    except (OSError, json.JSONDecodeError, KeyError, audit_sample.MissingItems) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        store.close()
    records, hits = audit_sample.publishable(items)
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "note": "Blind: no outcomes, replies or judge verdicts, and no code. Each item names the commit the comment "
        "was made on and its flagged lines; the labeling page (honed human-labels serve) fetches the file from "
        "GitHub. `author` and `url` are the comment's attribution; the page never shows the url. Strings went "
        "through the redaction scan (src/honed/core/redaction.py).",
        "redaction_rules": redaction.RULES_VERSION,
        "items": records,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    repos = {item.repo for item in items}
    no_commit = [item.id for item in items if item.commit is None]
    print(f"wrote {len(items)} review comments from {len(repos)} repos to {out}")
    if no_commit:
        print(f"{len(no_commit)} without a commit (the code host no longer names it; the page falls back to the "
              f"comment's diff hunk from GitHub's API): {', '.join(no_commit)}")  # fmt: skip
    kinds = Counter(hit.kind for _, hit in hits)
    print(f"redaction: {len(hits)} hits {dict(kinds)}")
    for where, hit in hits:
        print(f"  {where}: {hit.kind} at offset {hit.start}")
    return 0


def cmd_usage(settings: config.Settings, args: argparse.Namespace) -> int:
    calls = SqliteCallStore(settings.paths.sqlite)
    try:
        entries = calls.ledger()
    finally:
        calls.close()
    if args.last and entries:
        args.run = max(entries, key=lambda e: e.at).run_id
    print("\n\n".join(stats.render(t) for t in usage.tables(entries, args.run)))
    print(
        "\nshadow_usd is list-price token cost: a shadow cost on the flat-rate subscription (claude_code), the "
        "billed cost on the anthropic backend (message batches at their discount), 0 on the local model."
    )
    return 0
