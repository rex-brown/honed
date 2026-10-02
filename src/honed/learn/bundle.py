"""The portable dataset (ARCHITECTURE.md section 8): `honed bundle export FILE` and `bundle import FILE`.

Export writes every stored PR with its review threads, outcomes, judgments, judged labels, gold set and round gold,
the splits, escaped defects, the decision log, policy versions, benchmark records, and the judge's cached answers
(`ports.bundle`). Code stays out: every patch and diff hunk is removed (each PR record lists which files had a patch),
and context packs are not exported; later review rounds keep only their diff's file list and merge base.

Before anything is written, the removal list (`core.removals`, `yardstick/removals.json`) takes out the listed PRs
and the review threads that hold a listed comment, with everything derived from them (their judge answers too, as
far as the usage ledger ties them to a PR); a guard refuses any record that would still carry one. Quoted text then
goes through `learn/bundle_text.py`: the secrets and personal-data scan, attribution with a text hash, and with
`--strip-comments` the text left out. The manifest records the dataset version, the judge (model, effort, prompt
hashes), the license split, the redaction rules and counts, and the removals applied; the export report lists every
redaction and removal by location.

Import is idempotent: a PR already in the store is kept as it is (it has its code), judgments, labels and gold are
replaced by key, decision-log rows already present are skipped, cached answers already cached are kept, and every
PR's split is fixed as the bundle's maker had it. Imported PRs are marked as missing their patches (and, from a
stripped bundle, their text) until `honed rehydrate` restores them from git (`learn/rehydrate.py`) and
`honed rehydrate --comments` from the code host (`learn/rehydrate_text.py`); until then they are not reviewed. A
comment whose text doesn't match its hash is reported.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from honed.core import marks, redaction, removals
from honed.core.removals import Applied, Removals
from honed.core.types import Compare, HarvestedPR, PRFact, PRKey
from honed.learn import splits
from honed.learn.bundle_text import ExportReport, TextOptions, TextPolicy
from honed.ports.bundle import (
    BUNDLE_SCHEMA,
    BenchmarkRecord,
    BundleError,
    BundleReader,
    BundleWriter,
    CachedAnswerRecord,
    DecisionRecord,
    DefectRecord,
    GoldRecord,
    JudgedLabelsRecord,
    JudgeVersion,
    JudgmentRecord,
    LicenseSplit,
    Manifest,
    PolicyVersionRecord,
    PRRecord,
    Record,
    RoundDiffRecord,
    RoundGoldRecord,
    SourceBenchmark,
    SourceRepo,
    SplitRecord,
)
from honed.ports.call_store import CallStore
from honed.ports.store import EvalStore, ImproveStore, LabelStore, Store

NOTE = ("No code: patches, diff hunks and context packs are left out; `honed rehydrate` rebuilds them from git. "
        "Review comments and PR text are quoted from public pull requests with attribution (author login, URL); "
        "they remain their authors' and are not covered by the annotations' license. See DATASET.md.")  # fmt: skip
COMMENT_TEXT_TERMS = ("not licensed by this project: quoted from public GitHub pull requests with attribution "
                      "(each comment's author login and URL); copyright stays with each author")  # fmt: skip
BENCHMARK_TERMS = "each benchmark's own license (see `benchmarks`)"
SOFTWARE_TERMS = "Apache-2.0 (honed itself; no code is in the bundle)"


@dataclass(frozen=True)
class ExportOptions:
    store_schema: int
    judge_model: str  # the judge's cached answers go in the bundle
    generator: str
    fractions: Mapping[str, float]  # `[eval] split_fractions`, for PRs whose split isn't fixed yet
    excluded: Collection[str]
    benchmarks: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # name -> (license, homepage)
    dataset_version: str = ""
    judge: JudgeVersion | None = None
    annotations_license: str = "CC-BY-4.0"
    removals: Removals = field(default_factory=Removals)
    text: TextOptions = field(default_factory=TextOptions)
    min_validation: int = 0  # `[gate] min_prs_per_language`: validation's floor per language, for those PRs too
    include_private: bool = False  # also export the test-private split (maintainers: `--include-private`)


@dataclass(frozen=True)
class ExportResult:
    manifest: Manifest
    report: ExportReport


@dataclass
class ImportReport:
    manifest: Manifest
    added: Counter[str] = field(default_factory=Counter)  # records applied, by kind
    kept: Counter[str] = field(default_factory=Counter)  # records whose key was already there, left as it was
    mismatched: list[str] = field(default_factory=list)  # quoted texts whose hash doesn't match: where


# ---- stripping code -------------------------------------------------------------------------------------------


def strip_compare(compare: Compare) -> Compare:
    return replace(compare, files=tuple(replace(f, patch=None) for f in compare.files))


def patched_files(compare: Compare, prefix: str) -> set[str]:
    return {f"{prefix}{f.path}" for f in compare.files if f.patch is not None}


def strip_pr(item: HarvestedPR, pending: Iterable[str] = ()) -> PRRecord:
    """The PR without code: no patches, no diff hunks. `pending` lists the files of a PR that was itself imported and
    not yet rehydrated, so they are listed as patched again."""
    pr = item.pr
    patched = {p for p in pending if p.startswith(("reviewed:", "thread:"))}
    reviewed = pr.reviewed_diff
    if reviewed is not None:
        patched |= patched_files(reviewed, "reviewed:")
        reviewed = strip_compare(reviewed)
    compares = []
    for n, compare in enumerate(pr.compares):
        patched |= patched_files(compare, f"thread:{n}:")
        compares.append(strip_compare(compare))
    threads = tuple(replace(t, comments=tuple(replace(c, diff_hunk="") for c in t.comments)) for t in pr.threads)
    bare = replace(pr, threads=threads, compares=tuple(compares), reviewed_diff=reviewed)
    return PRRecord(replace(item, pr=bare), tuple(sorted(patched)))


# ---- export ---------------------------------------------------------------------------------------------------


def _pr_of(record: Record) -> PRKey | None:
    match record:
        case PRRecord(item=item):
            return item.key
        case JudgmentRecord(judgment=j):
            return PRKey(j.repo, j.number)
        case GoldRecord(gold=gold) | RoundGoldRecord(gold=gold):
            return gold.key
        case JudgedLabelsRecord(pr=key) | RoundDiffRecord(pr=key) | SplitRecord(pr=key) | BenchmarkRecord(pr=key):
            return key
        case DefectRecord(defect=defect):
            return defect.pr
    return None


class _Refusing:
    """The bundle writer behind a last check: a record of a removed PR, or a PR record still holding a removed
    comment, stops the export (`BundleError`) before it can reach the file."""

    def __init__(self, sink: BundleWriter, removed: Removals) -> None:
        self._sink = sink
        self._removed = removed

    def write(self, record: Record) -> None:
        key = _pr_of(record)
        if key is not None and self._removed.removes_pr(key):
            raise BundleError(f"refusing to export {key}: it is on the removal list")
        if isinstance(record, PRRecord):
            held = {c.id for t in record.item.pr.threads for c in t.comments} & self._removed.comments
            if held:
                raise BundleError(f"refusing to export {key}: it holds removed comments {sorted(held)}")
        self._sink.write(record)

    def close(self, manifest: Manifest) -> Manifest:
        return self._sink.close(manifest)


def _ledger_prs(calls: CallStore) -> dict[str, set[str]]:
    """Cache key -> the PRs the usage ledger ties it to."""
    out: dict[str, set[str]] = {}
    for entry in calls.ledger():
        if entry.pr:
            out.setdefault(entry.key, set()).add(entry.pr)
    return out


def export(store: Store, labels: LabelStore, evals: EvalStore, improve: ImproveStore, calls: CallStore,
           sink: BundleWriter, options: ExportOptions,
           license_of: Callable[[str], str | None] = lambda repo: None) -> ExportResult:  # fmt: skip
    run = _Export(store, labels, calls, _Refusing(sink, options.removals), options)
    facts = store.pr_facts()
    keys = [PRKey(f.repo, f.number) for f in facts]
    for key in keys:
        run.pr(key)
    run.splits(facts, keys)
    for defect in labels.escaped_defects():
        if not options.removals.removes_pr(defect.pr) and defect.pr not in run.private:
            run.sink.write(DefectRecord(run.text.defect(defect)))
    for decision in evals.decisions():
        run.sink.write(DecisionRecord(decision))
    for version in improve.policy_versions():
        run.sink.write(PolicyVersionRecord(version))
    run.benchmarks()
    run.cached_answers()
    return ExportResult(run.sink.close(run.manifest(keys, license_of)), run.report)


class _Export:
    """One export's state: what the removal list took out, the text policy with its report, the guarded writer."""

    def __init__(self, store: Store, labels: LabelStore, calls: CallStore, sink: BundleWriter,
                 options: ExportOptions) -> None:  # fmt: skip
        self.store, self.labels, self.calls, self.sink, self.o = store, labels, calls, sink, options
        self.removed, self.applied, self.report = options.removals, Applied(), ExportReport()
        self.text = TextPolicy(options.text, self.report)
        self.touched: set[str] = set()  # PRs whose judge answers may quote something removed
        self.benchmark_counts: Counter[str] = Counter()
        self.private = {PRKey(f.repo, f.number) for f in store.pr_facts() if f.split == splits.TEST_PRIVATE}
        self.held_out = set() if options.include_private else self.private  # the holdout, left out with its answers

    def pr(self, key: PRKey) -> None:
        """The PR's record and everything about it, without what the removal list takes out."""
        if self.removed.removes_pr(key):
            self.applied.prs.add(key)
            self.touched.add(str(key))
            return
        if key in self.held_out:
            self.report.private_prs += 1
            return
        item = self.store.get_pr(key)
        if item is None:
            return
        item, dropped, found = removals.filter_pr(item, self.removed)
        if dropped:
            self.applied.threads |= dropped
            self.applied.comments |= found
            self.touched.add(str(key))
        pending = self.store.stripped(key)
        record = strip_pr(item, pending)
        still_out = dict(map(marks.parse_text, marks.text_marks(pending)))  # text a stripped bundle left out
        quoted, comments, description = self.text.quote_pr(record.item, still_out)
        self.sink.write(replace(record, item=quoted, comments=comments, description=description))
        for judgment in self.labels.judgments(key):
            if judgment.thread_id not in dropped:
                self.sink.write(JudgmentRecord(self.text.judgment(judgment)))
        judged = [lab for lab in self.labels.judged_labels(key) if lab.thread_id not in dropped]
        if judged:
            self.sink.write(JudgedLabelsRecord(key, tuple(judged)))
        gold = self.labels.get_gold(key)
        if gold is not None:
            gold, gone = removals.filter_gold(gold, dropped)
            self.applied.gold_issues += gone
            self.sink.write(GoldRecord(self.text.gold(gold)))
        for round_gold, rounds in self.labels.round_golds(key):
            round_gold, _ = removals.filter_gold(round_gold, dropped)
            what = f"round gold {round_gold.reviewed_commit[:12]}"
            self.sink.write(RoundGoldRecord(self.text.gold(round_gold, what), rounds))
        for diff in self.store.round_diffs(key):
            prefix = f"round:{diff.head}:"
            patched = patched_files(diff, prefix) | {p for p in pending if p.startswith(prefix)}
            self.sink.write(RoundDiffRecord(key, strip_compare(diff), tuple(sorted(patched))))

    def splits(self, facts: Sequence[PRFact], keys: Sequence[PRKey]) -> None:
        """Every PR's split, assigned with the removed PRs still counted, so a removal moves no other PR."""
        has_gold = {k for k in keys if self.labels.get_gold(k) is not None}
        assigned = splits.assign(splits.eligible(facts, has_gold, self.o.excluded), self.o.fractions,
                                 min_validation=self.o.min_validation)  # fmt: skip
        for key, split in sorted(assigned.items(), key=lambda kv: (kv[0].repo, kv[0].number)):
            if not self.removed.removes_pr(key) and key not in self.held_out:
                self.sink.write(SplitRecord(key, split))

    def benchmarks(self) -> None:
        for key, record in self.store.benchmark_prs():
            if self.removed.removes_pr(key):
                self.applied.prs.add(key)
                continue
            self.sink.write(BenchmarkRecord(key, record))
            self.benchmark_counts[record.benchmark] += 1

    def cached_answers(self) -> None:
        """The judge's cached answers, redacted, but for those the usage ledger ties to a PR a removal touched or
        to a test-private PR left out."""
        private = {str(k) for k in self.held_out}
        tied = _ledger_prs(self.calls) if self.touched or private else {}
        for entry in self.calls.cached([self.o.judge_model]):
            prs = tied.get(entry.key, set())
            if prs & private:
                self.report.private_answers += 1
            elif not prs & self.touched:
                self.sink.write(CachedAnswerRecord(self.text.cached(entry)))

    def manifest(self, keys: Sequence[PRKey], license_of: Callable[[str], str | None]) -> Manifest:
        o, report = self.o, self.report
        report.removed_prs = sorted(str(k) for k in self.applied.prs)
        report.removed_comments = sorted(self.applied.comments)
        report.removed_threads = sorted(self.applied.threads)
        per_repo = Counter(k.repo for k in keys if not self.removed.removes_pr(k) and k not in self.held_out)
        return Manifest(
            bundle_schema=BUNDLE_SCHEMA, store_schema=o.store_schema, created_at=dt.datetime.now(dt.UTC).isoformat(),
            generator=o.generator, judge_model=o.judge_model,
            repos=tuple(SourceRepo(repo, license_of(repo), n) for repo, n in sorted(per_repo.items())),
            benchmarks=tuple(SourceBenchmark(name, *o.benchmarks.get(name, ("unknown", "")), n)
                             for name, n in sorted(self.benchmark_counts.items())),
            note=NOTE, dataset_version=o.dataset_version, judge=o.judge,
            licenses=LicenseSplit(o.annotations_license, COMMENT_TEXT_TERMS, BENCHMARK_TERMS, SOFTWARE_TERMS),
            comment_text="stripped" if o.text.strip else "full", redaction_rules=redaction.RULES_VERSION,
            redactions=report.redaction_counts, removals=self.applied.counts(self.removed),
            test_private="included" if o.include_private else "excluded",
            test_private_prs=len(self.private - self.applied.prs),
        )  # fmt: skip


