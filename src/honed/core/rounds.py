"""Review rounds: which commit a PR was at when it was first reviewed, and its later review rounds."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from honed.core.filters import is_bot
from honed.core.types import PullRequest, Thread


def reviewed_commit(pr: PullRequest) -> str:
    """Head of the first review round: the commit of the earliest review by a human other than the author. Failing
    that, the earliest review of any kind, then the earliest thread's anchor commit, then the final head."""
    author = pr.author.login if pr.author else None
    reviews = sorted((r for r in pr.reviews if r.commit and r.submitted_at), key=lambda r: r.submitted_at or "")
    human = [r for r in reviews if not is_bot(r.author) and r.author is not None and r.author.login != author]
    for pool in (human, reviews):
        if pool and pool[0].commit:
            return pool[0].commit
    anchors = sorted(
        (t.first.created_at, t.first.original_commit) for t in pr.threads if t.first and t.first.original_commit
    )
    return anchors[0][1] if anchors else pr.head_oid


@dataclass(frozen=True)
class ReviewRound:
    """One review round of a PR: the commit reviewers looked at, and the human threads they opened on it."""

    index: int  # 1-based, in time order; round 1 is the PR's reviewed commit
    commit: str
    started_at: str  # when the round's first human thread was opened ("" when it has none)
    thread_ids: tuple[str, ...] = ()


def review_rounds(pr: PullRequest, reviewed: str, human_threads: Collection[str]) -> list[ReviewRound]:
    """Review rounds from the anchor commits of human review threads (ARCHITECTURE.md section 6). Round 1 is the
    reviewed commit; every other anchor commit is a later round, ordered by when its first thread was opened. A
    thread anchored at a commit whose first thread predates round 1's belongs to round 1."""
    by_commit: dict[str, list[Thread]] = {}
    for thread in pr.threads:
        if thread.id in human_threads and thread.anchor_commit and thread.created_at:
            by_commit.setdefault(thread.anchor_commit, []).append(thread)
    first = {commit: min(t.created_at or "" for t in threads) for commit, threads in by_commit.items()}
    start = first.get(reviewed, "")
    early = [c for c in by_commit if c != reviewed and start and first[c] < start]
    round_one = [t.id for c in (reviewed, *early) for t in by_commit.get(c, [])]
    later = sorted((c for c in by_commit if c != reviewed and c not in early), key=lambda c: (first[c], c))
    rounds = [ReviewRound(1, reviewed, start, tuple(round_one))]
    for n, commit in enumerate(later, 2):
        rounds.append(ReviewRound(n, commit, first[commit], tuple(t.id for t in by_commit[commit])))
    return rounds


def select_rounds(rounds: Sequence[ReviewRound], limit: int) -> list[ReviewRound]:
    """The rounds to replay: the first, then the ones with the most threads (earlier first on a tie), in order."""
    if not rounds or limit < 1:
        return []
    rest = sorted(rounds[1:], key=lambda r: (-len(r.thread_ids), r.index))[: limit - 1]
    return sorted([rounds[0], *rest], key=lambda r: r.index)


def round_of(thread_id: str, rounds: Sequence[ReviewRound]) -> int:
    """The round a thread was opened in (1 when it is in none: not a human thread, or no anchor)."""
    return next((r.index for r in rounds if thread_id in r.thread_ids), 1)


def target_round(opened_in: int, replayed: Sequence[ReviewRound], exists_at: Callable[[str], bool]) -> int | None:
    """The replayed round an issue raised in round `opened_in` counts in: the latest replayed round at or before it
    whose commit has the flagged code. Each issue counts once; with only round 1 replayed, this is the reviewed-commit
    rule, and with every round replayed, each round's gold is its own new threads."""
    for r in sorted(replayed, key=lambda r: -r.index):
        if r.index <= opened_in and exists_at(r.commit):
            return r.index
    return None
