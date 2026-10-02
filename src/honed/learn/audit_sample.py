"""A blind sample of review comments for human labeling (METRICS.md section 5: the 50-item human-labeled set that
sharpens the judge audit), in the shape the yardstick commits (`yardstick/human_labels/sample.json`).

Candidates are the threads opened by a human or AI reviewer on the new side of the diff. The sample is stratified
across repos first, then across (author kind, outcome) within each repo, taking one per stratum in turn in a fixed
pseudo-random order, so rare outcomes and AI-bot threads are represented. Each item carries only what a labeler needs
and what attribution requires: where the comment is (repo, PR, path, the commit it was made on and the flagged
lines), the first comment, its author's login and permalink. No code: the project never redistributes it, and the
labeling page (`tools/human-labels/`) fetches the file from GitHub at view time. No outcome, reply, judgment or later
code is included, a bot's own "✅ Resolved in ..." status lines are cut from its comment (`core/blinding.py`), and
items are ordered by repo and PR, so neither the fields, the text nor the order give the answer away.
Every string goes through the secrets and personal-data scan (`core/redaction.py`) before it is written.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from honed.core import redaction, sampling
from honed.core.blinding import blind_comment
from honed.core.filters import is_excluded
from honed.core.labels import REVIEW_KINDS
from honed.core.types import HarvestedPR, Outcome, PRKey, Thread
from honed.ports.store import LabelStore, Store


@dataclass(frozen=True)
class SampleItem:
    id: str  # the thread id in the store
    repo: str
    pr: int
    path: str
    language: str
    comment: str  # the thread's first comment, without bot status lines that report its outcome (`core/blinding.py`)
    author: str  # its author's login (attribution)
    url: str  # its permalink (attribution; the labeling page never shows it)
    commit: str | None  # the commit the comment was made on; None when the code host no longer names it
    lines: tuple[int, int] | None  # (start, end) of the flagged lines in that commit's file; None: a file comment


@dataclass(frozen=True)
class _Candidate:
    item: HarvestedPR
    thread: Thread
    stratum: str  # author kind and outcome: used to stratify, never written out

    @property
    def order(self) -> str:
        return hashlib.sha256(self.thread.id.encode()).hexdigest()


def candidates(store: Store, labels: LabelStore, excluded: Iterable[str]) -> list[_Candidate]:
    excluded = tuple(excluded)
    out = []
    for key in store.pr_keys():
        if is_excluded(key.repo, excluded):
            continue
        item = store.get_pr(key)
        if item is None:
            continue
        judged: dict[str, Outcome] = {lab.thread_id: lab.outcome for lab in labels.judged_labels(key)}
        threads = {t.id: t for t in item.pr.threads}
        for label in item.labels:
            thread = threads.get(label.thread_id)
            if label.author_kind not in REVIEW_KINDS or thread is None or not (thread.first and thread.first.body):
                continue
            if thread.diff_side != "RIGHT":  # old-side lines belong to the base file, which the item can't name
                continue
            outcome = judged.get(thread.id, label.outcome)
            out.append(_Candidate(item, thread, f"{label.author_kind.value}:{outcome.value}"))
    return out


def select(pool: Iterable[_Candidate], n: int) -> list[_Candidate]:
    """Up to `n` candidates: repos in turn, and within each repo its strata in turn, each in a fixed order."""
    by_repo: dict[str, dict[str, list[_Candidate]]] = defaultdict(lambda: defaultdict(list))
    for c in pool:
        by_repo[c.item.pr.repo][c.stratum].append(c)
    per_repo = [
        sampling.round_robin([sorted(group, key=lambda c: c.order) for _, group in sorted(strata.items())])
        for _, strata in sorted(by_repo.items())
    ]
    return sampling.round_robin(per_repo)[:n]


Link = Callable[[HarvestedPR, Thread], str]  # a review comment's permalink


def _items(chosen: Iterable[tuple[HarvestedPR, Thread]], link: Link) -> list[SampleItem]:
    """The sample's items, ordered by repo, PR and thread."""
    ordered = sorted(chosen, key=lambda c: (c[0].pr.repo, c[0].pr.number, c[1].created_at or "", c[1].id))
    out = []
    for item, thread in ordered:
        first = thread.first
        assert first is not None
        out.append(SampleItem(
            id=thread.id, repo=item.pr.repo, pr=item.pr.number, path=thread.path, language=item.language,
            comment=blind_comment(first.body), author=first.author.login if first.author else "",
            url=link(item, thread), commit=thread.anchor_commit or None, lines=thread.flagged_lines,
        ))  # fmt: skip
    return out


def export(store: Store, labels: LabelStore, *, n: int, excluded: Iterable[str], link: Link) -> list[SampleItem]:
    """A fresh blind sample of `n` review comments, ordered by repo, PR and thread."""
    return _items(((c.item, c.thread) for c in select(candidates(store, labels, excluded), n)), link)


class MissingItems(LookupError):
    pass


def rebuild(store: Store, refs: Sequence[tuple[str, int, str]], *, link: Link) -> list[SampleItem]:
    """The items of an existing sample, from the store: `refs` are (repo, PR number, thread id). Raises MissingItems
    naming every ref the store doesn't hold."""
    chosen, missing = [], []
    for repo, number, thread_id in refs:
        item = store.get_pr(PRKey(repo, number))
        thread = next((t for t in item.pr.threads if t.id == thread_id), None) if item else None
        if item is None or thread is None or thread.first is None:
            missing.append(f"{repo}#{number} {thread_id}")
        else:
            chosen.append((item, thread))
    if missing:
        raise MissingItems(", ".join(missing))
    return _items(chosen, link)


def publishable(items: Sequence[SampleItem]) -> tuple[list[dict[str, Any]], list[tuple[str, redaction.Hit]]]:
    """The items as JSON records with every string redacted, and each hit with its location (`<id>.comment`)."""
    records, hits = [], []
    for item in items:
        record, found = redaction.redact_value(asdict(item), item.id)
        records.append(record)
        hits += found
    return records, hits
