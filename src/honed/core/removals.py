"""The removal list (DATASET.md, "Removal"): PRs and review comments every dataset export leaves out, on request.
Pure data and functions; the committed file is `yardstick/removals.json` (`[paths] removals_file`).

A listed PR goes entirely: its record, labels, judgments, gold, splits, round data, escaped defects and benchmark
record. A listed comment takes its whole review thread with it (the replies answer it, and the thread's labels,
judgments and gold issues were made from it): the thread, its outcome label, the judgments and judged labels on it,
and every gold issue built from it. Removing a thread from the middle of a PR changes that PR's gold, so a removal
is followed by a point release.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from honed.core.types import GoldSet, HarvestedPR, PRKey


class RemovalsError(ValueError):
    """The removal list is malformed."""


@dataclass(frozen=True)
class Removals:
    prs: frozenset[PRKey] = frozenset()
    comments: frozenset[str] = frozenset()  # review-comment node ids

    def __len__(self) -> int:
        return len(self.prs) + len(self.comments)

    def removes_pr(self, key: PRKey) -> bool:
        return PRKey(key.repo.lower(), key.number) in self.prs


@dataclass
class Applied:
    """What a removal list took out of one export."""

    prs: set[PRKey] = field(default_factory=set)
    comments: set[str] = field(default_factory=set)  # listed comment ids found
    threads: set[str] = field(default_factory=set)  # threads dropped with them
    gold_issues: int = 0

    def counts(self, removals: Removals) -> dict[str, int]:
        listed = {"listed_prs": len(removals.prs), "listed_comments": len(removals.comments)}
        found = {"prs": len(self.prs), "comments": len(self.comments), "threads": len(self.threads)}
        return {**listed, **found, "gold_issues": self.gold_issues}


def parse_pr(ref: str) -> PRKey:
    repo, sep, number = ref.strip().rpartition("#")
    if not sep or "/" not in repo or not number.isdigit():
        raise RemovalsError(f"not a PR reference (owner/name#N): {ref!r}")
    return PRKey(repo.lower(), int(number))


def parse(data: Mapping[str, Any]) -> Removals:
    """The list from its JSON form: `{"prs": [{"pr": "owner/name#N", ...}], "comments": [{"id": "PRRC_...", ...}]}`.
    Entries may carry an `added` date; plain strings are accepted too."""

    def ids(name: str, key: str) -> Iterable[str]:
        entries = data.get(name) or []
        if not isinstance(entries, list):
            raise RemovalsError(f"{name} must be a list")
        for entry in entries:
            value = entry.get(key) if isinstance(entry, Mapping) else entry
            if not isinstance(value, str) or not value.strip():
                raise RemovalsError(f"{name}: an entry without {key!r}: {entry!r}")
            yield value.strip()

    return Removals(frozenset(parse_pr(ref) for ref in ids("prs", "pr")), frozenset(ids("comments", "id")))


def filter_pr(item: HarvestedPR, removals: Removals) -> tuple[HarvestedPR, set[str], set[str]]:
    """The PR without the threads that hold a listed comment; the dropped thread ids, and the listed ids found."""
    if not removals.comments:
        return item, set(), set()
    dropped, found = set(), set()
    for thread in item.pr.threads:
        hits = {c.id for c in thread.comments} & removals.comments
        if hits:
            dropped.add(thread.id)
            found |= hits
    if not dropped:
        return item, dropped, found
    threads = tuple(t for t in item.pr.threads if t.id not in dropped)
    labels = tuple(lab for lab in item.labels if lab.thread_id not in dropped)
    return replace(item, pr=replace(item.pr, threads=threads), labels=labels), dropped, found


def filter_gold(gold: GoldSet, dropped: set[str]) -> tuple[GoldSet, int]:
    """The gold set without the issues built from a dropped thread; and how many went."""
    if not dropped:
        return gold, 0
    kept = tuple(g for g in gold.issues if not set(g.source_threads) & dropped)
    return replace(gold, issues=kept), len(gold.issues) - len(kept)
