"""`honed splits [--assign]`: the evaluation splits per language (ARCHITECTURE.md section 6), and storing them;
`split_keys`, the PRs of a split, for every command that takes `--split`; and the feed selector's inputs
(`validation_cuts`, `ai_feedback_keys`)."""

from __future__ import annotations

import argparse
from collections import Counter

from honed import config
from honed.adapters.sqlite_store import SqliteStore
from honed.cli.wiring import open_store
from honed.core.types import PRKey, Severity
from honed.learn import splits

_ORDER = (splits.TRAIN, splits.VALIDATION, splits.TEST_PUBLIC, splits.TEST_PRIVATE, splits.TEST)


def _items(settings: config.Settings, store: SqliteStore) -> list[splits.SplitItem]:
    facts = store.pr_facts()
    has_gold = {k for k in (PRKey(f.repo, f.number) for f in facts) if store.get_gold(k) is not None}
    return splits.eligible(facts, has_gold, settings.corpus.excluded)


def split_keys(settings: config.Settings, store: SqliteStore, split: str, repos: list[str] | None = None,
               limit: int | None = None) -> list[PRKey]:  # fmt: skip
    """The eligible PRs of a split (ARCHITECTURE.md section 6), optionally narrowed to repos and N per repo."""
    keys = splits.select(split, _items(settings, store), settings.eval.split_fractions, settings.eval.dev_split,
                         min_validation=settings.gate.min_prs_per_language)  # fmt: skip
    if repos:
        wanted = {r.lower() for r in repos}
        keys = [k for k in keys if k.repo.lower() in wanted]
    if limit:
        per_repo: dict[str, list[PRKey]] = {}
        for key in keys:
            per_repo.setdefault(key.repo, []).append(key)
        keys = [k for group in per_repo.values() for k in group[:limit]]
    return keys


def _assigned(settings: config.Settings, items: list[splits.SplitItem]) -> dict[PRKey, str]:
    return splits.assign(items, settings.eval.split_fractions, min_validation=settings.gate.min_prs_per_language,
                         private_share=settings.eval.test_private_share)  # fmt: skip


def validation_cuts(settings: config.Settings, store: SqliteStore) -> dict[str, str]:
    """Per language, when its validation period begins (stored splits as they are, the rest by time order)."""
    items = _items(settings, store)
    return splits.validation_cuts(items, _assigned(settings, items))


def ai_feedback_keys(settings: config.Settings, store: SqliteStore) -> list[PRKey]:
    """Every AI-feedback PR outside the excluded repos (`learn/feed.py` decides which may be read)."""
    return splits.ai_feedback(store.pr_facts(), settings.corpus.excluded)


def cmd_splits(settings: config.Settings, args: argparse.Namespace) -> int:
    """The split table; with `--assign`, store the assignment of every PR that has no stored split yet (stored
    splits are never moved, so running it again changes nothing)."""
    store = open_store(settings)
    try:
        facts = store.pr_facts()
        stored = {PRKey(f.repo, f.number): f.split for f in facts}
        items = _items(settings, store)
        assigned = _assigned(settings, items)
        for key in splits.ai_feedback(facts, settings.corpus.excluded):
            assigned[key] = stored[key] or splits.TRAIN
        new = {k: s for k, s in assigned.items() if stored.get(k) is None}
        if args.assign and new:
            store.set_splits(new)
        gold = {}
        for key in assigned:
            found = store.get_gold(key)
            if found is not None:
                gold[key] = (len(found.issues), sum(i.severity is Severity.IMPORTANT for i in found.issues))
    finally:
        store.close()
    _print(splits.table(assigned, facts, gold), settings.gate.min_prs_per_language)
    kept = len(assigned) - len(new)
    if args.assign:
        print(f"\nstored: {len(new)} PRs assigned now ({_counts(new) or 'none'}); {kept} kept their stored split")
    elif new:
        print(f"\n{kept} PRs have a stored split; {len(new)} don't ({_counts(new)}): the table shows what "
              "`honed splits --assign` would store")  # fmt: skip
    else:
        print(f"\nall {kept} PRs have a stored split")
    return 0


def _counts(assigned: dict[PRKey, str]) -> str:
    counts = Counter(assigned.values())
    return ", ".join(f"{s} {counts[s]}" for s in _ORDER if counts[s])


def _print(cells: dict[tuple[str, str], splits.SplitCounts], floor: int) -> None:
    languages = sorted({lang for _, lang in cells})
    header = ("split", "language", "gold PRs", "w/ issue", "w/ Important", "issues", "Important", "clean",
              "AI-feedback", "benchmark")  # fmt: skip
    rows = []
    for split in _ORDER:
        total = splits.SplitCounts()
        for lang in languages:
            c = cells.get((split, lang))
            if c is None:
                continue
            note = " (< floor)" if split == splits.VALIDATION and c.gold_prs < floor else ""
            rows.append((split, lang, f"{c.gold_prs}{note}", c.issue_prs, c.important_prs, c.issues, c.important,
                         c.clean_prs, c.ai_feedback_prs, c.benchmark_prs))  # fmt: skip
            for name in total.__dataclass_fields__:
                setattr(total, name, getattr(total, name) + getattr(c, name))
        if any(getattr(total, name) for name in total.__dataclass_fields__):
            rows.append((split, "all", total.gold_prs, total.issue_prs, total.important_prs, total.issues,
                         total.important, total.clean_prs, total.ai_feedback_prs, total.benchmark_prs))  # fmt: skip
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    for n, row in enumerate([header, *rows]):
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)).rstrip())
        if n == 0:
            print("  ".join("-" * w for w in widths))
