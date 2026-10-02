"""Commands that move the dataset between machines and bring in held-out data: `bundle export|import`, `rehydrate`
(code, and with `--comments` a stripped bundle's text), `export-gold`, `import-benchmark` (ARCHITECTURE.md sections 6,
8 and 11)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from honed import config
from honed.adapters import bench_aacr, bench_martian, removals_file
from honed.adapters.bundle_file import BundleFileReader, BundleFileWriter
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.codec import to_json
from honed.adapters.github_parse import discussion_url
from honed.adapters.replay_llm import ReplayLLM
from honed.adapters.sqlite_store import SCHEMA_VERSION
from honed.cli.splits import split_keys
from honed.cli.wiring import git_reader, github_host, make_judge, open_store
from honed.core import marks
from honed.core.benchmarks import AACR, MARTIAN
from honed.core.filters import is_excluded
from honed.core.removals import Removals, RemovalsError
from honed.core.types import GoldProvenance, PRKey, PRSource
from honed.learn import bundle, gold_export, splits
from honed.learn.benchmarks import BenchmarkImporter, ImportOptions
from honed.learn.bundle_text import ExportReport, TextOptions, TextPolicy
from honed.learn.judge import PROMPTS
from honed.learn.rehydrate import Rehydrator
from honed.learn.rehydrate_text import TextRehydrator
from honed.ports.benchmark import BenchmarkError, BenchmarkSource
from honed.ports.bundle import BundleError, JudgeVersion
from honed.ports.code_host import HostError
from honed.ports.code_reader import ReaderUnavailable

BENCHMARKS = {
    MARTIAN: (bench_martian.MartianBenchmark, bench_martian.LICENSE, bench_martian.HOMEPAGE, bench_martian.REPO),
    AACR: (bench_aacr.AACRBenchmark, bench_aacr.LICENSE, bench_aacr.HOMEPAGE, bench_aacr.REPO),
}


def _version() -> str:
    try:
        return metadata.version("honed")
    except metadata.PackageNotFoundError:
        return "unknown"


# ---- bundle ---------------------------------------------------------------------------------------------------


def cmd_bundle(settings: config.Settings, args: argparse.Namespace) -> int:
    return _export(settings, args) if args.bundle_command == "export" else _import(settings, args)


def _licenses(settings: config.Settings, args: argparse.Namespace) -> Callable[[str], str | None]:
    """The source repos' licenses from the code host (one lookup per repo), or none offline / with --no-licenses."""
    if args.no_licenses or settings.offline:
        return lambda repo: None
    host = github_host(settings)
    found: dict[str, str | None] = {}

    def license_of(repo: str) -> str | None:
        if repo not in found:
            try:
                found[repo] = host.repo_info(repo).license
            except HostError as error:
                print(f"warning: no license for {repo}: {error}", file=sys.stderr)
                found[repo] = None
        return found[repo]

    return license_of


def _removals(settings: config.Settings) -> Removals:
    """The removal list every export honors (`[paths] removals_file`)."""
    return removals_file.load(settings.paths.removals_file)


def judge_version(settings: config.Settings, calls: SqliteCallStore) -> JudgeVersion:
    """The fixed judge, from the yardstick: model, effort, each prompt file's hash, and the evaluation fingerprint."""
    judge = settings.models.judge
    prompts = {name: hashlib.sha256((settings.paths.yardstick_prompts / f"{name}.md").read_bytes()).hexdigest()
               for name in PROMPTS}  # fmt: skip
    fingerprint = make_judge(settings, ReplayLLM(calls)).fingerprint  # the LLM is never called
    return JudgeVersion(judge.online, judge.effort or "", prompts, f"{judge.online}:{fingerprint}")


def report_path(bundle_file: Path) -> Path:
    """`<name>.report.json` beside the bundle (`name.jsonl.gz` -> `name.report.json`)."""
    name = bundle_file.name
    for suffix in (".jsonl.gz", ".jsonl.zst", ".jsonl.zstd", ".gz", ".zst", ".zstd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return bundle_file.with_name(f"{name}.report.json")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as tmp:
        tmp.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp.name, path)


