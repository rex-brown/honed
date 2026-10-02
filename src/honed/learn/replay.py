"""Replayed review rounds (ARCHITECTURE.md section 6).

- Rounds come from the anchor commits of human review threads (`core.rounds`); up to N are replayed: the first,
  then the ones with the most threads.
- A round's review request is the PR as it stood at the round's commit: the diff from the merge base to that commit,
  the commits up to it, and the review discussion opened before the round began (earlier rounds' threads, with only
  the replies made by then). Nothing later reaches the reviewer.
- A round's gold issues: each issue counts once, in the latest replayed round at or before the round it was raised
  in whose commit has the flagged code (`core.rounds.target_round`). With one round replayed this is the stored gold
  set (the reviewed-commit rule); issues raised in later rounds on code the reviewed commit didn't have are grouped
  by the judge per round, like the stored gold set, and located at the round's commit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from honed.core import labels as core_labels
from honed.core import rounds as core_rounds
from honed.core.evals import EscapedDefect
from honed.core.filters import is_bot
from honed.core.reviews import EarlierComment, EarlierThread, ReviewRequest
from honed.core.rounds import ReviewRound
from honed.core.types import (
    AuthorKind,
    Compare,
    ContextPack,
    Corpus,
    GoldIssue,
    GoldProvenance,
    GoldSet,
    HarvestedPR,
    JudgedLabel,
    Thread,
)
from honed.learn.evidence import EvidenceBuilder, comments
from honed.learn.label import groups_with_retry
from honed.ports.code_reader import CodeReader
from honed.ports.judge import Judge
from honed.ports.store import LabelStore

log = logging.getLogger(__name__)


def rounds_for(item: HarvestedPR) -> list[ReviewRound]:
    human = {label.thread_id for label in item.labels if label.author_kind is AuthorKind.HUMAN}
    return core_rounds.review_rounds(item.pr, item.reviewed_commit, human)


def replayed_rounds(item: HarvestedPR, limit: int) -> list[ReviewRound]:
    """The rounds a replay of up to `limit` rounds per PR reviews (`core.rounds.select_rounds`); an approval-only
    PR has its first round only."""
    return core_rounds.select_rounds(rounds_for(item), 1 if item.corpus is Corpus.APPROVAL_ONLY else limit)


def diff_for(item: HarvestedPR, round_: ReviewRound, pack: ContextPack | None) -> Compare | None:
    """Merge base -> the round's commit: the PR's reviewed diff for round 1, the round pack's diff after that."""
    if round_.commit == item.reviewed_commit and item.pr.reviewed_diff is not None:
        return item.pr.reviewed_diff
    return pack.diff if pack is not None else None


def _commit_messages(item: HarvestedPR, round_: ReviewRound) -> tuple[str, ...]:
    oids = [c.oid for c in item.pr.commits]
    if round_.commit in oids:
        kept = item.pr.commits[: oids.index(round_.commit) + 1]
    elif round_.started_at:
        kept = tuple(c for c in item.pr.commits if c.committed_date and c.committed_date <= round_.started_at)
    else:
        kept = ()
    return tuple(c.message_headline for c in kept if c.message_headline)


def earlier_threads(item: HarvestedPR, before: str) -> tuple[EarlierThread, ...]:
    """Every review thread opened before `before`, with the comments made by then (none for an empty time)."""
    if not before:
        return ()
    out = []
    for thread in sorted(item.pr.threads, key=lambda t: t.created_at or ""):
        first = thread.first
        if first is None or not thread.created_at or thread.created_at >= before or is_bot(first.author):
            continue
        roles = comments(item, thread)
        shown = tuple(EarlierComment(c.author, c.role, c.body, raw.created_at)
                      for c, raw in zip(roles, thread.comments, strict=True) if raw.created_at < before)  # fmt: skip
        out.append(EarlierThread(thread.path, thread.flagged_lines, shown))
    return tuple(out)


def request_for(item: HarvestedPR, round_: ReviewRound, diff: Compare) -> ReviewRequest:
    pr = item.pr
    return ReviewRequest(
        repo=pr.repo, number=pr.number, title=pr.title, body=pr.body,
        author=pr.author.login if pr.author else "(deleted account)", language=item.language,
        base_commit=diff.merge_base or diff.base, head_commit=round_.commit, files=diff.files,
        created_at=pr.created_at, commit_messages=_commit_messages(item, round_), round=round_.index,
        earlier_threads=earlier_threads(item, round_.started_at),
    )  # fmt: skip


# ---- gold per round ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundGoldOptions:
    max_rounds: int
    context_lines: int
    gold_conf: Mapping[GoldProvenance, float]
    include_escaped_defects: bool