# ---- import ---------------------------------------------------------------------------------------------------


def import_bundle(source: BundleReader, store: Store, labels: LabelStore, evals: EvalStore, improve: ImproveStore,
                  calls: CallStore) -> ImportReport:  # fmt: skip
    report = ImportReport(source.manifest())
    logged = {_decision_key(d) for d in evals.decisions()}
    new_prs: set[PRKey] = set()
    for record in source.records():
        if isinstance(record, PRRecord):
            report.mismatched += mismatched_texts(record)
        kind, added = _apply(record, store, labels, evals, improve, calls, logged, new_prs)
        (report.added if added else report.kept)[kind] += 1
    return report


def text_marks(record: PRRecord) -> list[str]:
    """The marks for the text a stripped bundle left out: each comment's, and the description's."""
    out = [marks.comment_mark(a.id, a.text_sha256) for a in record.comments if a.stripped]
    if record.description is not None and record.description.stripped:
        out.append(marks.body_mark(record.description.text_sha256))
    return out


def mismatched_texts(record: PRRecord) -> list[str]:
    """Where a PR record's text doesn't hash to its attribution's `text_sha256` (a damaged or edited bundle)."""
    bodies = {c.id: c.body for t in record.item.pr.threads for c in t.comments}
    out = [f"{record.item.key} comment {a.id}" for a in record.comments
           if not a.stripped and a.id in bodies and redaction.sha256(bodies[a.id]) != a.text_sha256]  # fmt: skip
    d = record.description
    if d is not None and not d.stripped and redaction.sha256(record.item.pr.body) != d.text_sha256:
        out.append(f"{record.item.key} body")
    return out