def _export(settings: config.Settings, args: argparse.Namespace) -> int:
    try:
        removed = _removals(settings)
    except RemovalsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    store, calls = open_store(settings), SqliteCallStore(settings.paths.sqlite)
    options = bundle.ExportOptions(
        store_schema=SCHEMA_VERSION, judge_model=settings.models.judge.online, generator=f"honed {_version()}",
        fractions=settings.eval.split_fractions, excluded=settings.corpus.excluded,
        min_validation=settings.gate.min_prs_per_language, include_private=args.include_private,
        benchmarks={name: (lic, home) for name, (_, lic, home, _) in BENCHMARKS.items()},
        dataset_version=settings.dataset.version, judge=judge_version(settings, calls),
        annotations_license=settings.dataset.annotations_license, removals=removed,
        text=TextOptions(strip=args.strip_comments, link=discussion_url),
    )  # fmt: skip
    try:
        result = bundle.export(store, store, store, store, calls, BundleFileWriter(args.file), options,
                               _licenses(settings, args))  # fmt: skip
    except BundleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        calls.close()
        store.close()
    manifest, report = result.manifest, result.report
    report_file = args.report or report_path(args.file)
    _write_json(report_file, {"bundle": args.file.name, "dataset_version": manifest.dataset_version,
                              "comment_text": manifest.comment_text, "redaction_rules": manifest.redaction_rules,
                              "redaction_counts": report.redaction_counts, "removals": dict(manifest.removals),
                              **to_json(report)})  # fmt: skip
    size = args.file.stat().st_size
    print(f"wrote {args.file} ({size / 2**20:.1f} MB, dataset {manifest.dataset_version}, bundle schema "
          f"{manifest.bundle_schema}, store schema {manifest.store_schema}): "
          + ", ".join(f"{k} {v}" for k, v in manifest.counts.items()))  # fmt: skip
    print("source repos: " + ", ".join(f"{r.repo} ({r.license or 'license not looked up'}, {r.prs} PRs)"
                                       for r in manifest.repos))  # fmt: skip
    for b in manifest.benchmarks:
        print(f"benchmark {b.name}: {b.prs} PRs ({b.license}, {b.homepage})")
    judge = manifest.judge
    if judge is not None:
        print(f"judge {judge.fingerprint} (effort {judge.effort or 'default'}); prompts "
              + ", ".join(f"{name} {digest[:12]}" for name, digest in judge.prompts.items()))  # fmt: skip
    redactions = ", ".join(f"{k} {v}" for k, v in manifest.redactions.items()) or "none"
    print(f"redactions (rules {manifest.redaction_rules}): {redactions}")
    print("removals: " + ", ".join(f"{k} {v}" for k, v in manifest.removals.items()))
    if manifest.test_private == "included":
        print(f"test-private: INCLUDED ({manifest.test_private_prs} PRs; --include-private): a maintainers' bundle, "
              "never to be published")  # fmt: skip
    else:
        print(f"test-private: left out ({report.private_prs} PRs and {report.private_answers} judge answers; "
              "`--include-private` keeps them, for maintainers)")  # fmt: skip
    print(f"comment text: {manifest.comment_text}"
          + (f" ({report.stripped_texts} texts left out; `honed rehydrate --comments` refetches them)"
             if manifest.comment_text == "stripped" else " (with attribution and text hashes)"))  # fmt: skip
    print(f"export report: {report_file}")
    print("no code: patches, diff hunks and context packs are left out (`honed rehydrate` rebuilds them)")
    return 0


