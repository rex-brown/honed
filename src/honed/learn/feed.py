"""The feed selector (ARCHITECTURE.md section 7, Splits): the one place that decides which PRs the proposer and the
lesson miner read.

- **The feed sample.** The proposer mines the incumbent's failures on a sample of the feed split's PR-rounds, not on
  the whole split, which would cost several validation evaluations a run. `[improve] feed_sample_rounds` PR-rounds,
  stratified by language (each language gets its share of the pool's PR-rounds), in an order fixed by a salt, the
  incumbent's hash: every round with the same incumbent draws the same sample and reuses its stored run, and a
  promotion redraws it. Whole PRs are drawn, each with every replayed round that has a context pack (the bootstrap
  resamples whole PRs too). PRs with at least one gold issue come first; among them, PRs with an Important gold
  issue are drawn at the share that language's human-review PRs in the pool have, so preferring PRs with gold
  neither floods the feed with Important issues nor starves it of them. PRs without a gold issue, clean PRs
  included, fill only what the others can't.
- **The time cut.** An AI-feedback PR may be read only if it was created before its language's validation period
  began (`learn/splits.py` `validation_cuts`; for a language without validation PRs, the earliest cut; with no
  validation split at all, none may), so nothing from the validation or test weeks reaches a lesson. `admitted` is
  the rule and `FeedSelector.ai_feedback` the only place it is applied. Feed-split PRs need no cut: train is older
  than validation by construction, and the dev split is the small-store exception (both sides are dev).
- **Evidence.** The PRs a mined lesson may cite (`learn/lessons.py`): the feed split's PRs and the AI-feedback PRs
  the time cut admits.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from honed.core.sampling import largest_remainder
from honed.core.types import Corpus, HarvestedPR, PRKey, Severity
from honed.learn import replay
from honed.learn.lessons import EvidencePR
from honed.ports.store import LabelStore, Store

ALL = "all"  # the composition's total row


@dataclass(frozen=True)
class FeedPR:
    """A PR of the feed split, as the sampler sees it."""

    key: PRKey
    language: str
    rounds: tuple[int, ...]  # its replayed rounds that have a context pack
    gold_set: bool  # a human-review PR with a gold set (False: a clean PR)
    issues: int = 0  # gold issues in its stored gold set
    important: int = 0  # ... of them Important

    def units(self) -> list[str]:
        return [f"{self.key}@{r}" for r in self.rounds]


@dataclass(frozen=True)
class FeedSample:
    """The PR-rounds the incumbent is evaluated on for the proposer, and how they were drawn."""

    salt: str
    size: int  # `[improve] feed_sample_rounds`
    keys: tuple[PRKey, ...]
    units: tuple[str, ...]  # `owner/name#N@round`, the evaluation's PR-round units
    composition: Mapping[str, Mapping[str, Any]]  # language (and "all") -> counts, the pool's beside the sample's

    def to_json(self) -> dict[str, Any]:
        return {"size": self.size, "salt": self.salt[:12], "prs": len(self.keys), "pr_rounds": len(self.units),
                "composition": {k: dict(v) for k, v in self.composition.items()},
                "units": list(self.units)}  # fmt: skip

    def summary(self) -> str:
        cells = ", ".join(f"{lang} {c['pr_rounds']}" for lang, c in self.composition.items() if lang != ALL)
        total = self.composition.get(ALL, {})
        return (f"{len(self.units)} PR-rounds of {len(self.keys)} PRs ({cells}); {total.get('with_gold_issue', 0)} "
                f"PRs with a gold issue, {total.get('with_important', 0)} with an Important one")  # fmt: skip


def _rank(salt: str, key: PRKey) -> str:
    return hashlib.sha256(f"{salt}\0{key}".encode()).hexdigest()


def _take(queue: list[FeedPR], room: int) -> FeedPR | None:
    """The first PR of `queue` whose rounds fit in `room`, removed from it."""
    for n, pr in enumerate(queue):
        if len(pr.rounds) <= room:
            return queue.pop(n)
    return None


def _important_share(prs: Sequence[FeedPR]) -> float:
    human = [p for p in prs if p.gold_set]
    return sum(p.important > 0 for p in human) / len(human) if human else 0.0


def _draw_language(prs: Sequence[FeedPR], quota: int, salt: str) -> list[FeedPR]:
    """Up to `quota` PR-rounds of one language: PRs with a gold issue first, an Important one at the pool's share."""
    ordered = sorted(prs, key=lambda p: _rank(salt, p.key))
    important = [p for p in ordered if p.important > 0]
    gold = [p for p in ordered if p.issues > 0 and p.important == 0]
    rest = [p for p in ordered if p.issues == 0]
    share = _important_share(prs)
    chosen: list[FeedPR] = []
    used = with_important = 0
    while used < quota:
        wants_important = with_important < math.floor(share * (len(chosen) + 1) + 0.5)
        queues = (important, gold, rest) if wants_important else (gold, important, rest)
        pick = next((p for q in queues if (p := _take(q, quota - used)) is not None), None)
        if pick is None:
            break
        chosen.append(pick)
        used += len(pick.rounds)
        with_important += pick.important > 0
    return chosen