def _decision_key(d: object) -> tuple[str, ...]:
    return tuple(str(getattr(d, f)) for f in ("at", "hypothesis", "change", "delta", "verdict"))


def _apply(record: object, store: Store, labels: LabelStore, evals: EvalStore, improve: ImproveStore,
           calls: CallStore, logged: set[tuple[str, ...]], new_prs: set[PRKey]) -> tuple[str, bool]:  # fmt: skip
    """Apply one record; returns its kind and whether it changed the store."""
    match record:
        case PRRecord(item=item, patched=patched):
            if store.has_pr(item.key):
                return "pr", False
            store.upsert_pr(item)
            store.mark_stripped(item.key, [*patched, *text_marks(record)])
            new_prs.add(item.key)
            return "pr", True
        case JudgmentRecord(judgment=judgment):
            labels.save_judgment(judgment)
            return "judgment", True
        case JudgedLabelsRecord(pr=key, labels=judged):
            labels.save_judged_labels(key, judged)
            return "judged_labels", True
        case GoldRecord(gold=gold):
            labels.save_gold(gold)
            return "gold", True
        case RoundGoldRecord(gold=gold, rounds=rounds):
            labels.save_round_gold(gold, rounds)
            return "round_gold", True
        case RoundDiffRecord(pr=key, diff=diff, patched=patched):
            if store.get_round_pack(key, diff.head) is not None:
                return "round_diff", False  # the round pack is here, with its code
            store.save_round_diff(key, diff)
            store.mark_stripped(key, [*store.stripped(key), *patched])
            return "round_diff", True
        case SplitRecord(pr=key, split=split):
            if not store.has_pr(key):
                return "split", False
            store.set_split(key, split)
            return "split", True
        case DefectRecord(defect=defect):
            labels.save_escaped_defect(defect)
            return "escaped_defect", True
        case DecisionRecord(decision=decision):
            key = _decision_key(decision)
            if key in logged:
                return "decision", False
            evals.add_decision(replace(decision, id=None))
            logged.add(key)
            return "decision", True
        case PolicyVersionRecord(version=version):
            known = improve.policy_version(version.hash) is not None
            improve.save_policy_version(version)
            return "policy_version", not known
        case BenchmarkRecord(pr=key, record=benchmark):
            store.save_benchmark_pr(benchmark, key)
            return "benchmark", True
        case CachedAnswerRecord(entry=entry):
            return "cached_answer", calls.add_cached(entry)
    raise TypeError(f"unknown bundle record {type(record).__name__}")