def _import(settings: config.Settings, args: argparse.Namespace) -> int:
    try:
        reader = BundleFileReader(args.file)
    except (BundleError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    store, calls = open_store(settings), SqliteCallStore(settings.paths.sqlite)
    try:
        report = bundle.import_bundle(reader, store, store, store, store, calls)
        waiting = [store.stripped(k) for k in store.stripped_keys()]
    except BundleError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        calls.close()
        store.close()
    m = report.manifest
    print(f"{args.file}: bundle schema {m.bundle_schema}, made {m.created_at[:10]} by {m.generator}, judge "
          f"{m.judge_model}; {len(m.repos)} source repos" + (f", benchmarks {[b.name for b in m.benchmarks]}"
                                                             if m.benchmarks else ""))  # fmt: skip
    kinds = sorted(set(report.added) | set(report.kept))
    print("imported: " + ", ".join(f"{k} {report.added[k]}" + (f" ({report.kept[k]} already here)"
                                                               if report.kept[k] else "") for k in kinds))  # fmt: skip
    if m.dataset_version or m.comment_text != "full":
        print(f"dataset {m.dataset_version or '(unversioned)'}, comment text {m.comment_text}, redactions "
              + (", ".join(f"{k} {v}" for k, v in m.redactions.items()) or "none"))  # fmt: skip
    if report.mismatched:
        print(f"warning: {len(report.mismatched)} texts don't match their hash: "
              + ", ".join(report.mismatched[:10]), file=sys.stderr)  # fmt: skip
    code = sum(any(map(marks.is_code, w)) for w in waiting)
    text = sum(any(map(marks.is_text, w)) for w in waiting)
    if code:
        print(f"{code} PRs wait for their code: run `honed rehydrate` (needs git access to their repos once)")
    if text:
        print(f"{text} PRs wait for their comment text: run `honed rehydrate --comments` (GitHub's API: per repo, "
              "a query per 100 comments and one per 50 PR descriptions)")  # fmt: skip
    return 0


# ---- rehydrate ------------------------------------------------------------------------------------------------


def cmd_rehydrate(settings: config.Settings, args: argparse.Namespace) -> int:
    """Rebuild, from git, the patches and context packs a bundle left out (and packs that are missing); with
    `--comments`, refetch from GitHub the text a stripped bundle left out."""
    if args.comments:
        return _rehydrate_text(settings, args)
    store = open_store(settings)
    wanted = {r.lower() for r in args.repos} if args.repos else None
    pending = {k for k in store.stripped_keys() if marks.code_marks(store.stripped(k))}
    facts = {PRKey(f.repo, f.number): f for f in store.pr_facts()}
    per_repo: dict[str, list[PRKey]] = {}
    for key, fact in facts.items():
        if wanted is not None and key.repo.lower() not in wanted:
            continue
        if is_excluded(key.repo, settings.corpus.excluded) and fact.source is not PRSource.BENCHMARK:
            continue
        if key in pending or args.rebuild or store.get_pack(key) is None:
            per_repo.setdefault(key.repo, []).append(key)
    keys = [k for group in per_repo.values() for k in (group[: args.limit] if args.limit else group)]
    if not keys:
        store.close()
        print("nothing to rehydrate: every PR has its patches and its context pack")
        return 0
    try:
        reports = Rehydrator(store, lambda repo: git_reader(settings, repo), settings.harvest.pack,
                             rebuild=args.rebuild).run(keys)  # fmt: skip
    except ReaderUnavailable as error:
        print(f"stopped: git can't reach a repo ({error}); run again to resume", file=sys.stderr)
        return 3
    finally:
        store.close()
    for r in reports:
        print(f"{r.repo}: patches restored on {r.patched} PRs ({r.patches} files), {r.packs} packs, "
              f"{r.round_packs} round packs, {len(r.failed)} failed")  # fmt: skip
        for item in r.unrestored[:10]:
            print(f"  no patch from git for {item}")
        for pr, error in r.failed.items():
            print(f"  failed {pr}: {error[:200]}")
    return 3 if any(r.failed for r in reports) else 0


def _rehydrate_text(settings: config.Settings, args: argparse.Namespace) -> int:
    store = open_store(settings)
    wanted = {r.lower() for r in args.repos} if args.repos else None
    per_repo: dict[str, list[PRKey]] = {}
    for key in store.stripped_keys():
        if (wanted is None or key.repo.lower() in wanted) and marks.text_marks(store.stripped(key)):
            per_repo.setdefault(key.repo, []).append(key)
    keys = [k for group in per_repo.values() for k in (group[: args.limit] if args.limit else group)]
    if not keys:
        store.close()
        print("nothing to rehydrate: no PR waits for comment text")
        return 0
    try:
        report = TextRehydrator(store, github_host(settings), accept_changed=args.accept_changed).run(keys)
    except HostError as error:
        print(f"stopped: GitHub didn't answer ({error}); run again to resume", file=sys.stderr)
        return 3
    finally:
        store.close()
    print(f"comment text: {report.prs} PRs, {report.restored} texts restored with a matching hash, "
          f"{len(report.changed)} changed since the bundle, {len(report.missing)} gone from GitHub"
          + (f", {report.accepted} stored anyway (--accept-changed)" if args.accept_changed else "")
          + f"; {len(report.done)} PRs complete")  # fmt: skip
    for where in [*report.changed, *report.missing][:20]:
        print(f"  hash mismatch or missing: {where}")
    if (report.changed or report.missing) and not args.accept_changed:
        print("those PRs stay unreplayed: their labels were made on other text. `--accept-changed` stores the "
              "current text (empty when deleted) and releases them")  # fmt: skip
        return 3
    return 0


# ---- export-gold ----------------------------------------------------------------------------------------------


def cmd_export_gold(settings: config.Settings, args: argparse.Namespace) -> int:
    """Our gold issues in another benchmark's answer-key shape (Martian's golden comments)."""
    try:
        removed = _removals(settings)
    except RemovalsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    store = open_store(settings)
    report = ExportReport()
    try:
        facts = store.pr_facts()
        private = 0 if args.include_private else sum(f.split == splits.TEST_PRIVATE for f in facts)
        keys = [PRKey(f.repo, f.number) for f in facts if f.source is PRSource.CORPUS
                and (args.include_private or f.split != splits.TEST_PRIVATE)]  # fmt: skip
        if args.split:
            chosen = set(split_keys(settings, store, args.split))
            keys = [k for k in keys if k in chosen]
        if args.repos:
            wanted = {r.lower() for r in args.repos}
            keys = [k for k in keys if k.repo.lower() in wanted]
        result = gold_export.martian(store, store, keys, removed, TextPolicy(TextOptions(), report))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        store.close()
    out = args.out or settings.paths.exports / args.format
    out.mkdir(parents=True, exist_ok=True)
    for name, entries in sorted(result.files.items()):
        _write_json(out / name, entries)
    stale = sorted(p.name for p in out.glob("*.json") if p.name not in result.files)
    print(f"wrote {len(result.files)} files to {out}: {result.prs} PRs, {result.issues} golden comments (Martian "
          f"shape); left out: {result.clean} PRs whose gold set is empty, {result.no_gold} without a gold set, "
          f"{result.removed} removed PRs and gold issues"
          + (f", {private} test-private PRs (`--include-private` keeps them)" if private else ""))  # fmt: skip
    if report.redactions:
        print("redactions: " + ", ".join(f"{k} {v}" for k, v in report.redaction_counts.items()))
    if stale:
        print(f"warning: {out} also holds files this export didn't write (Martian's loader reads every *.json): "
              + ", ".join(stale[:10]), file=sys.stderr)  # fmt: skip
    return 0


# ---- benchmarks -----------------------------------------------------------------------------------------------


def _fetch(settings: config.Settings, name: str, directory: Path) -> None:
    """Download the benchmark's files from its GitHub repo into `directory` (a few MB)."""
    _, _, _, repo = BENCHMARKS[name]
    files = bench_martian.FILES if name == MARTIAN else bench_aacr.FILES
    host = github_host(settings)
    ref = host.repo_info(repo).default_branch or "main"
    paths = [f.format(name=g) for f in files for g in (bench_martian.GOLDEN if "{name}" in f else ("",))]
    for path in paths:
        text = host.read_file(repo, path, ref)
        if text is None:
            raise BenchmarkError(f"{repo}: {path} not found at {ref}")
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        print(f"fetched {repo}/{path} ({len(text) / 2**20:.1f} MB)")


def cmd_import_benchmark(settings: config.Settings, args: argparse.Namespace) -> int:
    factory, license_, homepage, _ = BENCHMARKS[args.benchmark]
    directory = args.dir or settings.paths.benchmarks / args.benchmark
    store = open_store(settings)
    try:
        if args.fetch:
            _fetch(settings, args.benchmark, directory)
        source: BenchmarkSource = factory(directory)
        options = ImportOptions(
            github_languages=settings.corpus.github_languages, fallback_language=settings.corpus.fallback_language,
            conf=settings.metrics.gold_conf[GoldProvenance.BENCHMARK], limit=args.limit,
        )  # fmt: skip
        report = BenchmarkImporter(github_host(settings), store, store, options).run(source)
    except (BenchmarkError, HostError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(f"{args.benchmark} ({license_}, {homepage}): {report.listed} PRs listed, {report.imported} imported as "
          f"test-split gold: {report.gold_issues} gold issues; {report.rejected_comments} rejected comments kept "
          f"in the benchmark record; {report.without_gold} PRs without a golden comment; {len(report.failed)} failed"
          + (f"; STOPPED: {report.stopped}" if report.stopped else ""))  # fmt: skip
    for pr, error in sorted(report.failed.items()):
        print(f"  failed {pr}: {error[:200]}")
    print("context packs: `honed rehydrate` (clones each benchmark repo once)")
    return 3 if report.stopped or report.failed else 0