class RoundGold:
    """Builds, saves and loads each replayed round's gold issues for a human-review PR."""

    def __init__(self, labels: LabelStore, judge: Judge, reader_for: Callable[[str], CodeReader],
                 options: RoundGoldOptions) -> None:  # fmt: skip
        self._labels = labels
        self._judge = judge
        self._reader_for = reader_for
        self._o = options

    def cached(self, item: HarvestedPR, replayed: Sequence[ReviewRound]) -> dict[int, GoldSet] | None:
        out = {}
        for r in replayed:
            gold = self._labels.get_round_gold(item.key, r.commit, self._o.max_rounds)
            if gold is None:
                return None
            out[r.index] = gold
        return out

    def build(self, item: HarvestedPR, rounds: Sequence[ReviewRound], replayed: Sequence[ReviewRound],
              judged: Sequence[JudgedLabel]) -> dict[int, GoldSet]:  # fmt: skip
        stored = self._labels.get_gold(item.key)
        if stored is None:
            raise ValueError(f"{item.key}: no gold set")
        commit_of = {r.index: r.commit for r in replayed}
        issues: dict[int, list[GoldIssue]] = {r.index: [] for r in replayed}
        threads = {t.id: t for t in item.pr.threads}
        builder = EvidenceBuilder(self._reader_for(item.pr.repo), self._o.context_lines) if len(replayed) > 1 else None

        def lines_at(thread: Thread, commit: str) -> tuple[int, int] | None:
            if builder is None:
                return None
            return builder.location_at(thread, commit).lines

        for issue in stored.issues:
            primary = threads.get(issue.source_threads[0]) if issue.source_threads else None
            opened = core_rounds.round_of(primary.id, rounds) if primary else 1
            target = core_rounds.target_round(
                opened, replayed,
                lambda c, p=primary: c == item.reviewed_commit or (p is not None and lines_at(p, c) is not None),
            )  # fmt: skip
            if target == 1:
                issues[1].append(issue)
            elif target is not None and primary is not None:
                where = lines_at(primary, commit_of[target]) or (issue.start_line, issue.end_line)
                issues[target].append(replace(issue, start_line=where[0], end_line=where[1]))
        if builder is not None:
            self._later_rounds(item, rounds, replayed, judged, stored, builder, issues)
        if self._o.include_escaped_defects:
            issues[1] += [d.issue for d in self._escaped(item)]
        out = {}
        for r in replayed:
            gold = GoldSet(item.pr.repo, item.pr.number, r.commit, tuple(sorted(issues[r.index], key=_order)),
                           candidates=len(issues[r.index]))  # fmt: skip
            self._labels.save_round_gold(gold, self._o.max_rounds)
            out[r.index] = gold
        return out

    def _escaped(self, item: HarvestedPR) -> list[EscapedDefect]:
        return list(self._labels.escaped_defects(item.key))

    def _later_rounds(self, item: HarvestedPR, rounds: Sequence[ReviewRound], replayed: Sequence[ReviewRound],
                      judged: Sequence[JudgedLabel], stored: GoldSet, builder: EvidenceBuilder,
                      issues: dict[int, list[GoldIssue]]) -> None:  # fmt: skip
        """Gold threads the stored set left out (their code wasn't at the reviewed commit), grouped per round."""
        threads = {t.id: t for t in item.pr.threads}
        left_out = set(stored.excluded_later_round) | set(stored.excluded_unreadable)
        commit_of = {r.index: r.commit for r in replayed}
        per_round: dict[int, list[tuple[Thread, GoldProvenance, tuple[int, int]]]] = {}
        for label in judged:
            provenance = core_labels.gold_provenance(label)
            thread = threads.get(label.thread_id)
            if provenance is None or thread is None or thread.id not in left_out:
                continue
            opened = core_rounds.round_of(thread.id, rounds)
            target = core_rounds.target_round(
                opened, replayed, lambda c, t=thread: builder.location_at(t, c).lines is not None
            )
            if target is None or target == 1:
                continue
            lines = builder.location_at(thread, commit_of[target]).lines
            assert lines is not None
            per_round.setdefault(target, []).append((thread, provenance, lines))
        for target, kept in per_round.items():
            evidence = [builder.thread(item, thread) for thread, _, _ in kept]
            groups = groups_with_retry(self._judge, str(item.key), item.pr.title, evidence)
            info = {thread.id: (thread, provenance, lines) for thread, provenance, lines in kept}
            for group in groups:
                members = sorted(group.thread_ids, key=lambda tid: (info[tid][0].created_at or "", tid))
                primary, _, lines = info[members[0]]
                provenance = core_labels.strongest(info[tid][1] for tid in members)
                issues[target].append(GoldIssue(
                    id=f"{item.key}:{primary.id}", path=primary.path, start_line=lines[0], end_line=lines[1],
                    severity=group.severity, provenance=provenance, conf=self._o.gold_conf[provenance],
                    description=group.summary, source_threads=tuple(members), category=group.category,
                ))  # fmt: skip


def _order(issue: GoldIssue) -> tuple[str, int, str]:
    return (issue.path, issue.start_line, issue.id)