def store_counts(store: Store, labels: LabelStore, evals: EvalStore, improve: ImproveStore, calls: CallStore,
                 judge_model: str, keys: Sequence[PRKey] | None = None) -> dict[str, int]:  # fmt: skip
    """What a bundle of this store would hold, by record kind (to check an import against its source)."""
    keys = list(keys if keys is not None else store.pr_keys())
    counts: Counter[str] = Counter()
    for key in keys:
        item = store.get_pr(key)
        if item is None:
            continue
        counts["pr"] += 1
        counts["thread"] += len(item.pr.threads)
        counts["comment"] += sum(len(t.comments) for t in item.pr.threads)
        counts["judgment"] += len(labels.judgments(key))
        counts["judged_labels"] += bool(labels.judged_labels(key))
        gold = labels.get_gold(key)
        counts["gold"] += gold is not None
        counts["gold_issue"] += len(gold.issues) if gold is not None else 0
        counts["round_gold"] += len(labels.round_golds(key))
    counts["escaped_defect"] = len(labels.escaped_defects())
    counts["decision"] = len(evals.decisions())
    counts["policy_version"] = len(improve.policy_versions())
    counts["benchmark"] = len(store.benchmark_prs())
    counts["cached_answer"] = len(calls.cached([judge_model]))
    return dict(sorted(counts.items()))
