"""Evaluation splits (ARCHITECTURE.md section 6): by PR, time-ordered by creation, stratified by language.

Within each language group, the human-review PRs with a gold set, sorted by creation time, go oldest to `train`, then
`validation`, newest to `test`, by `[eval] split_fractions`, so the future never leaks into training. Validation holds
at least `min_validation` of them per language (`[gate] min_prs_per_language`, so every language's floor applies at
the gate); the shortfall comes out of train, never out of test. Approval-only PRs (the clean set) follow the same time
cuts: a clean PR goes to the split whose period its creation date falls in. `honed splits --assign` stores the
assignment, and halves each language's test PRs into `test-public` and `test-private` (`[eval] test_private_share`):
consecutive blocks in time order, one PR of each block, chosen by a hash of its key, held out, so both halves span
the same weeks. Test-private is the maintainers' holdout: `bundle export` leaves it out unless `--include-private`.
AI-feedback PRs are stored as `train` (they have no gold).

A PR with a fixed (stored) split keeps it: an imported benchmark PR is always `test`, an imported bundle fixes every
PR's split as its maker had it, and `splits --assign` fixes the rest, so re-running never reshuffles. PRs without a
stored split are assigned among themselves by the same rule (without the private half: their test PRs are `test`).
Excluded repos never enter a split, except as imported benchmark PRs (`test` only). `test` selects the whole test
family (benchmark, public and private). While the dataset is too small to split, `[eval] dev_split` names one split
holding every eligible PR but the benchmark ones (the dev split feeds the proposer). It is for stores without stored
splits only: on a store with them it would silently mean train plus validation, so selecting it is an error that
points to `train` and `validation` (`SplitError`). `validation_cuts` gives the date each language's validation period
begins, which the feed selector's time cut reads (`learn/feed.py`).

Eligible PRs: human-review PRs with a built gold set, and approval-only PRs (the clean-PR set, no gold issues).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from honed.core.filters import is_excluded
from honed.core.sampling import spread
from honed.core.types import Corpus, PRFact, PRKey, PRSource

TRAIN, VALIDATION, TEST = "train", "validation", "test"
TEST_PUBLIC, TEST_PRIVATE = "test-public", "test-private"
TEST_FAMILY = frozenset({TEST, TEST_PUBLIC, TEST_PRIVATE})  # `test` selects all three
SPLITS = (TRAIN, VALIDATION, TEST, TEST_PUBLIC, TEST_PRIVATE)
_PRIVATE_SALT = "test-private"  # the hash that picks a block's held-out PR; fixed, so assignment is deterministic


class SplitError(ValueError):
    """A split that can't be selected on this store."""


@dataclass(frozen=True)
class SplitItem:
    key: PRKey
    language: str
    created_at: str
    corpus: Corpus
    fixed: str | None = None  # a fixed split, kept as is
    benchmark: bool = False  # an imported benchmark PR (always `test`)


def eligible(facts: Sequence[PRFact], has_gold: Collection[PRKey], excluded: Collection[str]) -> list[SplitItem]:
    out = []
    for fact in facts:
        key = PRKey(fact.repo, fact.number)
        benchmark = fact.source is PRSource.BENCHMARK
        if is_excluded(fact.repo, excluded) and not benchmark:
            continue
        if (fact.corpus is Corpus.HUMAN and key in has_gold) or fact.corpus is Corpus.APPROVAL_ONLY:
            fixed = TEST if benchmark else fact.split
            out.append(SplitItem(key, fact.language, fact.created_at, fact.corpus, fixed, benchmark))
    return out


def ai_feedback(facts: Sequence[PRFact], excluded: Collection[str]) -> list[PRKey]:
    """AI-feedback PRs outside the excluded repos: stored as `train` (no gold; the lesson miner's material)."""
    return [PRKey(f.repo, f.number) for f in facts
            if f.corpus is Corpus.AI_FEEDBACK and not is_excluded(f.repo, excluded)]  # fmt: skip


def _order(item: SplitItem) -> tuple[str, str, int]:
    return (item.created_at, item.key.repo, item.key.number)


def _bounds(n: int, fractions: Mapping[str, float], min_validation: int) -> tuple[int, int]:
    """(train end, validation end) ranks for `n` time-ordered PRs; validation at least `min_validation`, from train."""
    train_end = round(n * fractions[TRAIN])
    validation_end = round(n * (fractions[TRAIN] + fractions[VALIDATION]))
    if validation_end - train_end < min_validation:
        train_end = max(0, validation_end - min_validation)
    return train_end, validation_end


def _held_out(group: Sequence[SplitItem], share: float) -> set[PRKey]:
    """`share` of the time-ordered `group`, in whole PRs (rounded down): the group in that many consecutive blocks,
    and from each the PR whose key hashes lowest, so the held-out PRs are spread over the same period."""
    blocks = int(share * len(group))
    out, start = set(), 0
    for size in spread(len(group), blocks):
        block = group[start : start + size]
        out.add(min(block, key=lambda i: hashlib.sha256(f"{_PRIVATE_SALT}\0{i.key}".encode()).hexdigest()).key)
        start += size
    return out


