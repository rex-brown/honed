"""`honed import-benchmark`: a public benchmark's PRs into the store as held-out test data (ARCHITECTURE.md
sections 6 and 11; `core/benchmarks.py` for the mapping).

For each of the benchmark's PRs: the PR's metadata, commits and reviews from the code host, and the diff it reviewed
(the benchmark's pinned commits when it has them, else the PR's base and head); stored with `source = "benchmark"`,
the fixed split `test`, no review threads of its own, and the benchmark's golden comments as its gold set (provenance
`benchmark`); the benchmark's record (golden and rejected comments) beside it. Context packs come from
`honed rehydrate`. Re-importing replaces in place. Needs the network (the code host).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from honed.core import filters
from honed.core.benchmarks import TEST_SPLIT, BenchmarkPR, gold_set
from honed.core.types import Corpus, HarvestedPR, PRSource
from honed.ports.benchmark import BenchmarkSource
from honed.ports.code_host import BudgetExhausted, CodeHost, HostError, NotFound
from honed.ports.store import LabelStore, Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportOptions:
    github_languages: Mapping[str, str]  # GitHub language -> our language group
    fallback_language: str
    conf: float  # `[metrics.gold_conf] benchmark`
    limit: int | None = None  # at most this many PRs (trials)


@dataclass
class ImportReport:
    benchmark: str
    listed: int = 0
    imported: int = 0
    gold_issues: int = 0
    rejected_comments: int = 0  # kept in the benchmark record, never gold
    without_gold: int = 0  # PRs the benchmark's annotators confirmed no comment on
    failed: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None


class BenchmarkImporter:
    def __init__(self, host: CodeHost, store: Store, labels: LabelStore, options: ImportOptions) -> None:
        self._host = host
        self._store = store
        self._labels = labels
        self._o = options
        self._languages: dict[str, str | None] = {}

    def run(self, source: BenchmarkSource) -> ImportReport:
        prs = source.prs()
        report = ImportReport(source.name, listed=len(prs))
        for pr in prs[: self._o.limit] if self._o.limit else prs:
            try:
                self._one(pr, report)
            except BudgetExhausted as error:
                report.stopped = str(error)
                break
            except HostError as error:
                report.failed[str(pr.key)] = str(error)
                log.warning("%s: not imported: %s", pr.key, error)
        return report

    def _language(self, pr: BenchmarkPR) -> str:
        name = pr.language or self._repo_language(pr.repo)
        groups = {k.lower(): v for k, v in self._o.github_languages.items()}
        return groups.get((name or "").lower(), self._o.fallback_language)

    def _repo_language(self, repo: str) -> str | None:
        if repo not in self._languages:
            self._languages[repo] = self._host.repo_info(repo).language
        return self._languages[repo]

    def _one(self, pr: BenchmarkPR, report: ImportReport) -> None:
        fetched = self._host.fetch_pr(pr.repo, pr.number)
        base, head = pr.base_commit or fetched.base_oid, pr.head_commit or fetched.head_oid
        try:
            diff = self._host.compare(pr.repo, base, head)
        except NotFound:  # the benchmark reviewed a commit since force-pushed away: its comments can't be placed
            raise NotFound(f"the reviewed range {base[:10]}...{head[:10]} is no longer on the code host") from None
        stored = replace(fetched, title=fetched.title or pr.title, threads=(), compares=(), reviewed_diff=diff)
        item = HarvestedPR(
            pr=stored, language=self._language(pr), corpus=Corpus.HUMAN,
            author_kind=filters.pr_author_kind(stored.author), reviewed_commit=head, source=PRSource.BENCHMARK,
            split=TEST_SPLIT,
        )  # fmt: skip
        gold = gold_set(pr, pr.key, head, self._o.conf)
        self._store.upsert_pr(item)
        self._labels.save_gold(gold)
        self._store.save_benchmark_pr(pr, pr.key)
        report.imported += 1
        report.gold_issues += len(gold.issues)
        report.rejected_comments += len(pr.comments) - len(pr.golden)
        report.without_gold += not gold.issues
        log.info("%s: %d golden comments, %d files changed", pr.key, len(gold.issues), len(diff.files))