def _counts(pool: Sequence[FeedPR], chosen: Sequence[FeedPR]) -> dict[str, Any]:
    return {
        "pr_rounds": sum(len(p.rounds) for p in chosen), "prs": len(chosen),
        "later_rounds": sum(len(p.rounds) - (1 in p.rounds) for p in chosen),
        "with_gold_issue": sum(p.issues > 0 for p in chosen), "with_important": sum(p.important > 0 for p in chosen),
        "gold_issues": sum(p.issues for p in chosen), "important_issues": sum(p.important for p in chosen),
        "pool_pr_rounds": sum(len(p.rounds) for p in pool), "pool_prs": len(pool),
        "pool_with_gold_issue": sum(p.issues > 0 for p in pool),
        "pool_important_share": round(_important_share(pool), 4),
    }  # fmt: skip


def draw(pool: Sequence[FeedPR], size: int, salt: str) -> FeedSample:
    """The feed sample: `size` PR-rounds of `pool` (all of it when it has no more), stratified by language."""
    by_language: dict[str, list[FeedPR]] = defaultdict(list)
    for pr in pool:
        if pr.rounds:
            by_language[pr.language].append(pr)
    rounds = {lang: sum(len(p.rounds) for p in prs) for lang, prs in sorted(by_language.items())}
    quotas = rounds if sum(rounds.values()) <= size else largest_remainder(size, rounds)
    chosen = {lang: _draw_language(by_language[lang], quotas[lang], salt) for lang in rounds}
    picked = sorted((p for c in chosen.values() for p in c), key=lambda p: (p.key.repo, p.key.number))
    composition = {lang: _counts(by_language[lang], chosen[lang]) for lang in rounds}
    composition[ALL] = _counts([p for prs in by_language.values() for p in prs], picked)
    return FeedSample(salt=salt, size=size, keys=tuple(p.key for p in picked),
                      units=tuple(u for p in picked for u in p.units()), composition=composition)  # fmt: skip


def admitted(created_at: str, language: str, cuts: Mapping[str, str]) -> bool:
    """The time cut: created before its language's validation period began (`cuts`, per language); a language
    without a cut takes the earliest one, and without any cut (no validation split) nothing is admitted."""
    cut = cuts.get(language) or min(cuts.values(), default=None)
    return cut is not None and created_at < cut


@dataclass(frozen=True)
class FeedOptions:
    sample_rounds: int  # `[improve] feed_sample_rounds`
    review_rounds: int  # review rounds replayed per PR
    cuts: Mapping[str, str]  # per language, when its validation period begins (`splits.validation_cuts`)
    ai_feedback: tuple[PRKey, ...] = ()  # every AI-feedback PR; the time cut decides which may be read


class FeedSelector:
    """What the proposer and the lesson miner read: the sample of the feed split whose failures are mined, and the
    PRs a lesson may cite. The time cut on AI-feedback PRs is applied here and nowhere else."""

    def __init__(self, store: Store, labels: LabelStore, options: FeedOptions) -> None:
        self._store = store
        self._labels = labels
        self._o = options

    def pool(self, items: Mapping[PRKey, HarvestedPR]) -> list[FeedPR]:
        """The feed split's PRs with their packed replayed rounds and stored gold counts (no packed round: left out,
        so a missing pack shrinks the pool, never the sample)."""
        out = []
        for key, item in items.items():
            replayed = replay.replayed_rounds(item, self._o.review_rounds)
            rounds = tuple(r.index for r in replayed if self._store.get_round_pack(key, r.commit) is not None)
            if not rounds:
                continue
            gold = self._labels.get_gold(key) if item.corpus is Corpus.HUMAN else None
            issues = gold.issues if gold is not None else ()
            out.append(FeedPR(key, item.language, rounds, gold is not None, len(issues),
                              sum(i.severity is Severity.IMPORTANT for i in issues)))  # fmt: skip
        return out

    def sample(self, items: Mapping[PRKey, HarvestedPR], salt: str) -> FeedSample:
        return draw(self.pool(items), self._o.sample_rounds, salt)

    def ai_feedback(self) -> dict[PRKey, HarvestedPR]:
        """The AI-feedback PRs the time cut admits."""
        out = {}
        for key in self._o.ai_feedback:
            item = self._store.get_pr(key)
            if item is not None and admitted(item.pr.created_at, item.language, self._o.cuts):
                out[key] = item
        return out

    def evidence(self, items: Mapping[PRKey, HarvestedPR]) -> dict[PRKey, EvidencePR]:
        """The PRs a mined lesson may cite: the feed split's `items` and the AI-feedback PRs the time cut admits."""
        return {key: EvidencePR(key, item.pr.author.login if item.pr.author else "",
                                item.pr.reviewed_diff.files if item.pr.reviewed_diff else ())
                for key, item in {**items, **self.ai_feedback()}.items()}  # fmt: skip