def assign(items: Sequence[SplitItem], fractions: Mapping[str, float], *, min_validation: int = 0,
           private_share: float = 0.0) -> dict[PRKey, str]:  # fmt: skip
    """A fixed split as it is; the rest time-ordered within each language: the human-review PRs by rank (the oldest
    `train` share, then `validation`, at least `min_validation` of them, the newest `test`), the clean PRs by the
    same cut dates. With `private_share`, that share of each language's test PRs (human and clean apart) is
    `test-private` and the rest `test-public`; without it they are `test`."""
    by_language: dict[str, list[SplitItem]] = defaultdict(list)
    out: dict[PRKey, str] = {}
    for item in items:
        if item.fixed:
            out[item.key] = item.fixed
        else:
            by_language[item.language].append(item)
    for group in by_language.values():
        human = sorted((i for i in group if i.corpus is Corpus.HUMAN), key=_order)
        clean = sorted((i for i in group if i.corpus is not Corpus.HUMAN), key=_order)
        floor = min_validation
        if not human:  # a language with clean PRs only: they rank among themselves, with no validation floor
            human, clean, floor = clean, [], 0
        train_end, validation_end = _bounds(len(human), fractions, floor)
        for rank, item in enumerate(human):
            out[item.key] = TRAIN if rank < train_end else VALIDATION if rank < validation_end else TEST
        validation_from = human[train_end].created_at if train_end < len(human) else None
        test_from = human[validation_end].created_at if validation_end < len(human) else None
        for item in clean:
            if validation_from is not None and item.created_at < validation_from:
                out[item.key] = TRAIN
            elif test_from is None or item.created_at < test_from:
                out[item.key] = VALIDATION
            else:
                out[item.key] = TEST
        if private_share > 0:
            for part in (human, clean):  # each language's human and clean test PRs halved apart
                tested = [i for i in part if out[i.key] == TEST]
                private = _held_out(tested, private_share)
                for item in tested:
                    out[item.key] = TEST_PRIVATE if item.key in private else TEST_PUBLIC
    return out


def select(split: str, items: Sequence[SplitItem], fractions: Mapping[str, float], dev_split: str, *,
           min_validation: int = 0) -> list[PRKey]:  # fmt: skip
    def order(keys: Collection[PRKey]) -> list[PRKey]:
        return sorted(keys, key=lambda k: (k.repo, k.number))

    if split == dev_split:
        stored = sum(1 for i in items if i.fixed and not i.benchmark)
        if stored:
            raise SplitError(
                f"this store has stored splits ({stored} PRs, from `honed splits --assign` or an imported bundle): "
                f"the dev split {dev_split!r} would silently mean train plus validation. Use --split train (the "
                "proposer's feed) or --split validation (the gate); test-public, test-private and test are held out"
            )
        return order([i.key for i in items if not i.benchmark])
    if split not in SPLITS:
        raise SplitError(f"unknown split {split!r}: one of {(*SPLITS, dev_split)}")
    assigned = assign(items, fractions, min_validation=min_validation)
    wanted = TEST_FAMILY if split == TEST else {split}
    return order([k for k, s in assigned.items() if s in wanted])


@dataclass
class SplitCounts:
    """One (split, language) cell of the split table."""

    gold_prs: int = 0  # human-review (or benchmark) PRs with a gold set
    issue_prs: int = 0  # ... with at least one gold issue
    important_prs: int = 0  # ... with at least one Important gold issue
    issues: int = 0
    important: int = 0
    clean_prs: int = 0  # approval-only PRs
    ai_feedback_prs: int = 0
    benchmark_prs: int = 0


def table(splits: Mapping[PRKey, str], facts: Sequence[PRFact],
          gold: Mapping[PRKey, tuple[int, int]]) -> dict[tuple[str, str], SplitCounts]:  # fmt: skip
    """Counts per (split, language) for the PRs in `splits`; `gold`: a PR's (gold issues, Important ones)."""
    out: dict[tuple[str, str], SplitCounts] = defaultdict(SplitCounts)
    for fact in facts:
        key = PRKey(fact.repo, fact.number)
        split = splits.get(key)
        if split is None:
            continue
        cell = out[(split, fact.language)]
        if fact.source is PRSource.BENCHMARK:
            cell.benchmark_prs += 1
        elif fact.corpus is Corpus.AI_FEEDBACK:
            cell.ai_feedback_prs += 1
        elif fact.corpus is Corpus.APPROVAL_ONLY:
            cell.clean_prs += 1
        if key in gold and fact.corpus is Corpus.HUMAN:
            issues, important = gold[key]
            cell.gold_prs += 1
            cell.issue_prs += issues > 0
            cell.important_prs += important > 0
            cell.issues += issues
            cell.important += important
    return dict(out)


def validation_cuts(items: Sequence[SplitItem], assigned: Mapping[PRKey, str]) -> dict[str, str]:
    """Per language, when its validation period begins: the creation time of its earliest validation PR. Nothing
    created at or after a language's cut may reach the proposer or a lesson (`learn/feed.py`)."""
    out: dict[str, str] = {}
    for item in items:
        if assigned.get(item.key) == VALIDATION and (item.language not in out or item.created_at < out[item.language]):
            out[item.language] = item.created_at
    return out


SCREEN_SPLIT_SUFFIX = "/screen"  # screen runs are stored under the gate split's name plus this
FEED_SPLIT_SUFFIX = "/feed"  # the incumbent's runs on the feed sample, under the feed split's name plus this


def screen_keys(languages: Mapping[PRKey, str], fraction: float, salt: str) -> list[PRKey]:
    """The screen subset (METRICS.md section 3, screening): `fraction` of each language's PRs (at least one, rounded),
    in a pseudo-random order fixed by `salt`, so every candidate of a round is screened on the same PRs."""
    by_language: dict[str, list[PRKey]] = defaultdict(list)
    for key, language in languages.items():
        by_language[language].append(key)
    out: list[PRKey] = []
    for _, keys in sorted(by_language.items()):
        ranked = sorted(keys, key=lambda k: hashlib.sha256(f"{salt}\0{k}".encode()).hexdigest())
        out += ranked[: max(1, round(fraction * len(keys)))]
    return sorted(out, key=lambda k: (k.repo, k.number))
