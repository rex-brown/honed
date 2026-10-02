"""Harvest PRs, review threads and their outcomes from the code host into the store (ARCHITECTURE.md sections 5,
11). Runs are resumable: each repo, corpus, window slice and sample keeps a cursor, advanced after every listed PR.

A human-review PR without a human inline thread is stored in the approval-only (clean-PR) set instead: it doesn't
count toward the repo's quota, and is kept only up to `approval_only_share` of that quota, on top of it.

Bug-targeted sampling: `bug_targeted_share` of a human-corpus repo's quota comes from landed PRs where a human
submitted a CHANGES_REQUESTED review (`filters.changes_requested_by_human`), taken from the same listing with a cursor
of their own; the rest is sampled as before. Each slice's two samples are interleaved, so a capped run takes from
both. A slice whose listing runs out of targeted PRs hands the shortfall to its general sample. Targeted PRs never
fill the approval-only set."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from honed.core import filters, outcomes, patches, rounds, sampling
from honed.core.labels import is_approval_only
from honed.core.types import (
    AuthorKind,
    Compare,
    CompareSource,
    Corpus,
    Cursor,
    DateWindow,
    FilePatch,
    HarvestedPR,
    PRSummary,
    PullRequest,
    RepoInfo,
    SampledAs,
)
from honed.learn.plan import ExcludedRepo, RepoPlan
from honed.ports.code_host import BudgetExhausted, CodeHost, HostError
from honed.ports.store import Store

log = logging.getLogger(__name__)

_APPROVAL_ONLY = "stored_approval_only"  # a `_harvest_pr` result: stored, outside the quota


@dataclass(frozen=True)
class HarvestOptions:
    slices: tuple[DateWindow, ...]  # the creation window, split for time-ordered sampling
    page_size: int
    line_slack: int  # lines of slack around flagged lines when deciding "fixed"
    excluded: frozenset[str]
    github_languages: Mapping[str, str]  # GitHub primary language -> language group key
    fallback_language: str
    limit: int | None = None  # at most this many new PRs per repo in this run
    approval_only_share: float = 0.25  # approval-only PRs kept, as a share of the repo's quota, on top of it
    bug_targeted_share: float = 0.0  # share of a human-corpus repo's quota sampled from changes-requested PRs


@dataclass
class RepoReport:
    repo: str
    corpus: Corpus
    added: int = 0  # counted toward the quota
    targeted: int = 0  # of `added`, from the bug-targeted sample
    approval_only: int = 0  # stored in the clean-PR set, outside the quota
    threads: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    stopped: str | None = None


class Harvester:
    def __init__(self, host: CodeHost, store: Store, options: HarvestOptions) -> None:
        self._host = host
        self._store = store
        self._options = options

    def run(self, plans: Sequence[RepoPlan]) -> list[RepoReport]:
        for plan in plans:
            if filters.is_excluded(plan.repo, self._options.excluded):
                raise ExcludedRepo(f"{plan.repo} is excluded from harvesting")
        reports = []
        for plan in plans:
            report = RepoReport(plan.repo, plan.corpus)
            reports.append(report)
            try:
                self._harvest_repo(plan, report)
            except BudgetExhausted as error:
                report.stopped = str(error)
                log.warning("stopping: %s", error)
                break
        return reports

    # ---- per repo ------------------------------------------------------------------------------------------

    def _harvest_repo(self, plan: RepoPlan, report: RepoReport) -> None:
        repo = filters.RENAMED.get(plan.repo, plan.repo)  # search does not follow renames
        info = self._host.repo_info(repo)
        language = plan.language or self._options.github_languages.get(
            info.language or "", self._options.fallback_language
        )
        slices = self._options.slices
        tasks = self._tasks(repo, plan)
        needs = [need for _, _, need in tasks]
        allowance = sum(needs) if self._options.limit is None else min(self._options.limit, sum(needs))
        approval_caps = sampling.spread(int(plan.quota * self._options.approval_only_share), len(slices))
        rooms = [
            max(0, cap - self._store.count_prs(repo, Corpus.APPROVAL_ONLY, w)) if plan.corpus is Corpus.HUMAN else 0
            for cap, w in zip(approval_caps, slices, strict=True)
        ]
        targeted = sum(need for sample, _, need in tasks if sample is SampledAs.TARGETED)
        log.info("%s [%s, %s]: quota %d, need %d (%d targeted), this run %d", repo, plan.corpus, language,
                 plan.quota, sum(needs), targeted, allowance)  # fmt: skip
        for i, (sample, slot, _) in enumerate(tasks):
            window = slices[slot]
            share = sampling.allocate(allowance - report.added, needs[i:])[0]
            taken = 0
            if share:
                room = rooms[slot] if sample is SampledAs.GENERAL else 0
                taken = self._harvest_slice(repo, info, language, plan.corpus, window, share, room, report, sample)
            if sample is SampledAs.TARGETED and self._store.get_cursor(repo, plan.corpus, window, sample).exhausted:
                needs[i + 1] += max(0, needs[i] - taken)  # the listing ran out: the general sample takes the rest

    def _tasks(self, repo: str, plan: RepoPlan) -> list[tuple[SampledAs, int, int]]:
        """(sample, slice index, PRs still needed) per listing, in harvest order: each slice's targeted sample, then
        its general one. Without a targeted share, one general listing per slice counts every stored PR."""
        slices, corpus = self._options.slices, plan.corpus
        wanted = sampling.share_of(plan.quota, self._options.bug_targeted_share) if corpus is Corpus.HUMAN else 0
        general_quotas = sampling.spread(plan.quota - wanted, len(slices))
        if not wanted:
            return [(SampledAs.GENERAL, i, max(0, q - self._store.count_prs(repo, corpus, w)))
                    for i, (q, w) in enumerate(zip(general_quotas, slices, strict=True))]  # fmt: skip
        tasks = []
        for i, (tq, gq, w) in enumerate(zip(sampling.spread(wanted, len(slices)), general_quotas, slices,
                                            strict=True)):  # fmt: skip
            have_t = self._store.count_prs(repo, corpus, w, SampledAs.TARGETED)
            have_g = self._store.count_prs(repo, corpus, w, SampledAs.GENERAL)
            if self._store.get_cursor(repo, corpus, w, SampledAs.TARGETED).exhausted:
                need_t, need_g = 0, max(0, tq + gq - have_t - have_g)
            else:
                need_t, need_g = max(0, tq - have_t), max(0, gq - have_g)
            tasks += [(SampledAs.TARGETED, i, need_t), (SampledAs.GENERAL, i, need_g)]
        return tasks

    def _harvest_slice(
        self, repo: str, info: RepoInfo, language: str, corpus: Corpus, window: DateWindow, wanted: int,
        approval_room: int, report: RepoReport, sample: SampledAs = SampledAs.GENERAL,
    ) -> int:  # fmt: skip
        """Take up to `wanted` PRs toward the quota from one slice's listing, for one sample; returns how many."""
        cursor = self._store.get_cursor(repo, corpus, window, sample)
        taken = 0
        while taken < wanted and not cursor.exhausted:
            page = self._host.list_landed_prs(repo, window, after=cursor.after, page_size=self._options.page_size)
            keep = {s.number for s in filters.drop_branch_copies(page.items)}
            for index in range(cursor.offset, len(page.items)):
                if taken >= wanted:
                    return taken
                summary = page.items[index]
                reason = self._reject(summary, info, corpus, keep, sample)
                if reason is None and corpus is Corpus.HUMAN and summary.thread_count == 0 and approval_room <= 0:
                    reason = "approval_only_over_cap"  # no inline thread at all: approval-only, and no room
                if reason is None:
                    reason = self._harvest_pr(summary, language, corpus, report, approval_room > 0, sample)
                if reason is None:
                    taken += 1
                elif reason == _APPROVAL_ONLY:
                    approval_room -= 1
                else:
                    report.skipped[reason] += 1
                cursor = Cursor(cursor.after, index + 1, False)
                self._store.set_cursor(repo, corpus, window, cursor, sample)
            cursor = Cursor(page.end_cursor, 0, not page.has_next or not page.items)
            self._store.set_cursor(repo, corpus, window, cursor, sample)
        return taken

    def _reject(self, s: PRSummary, info: RepoInfo, corpus: Corpus, keep: set[int],
                sample: SampledAs = SampledAs.GENERAL) -> str | None:  # fmt: skip
        """Why a listed PR is skipped before its detail is fetched, or None to fetch it."""
        if self._store.has_pr(s.key):
            return "already_stored"
        if sample is SampledAs.TARGETED and not filters.changes_requested_by_human(s):
            return "no_changes_requested"
        if not filters.keep_pr(s.title, s.author):
            return "bot_or_release_title"
        if filters.on_release_branch(s.title, s.base_ref, info.default_branch):
            return "release_branch"
        if s.number not in keep:
            return "branch_copy"
        if s.repo in filters.LANDED_BY_COMMIT and not s.closed_by_commit:
            return "not_landed"
        if corpus is Corpus.HUMAN and not filters.reviewed_by_human(s):
            return "no_human_review"
        if corpus is Corpus.AI_FEEDBACK and not filters.reviewed_by_ai(s):
            return "no_ai_review"
        return None

    # ---- per PR --------------------------------------------------------------------------------------------

    def _harvest_pr(
        self, s: PRSummary, language: str, corpus: Corpus, report: RepoReport, approval_room: bool,
        sample: SampledAs = SampledAs.GENERAL,
    ) -> str | None:  # fmt: skip
        """Fetch, label and store one PR: None when stored toward the quota, `_APPROVAL_ONLY` when stored in the
        clean-PR set, else a skip reason."""
        try:
            pr = self._host.fetch_pr(s.repo, s.number, thread_hint=s.thread_count)
        except BudgetExhausted:
            raise
        except HostError as error:
            log.warning("%s: fetch failed: %s", s.key, error)
            return "fetch_error"
        kinds = {t.id: filters.thread_author_kind(t.first.author if t.first else None, pr.author, pr.repo)
                 for t in pr.threads}  # fmt: skip
        if corpus is Corpus.AI_FEEDBACK:
            pr = replace(pr, threads=tuple(t for t in pr.threads if kinds[t.id] is AuthorKind.AI))
            if not pr.threads:
                return "no_ai_threads"
        reviewed = rounds.reviewed_commit(pr)
        pr = replace(
            pr, compares=self._thread_compares(pr), reviewed_diff=self._compare(pr.repo, pr.base_oid, reviewed)
        )
        labels = tuple(outcomes.label(t, kinds[t.id], pr.compares, self._options.line_slack) for t in pr.threads)
        approval_only = corpus is Corpus.HUMAN and is_approval_only(label.author_kind for label in labels)
        if approval_only and not approval_room:
            return "approval_only_over_cap"
        if approval_only:
            corpus = Corpus.APPROVAL_ONLY
        self._store.upsert_pr(
            HarvestedPR(
                pr=pr,
                language=language,
                corpus=corpus,
                author_kind=filters.pr_author_kind(pr.author),
                reviewed_commit=reviewed,
                labels=labels,
                sampled_as=sample,
            )
        )
        report.threads += len(pr.threads)
        tally = Counter(f"{label.author_kind}:{label.outcome}" for label in labels)
        note = " (approval-only)" if approval_only else (" (targeted)" if sample is SampledAs.TARGETED else "")
        log.info("%s%s: %d threads %s", pr.key, note, len(pr.threads), dict(tally))
        if approval_only:
            report.approval_only += 1
            return _APPROVAL_ONLY
        report.added += 1
        report.targeted += sample is SampledAs.TARGETED
        return None

    def _compare(self, repo: str, base: str, head: str) -> Compare | None:
        try:
            return self._host.compare(repo, base, head)
        except HostError as error:
            log.warning("%s: compare %s...%s failed: %s", repo, base[:10], head[:10], error)
            return None

    def _thread_compares(self, pr: PullRequest) -> tuple[Compare, ...]:
        """For each thread anchor commit, the changes from it to the final head, restricted to the thread paths.
        Thread files are diffed from their contents at the two commits instead when the host's compare can't
        decide them: the anchor was force-pushed away (a merge-base compare is not the direct diff), or the compare
        hit the host's file cap without listing them (common after merging the main branch in)."""
        paths_by_anchor: dict[str, set[str]] = defaultdict(set)
        for thread in pr.threads:
            if thread.anchor_commit:
                paths_by_anchor[thread.anchor_commit].add(thread.path)
        compares = []
        for anchor, paths in paths_by_anchor.items():
            if anchor == pr.head_oid:
                compares.append(Compare(anchor, pr.head_oid, "identical"))
                continue
            compare = self._compare(pr.repo, anchor, pr.head_oid)
            if compare is None:
                continue
            if not compare.is_direct:
                compares.append(self._content_compare(pr, anchor, paths))
                continue
            restricted = _restrict(compare, paths)
            compares.append(restricted)
            if not compare.complete:
                listed = {f.path for f in restricted.files} | {f.previous_path for f in restricted.files}
                if unlisted := paths - listed:
                    compares.append(self._content_compare(pr, anchor, unlisted))
        return tuple(compares)

    def _content_compare(self, pr: PullRequest, anchor: str, paths: Iterable[str]) -> Compare:
        files = []
        for path in sorted(paths):
            try:
                old = self._host.read_file(pr.repo, path, anchor)
                new = self._host.read_file(pr.repo, path, pr.head_oid) if old is not None else None
            except HostError as error:
                log.warning("%s: reading %s failed: %s", pr.key, path, error)
                continue
            if old is None:
                continue  # unknown: the outcome falls back to isOutdated
            if new is None:
                files.append(FilePatch(path, "removed", None))
            else:
                files.append(FilePatch(path, "modified", patches.unified_diff(old, new, path)))
        return Compare(anchor, pr.head_oid, "diverged", tuple(files), complete=False, source=CompareSource.CONTENT_DIFF)


def _restrict(compare: Compare, paths: set[str]) -> Compare:
    files = tuple(f for f in compare.files if f.path in paths or f.previous_path in paths)
    return replace(compare, files=files)
