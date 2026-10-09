"""Command line and composition root: the only place adapters are wired into services (`cli/wiring.py`)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from honed import config
from honed.cli import audits, bundles, corpus, defects, human_labels, improve, reviews, splits
from honed.cli.wiring import BackendRefused
from honed.learn.splits import SplitError

log = logging.getLogger("honed")

COMMANDS: dict[str, Callable[[config.Settings, argparse.Namespace], int]] = {
    "harvest": corpus.cmd_harvest,
    "pack": corpus.cmd_pack,
    "stats": corpus.cmd_stats,
    "splits": splits.cmd_splits,
    "label": corpus.cmd_label,
    "audit-judge": corpus.cmd_audit_judge,
    "export-audit-sample": corpus.cmd_export_audit_sample,
    "usage": corpus.cmd_usage,
    "review": reviews.cmd_review,
    "eval": reviews.cmd_eval,
    "decisions": reviews.cmd_decisions,
    "mine-defects": defects.cmd_mine_defects,
    "improve": improve.cmd_improve,
    "human-labels": human_labels.cmd_human_labels,
    "fetch-local-model": audits.cmd_fetch_local_model,
    "bundle": bundles.cmd_bundle,
    "rehydrate": bundles.cmd_rehydrate,
    "export-gold": bundles.cmd_export_gold,
    "import-benchmark": bundles.cmd_import_benchmark,
}

_MAX_CALLS = "cap on live model calls this run (default: [llm] run_call_cap)"
_WAIT = ("wait out a plan-window stop (the utilization threshold, allowed_warning, a limit with a reset time) and "
         "resume; other stops still end the run")  # fmt: skip


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="honed", description="A self-improving pull request reviewer.")
    parser.add_argument("--config", type=Path, help="path to honed.toml (default: nearest in cwd or parents)")
    parser.add_argument(
        "--data-dir", type=Path, metavar="PATH",
        help="use PATH in place of [paths] data: every [paths] entry under it moves there (store, blobs, clones, "
        "heartbeat, reports), for a run on a copy of the data",
    )  # fmt: skip
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    harvest = sub.add_parser("harvest", help="harvest PRs, review threads and outcomes into the dataset")
    harvest.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME",
                         help="harvest only this corpus repo (repeatable; default: the whole corpus)")  # fmt: skip
    harvest.add_argument("--limit", type=int, help="at most N new PRs per repo in this run")
    pack = sub.add_parser("pack", help="build context packs for harvested PRs (online, one repo at a time)")
    pack.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME", help="only this repo (repeatable)")
    pack.add_argument("--limit", type=int, help="at most N PRs per repo")
    pack.add_argument("--rebuild", action="store_true", help="rebuild packs that already exist")
    pack.add_argument("--rounds", type=int, default=1,
                      help="also build packs at the later review rounds replayed by eval --rounds N")  # fmt: skip
    pack.add_argument("--split", action="append", dest="splits", metavar="SPLIT",
                      help="only the PRs of this split (repeatable: train, validation, test, test-public, "
                      "test-private, or the dev split)")  # fmt: skip
    sub.add_parser("stats", help="counts by repo, language, outcome and author kind")
    split_cmd = sub.add_parser("splits", help="the evaluation splits per language; --assign stores them")
    split_cmd.add_argument("--assign", action="store_true",
                           help="store the split of every PR that has none yet (time-ordered per language, test "
                           "halved into test-public and test-private); stored splits never move")  # fmt: skip
    label = sub.add_parser("label", help="judge threads (addressed, stance, category) and build gold sets")
    label.add_argument(
        "--repo", action="append", dest="repos", metavar="OWNER/NAME", help="only this repo (repeatable)"
    )
    label.add_argument("--limit", type=int, help="at most N PRs per repo")
    label.add_argument("--max-calls", type=int, help=_MAX_CALLS)
    label.add_argument("--rebuild-gold", action="store_true", help="rebuild gold sets that already exist")
    audit = sub.add_parser("audit-judge", help="judge accuracy, Cohen's kappa and consistency (METRICS.md section 5)")
    audit.add_argument("--max-calls", type=int, help=_MAX_CALLS)
    audit.add_argument("--cross-family", action="store_true",
                       help="the local model re-asks a sample of Fable's validity and match questions from stored "
                       "evaluation runs: agreement, Cohen's kappa and the matcher's F1 (no Claude calls)")  # fmt: skip
    audit.add_argument("--split", default="dev", help="--cross-family: the evaluation runs' split (default dev)")
    audit.add_argument("--run", action="append", dest="runs", metavar="ID",
                       help="--cross-family: only this evaluation run (repeatable; default: every complete "
                       "claude_code run of the split)")  # fmt: skip
    audit.add_argument("--n", type=int, help="--cross-family: validity verdicts sampled (default [judge] audit_items)")
    audit.add_argument("--match-rounds", type=int, default=20,
                       help="--cross-family: PR-rounds whose match question is re-asked (default 20)")  # fmt: skip
    sample = sub.add_parser("export-audit-sample", help="a blind, stratified sample of review comments to label")
    sample.add_argument("--n", type=int, help="how many review comments (default: [judge] human_label_items)")
    sample.add_argument("--out", type=Path, help="the JSON file (default: sample.json in [paths] human_labels)")
    sample.add_argument(
        "--ids-from",
        type=Path,
        metavar="SAMPLE",
        help="rebuild the items of this earlier sample (its id, repo and pr) instead of drawing anew",
    )
    sample.add_argument("--force", action="store_true",
                        help="overwrite an existing sample (people's labels are keyed by its ids)")  # fmt: skip
    use = sub.add_parser("usage", help="model calls, tokens and shadow cost per stage, PR and run")
    use.add_argument("--run", help="only this run id")
    use.add_argument("--last", action="store_true", help="only the latest run")

    review = sub.add_parser("review", help="review a stored PR (owner/name#N) or a diff file; nothing is posted")
    review.add_argument("target", help="owner/name#N (a harvested PR) or the path of a unified diff file")
    review.add_argument("--round", type=int, default=1, help="the PR's review round to review at (default 1)")
    review.add_argument("--repo-name", help="for a diff file: the repo it belongs to (default: local)")
    review.add_argument("--title", help="for a diff file: a title for the change")
    review.add_argument("--prior", type=Path, help="re-review: a previous `review --json` output for this PR")
    review.add_argument("--dismissed", help="re-review: comma-separated ids of prior findings a human dismissed")
    review.add_argument("--sample", type=int, default=0, help="model sample index (default 0)")
    output = review.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print the review as JSON")
    output.add_argument("--markdown", action="store_true", help="print the Markdown summary (the default)")

    evaluate = sub.add_parser("eval", help="replay a split with a policy and score it (METRICS.md)")
    evaluate.add_argument("--split", default="dev",
                          help="train, validation, test (all of it), test-public, test-private, or the dev split "
                          "(default dev: only on a store without stored splits)")  # fmt: skip
    evaluate.add_argument("--rounds", type=int, help="review rounds replayed per PR (default: [eval] max_rounds)")
    evaluate.add_argument("--sample", type=int, default=0, help="the reviewer's model sample index (default 0)")
    evaluate.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME", help="only this repo")
    evaluate.add_argument("--limit", type=int, help="at most N PRs per repo")
    evaluate.add_argument("--concurrency", type=int, help="PR-rounds in flight (default: [llm] concurrency)")
    evaluate.add_argument("--incumbent", help="the run to compare with (default: the stored incumbent)")
    evaluate.add_argument("--set-incumbent", action="store_true", help="make this run the incumbent")
    evaluate.add_argument("--fresh", action="store_true", help="don't reuse a stored run with the same key")
    evaluate.add_argument(
        "--sensitivity",
        action="store_true",
        help="noise floor (the policy at --samples model samples) and a weakened policy; writes min_gain to the "
        "report and the decision log",
    )
    evaluate.add_argument("--samples", type=int, default=2,
                          help="--sensitivity: samples of the policy for the noise floor (default 2)")  # fmt: skip
    mine = sub.add_parser("mine-defects", help="escaped-defect gold issues from later bug-fix PRs (online)")
    mine.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME", help="only this repo")
    mine.add_argument("--limit", type=int, default=50, help="merged PRs listed per repo (default 50)")
    decisions = sub.add_parser("decisions", help="the decision log")
    decisions.add_argument("--json", action="store_true", help="also print the rows as JSON")
    loop = sub.add_parser("improve", help="the improve loop: propose, self-review, evaluate, gate, promote")
    loop.add_argument("--rounds", type=int, default=1, help="improve rounds, at most (default 1)")
    loop.add_argument("--min-attempts", type=int, default=0,
                      help="candidates to try before reaching [improve] target_s may stop the loop")  # fmt: skip
    loop.add_argument(
        "--split",
        default="validation",
        help="the gate's split (default validation; the proposer mines a sample of [improve] feed_split); with "
        "the dev split (a store without stored splits), it also feeds the proposer",
    )
    loop.add_argument("--review-rounds", type=int, help="review rounds replayed per PR (default: [eval] max_rounds)")
    loop.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME",
                      help="narrow both splits to this repo (repeatable; for trials)")  # fmt: skip
    loop.add_argument("--limit", type=int, help="narrow both splits to N PRs per repo (for trials)")
    loop.add_argument("--pr", action="append", dest="prs", metavar="OWNER/NAME#N",
                      help="narrow both splits to these PRs (repeatable; for trials)")  # fmt: skip
    loop.add_argument("--ignore-sensitivity", action="store_true",
                      help="run although the latest sensitivity check doesn't separate (loud warning; every "
                      "promotion provisional)")  # fmt: skip
    labels = sub.add_parser("human-labels", help="label the blind audit sample (a local page), or check labels files")
    labels_sub = labels.add_subparsers(dest="human_labels_command", required=True)
    serve = labels_sub.add_parser("serve", help="serve the labeling page on localhost and open it in the browser")
    serve.add_argument("--port", type=int, default=8765, help="the local port (default 8765; 0 picks a free one)")
    serve.add_argument("--no-browser", action="store_true", help="print the page's address without opening it")
    check = labels_sub.add_parser("check", help="validate labels-<username>.json files against the sample")
    check.add_argument("files", nargs="*", type=Path,
                       help="the files (default: every labels-*.json in [paths] human_labels)")  # fmt: skip
    fetch = sub.add_parser("fetch-local-model", help="download the local model ([llm.local] model) into [paths] models")
    fetch.add_argument("--model", help="another Hugging Face repo id")
    bundle = sub.add_parser("bundle", help="the portable dataset: export it to a file, or import one (no code inside)")
    bundle_sub = bundle.add_subparsers(dest="bundle_command", required=True)
    export = bundle_sub.add_parser("export", help="write the dataset to FILE (.jsonl.gz; .jsonl.zst on Python 3.14+)")
    export.add_argument("file", type=Path)
    export.add_argument("--no-licenses", action="store_true",
                        help="don't look up the source repos' licenses on the code host")  # fmt: skip
    export.add_argument("--strip-comments", action="store_true",
                        help="leave review comments' and PR descriptions' text out, keeping attribution and hashes "
                        "(`rehydrate --comments` refetches it)")  # fmt: skip
    export.add_argument("--report", type=Path,
                        help="the export report: redactions and removals by location (default: <name>.report.json "
                        "beside the bundle)")  # fmt: skip
    export.add_argument("--include-private", action="store_true",
                        help="maintainers only: also export the test-private split (left out by default)")  # fmt: skip
    imported = bundle_sub.add_parser("import", help="merge a bundle into the store (idempotent); then `rehydrate`")
    imported.add_argument("file", type=Path)
    rehydrate = sub.add_parser("rehydrate", help="rebuild from git the patches and context packs a bundle left out")
    rehydrate.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME", help="only this repo")
    rehydrate.add_argument("--limit", type=int, help="at most N PRs per repo")
    rehydrate.add_argument("--rebuild", action="store_true", help="rebuild context packs that already exist")
    rehydrate.add_argument("--comments", action="store_true",
                           help="instead, refetch from GitHub the comment text a stripped bundle left out, keeping "
                           "only text that matches its hash")  # fmt: skip
    rehydrate.add_argument("--accept-changed", action="store_true",
                           help="--comments: store text edited or deleted since the bundle anyway")  # fmt: skip
    gold = sub.add_parser("export-gold", help="our gold issues in another benchmark's answer-key shape")
    gold.add_argument("--format", choices=["martian"], default="martian",
                      help="martian: Martian Code Review Bench golden-comment files, one per repo")  # fmt: skip
    gold.add_argument("--split", help="only this split (train, validation, test, or the dev split; default: all)")
    gold.add_argument("--repo", action="append", dest="repos", metavar="OWNER/NAME", help="only this repo")
    gold.add_argument("--out", type=Path, help="the output directory (default: [paths] exports/<format>)")
    gold.add_argument(
        "--important-only",
        action="store_true",
        help="only export gold issues with severity Important (mapped to Martian High)",
    )
    gold.add_argument("--include-private", action="store_true",
                      help="maintainers only: also export the test-private split's gold (left out by "
                      "default)")  # fmt: skip
    bench = sub.add_parser("import-benchmark", help="a public benchmark's PRs and golden comments as test-split gold")
    bench.add_argument("benchmark", choices=sorted(bundles.BENCHMARKS), help="martian or aacr")
    bench.add_argument("--dir", type=Path, help="the downloaded files (default: [paths] benchmarks/<benchmark>)")
    bench.add_argument("--fetch", action="store_true", help="download the benchmark's files first (a few MB)")
    bench.add_argument("--limit", type=int, help="at most N PRs (trials)")

    for model_calls in (label, audit, review, evaluate, loop):
        if model_calls is not label and model_calls is not audit:
            model_calls.add_argument("--max-calls", type=int, help=_MAX_CALLS)
        model_calls.add_argument("--wait-for-reset", action="store_true", help=_WAIT)
    for with_policy in (review, evaluate, loop):
        with_policy.add_argument("--policy", type=Path, help="the policy directory (default: [paths] policy)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # progress reaches a log file or pipe line by line, as it happens
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(line_buffering=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        settings = config.load(args.config)
    except config.ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.data_dir is not None:
        settings = config.relocate_data(settings, args.data_dir)
    try:
        return COMMANDS[args.command](settings, args)
    except BackendRefused as error:  # the LLM backend refused to start (the anthropic backend without an API key)
        print(f"error: {error}", file=sys.stderr)
        return 2
    except SplitError as error:  # an unknown split, or the dev split on a store with stored splits
        print(f"error: {error}", file=sys.stderr)
        return 2
