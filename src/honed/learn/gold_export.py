"""`honed export-gold --format martian`: our gold issues as Martian Code Review Bench golden comments (ARCHITECTURE.md
section 6, DATASET.md), so a tool scored by Martian's pipeline can be scored on our answer keys the same way.

One file per source repo (`<owner>__<name>.json`), each a list of `{pr_title, url, comments: [{comment, severity,
category}]}`, Martian's shape; `core.benchmarks` maps severities and categories. Only corpus PRs with at least one
gold issue go out: a benchmark PR's answer key is its benchmark's, and Martian's scorer skips a PR without golden
comments. The removal list applies as it does to a bundle, and the text goes through the same redaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from honed.core import removals
from honed.core.benchmarks import martian_entry
from honed.core.removals import Removals
from honed.core.types import PRKey, PRSource
from honed.learn.bundle_text import TextPolicy, pr_url
from honed.ports.store import LabelStore, Store


@dataclass
class GoldExport:
    files: dict[str, list[dict[str, object]]] = field(default_factory=dict)  # file name -> entries
    prs: int = 0
    issues: int = 0
    clean: int = 0  # PRs with a gold set but no gold issue, left out
    no_gold: int = 0  # PRs without a gold set
    removed: int = 0  # PRs on the removal list, and gold issues built from a removed comment's thread


def file_name(repo: str) -> str:
    return repo.replace("/", "__") + ".json"


def martian(store: Store, labels: LabelStore, keys: Sequence[PRKey], removed: Removals,
            text: TextPolicy) -> GoldExport:  # fmt: skip
    out = GoldExport()
    for key in sorted(keys, key=lambda k: (k.repo, k.number)):
        if removed.removes_pr(key):
            out.removed += 1
            continue
        item = store.get_pr(key)
        gold = labels.get_gold(key)
        if item is None or item.source is PRSource.BENCHMARK:
            continue
        if gold is None:
            out.no_gold += 1
            continue
        _, dropped, _ = removals.filter_pr(item, removed)
        gold, gone = removals.filter_gold(gold, dropped)
        out.removed += gone
        gold = text.gold(gold)
        entry = martian_entry(text.scrub(item.pr.title, f"{key} title"), pr_url(item.pr), gold.issues)
        if not entry["comments"]:
            out.clean += 1
            continue
        out.files.setdefault(file_name(key.repo), []).append(entry)
        out.prs += 1
        out.issues += len(entry["comments"])  # type: ignore[arg-type]
    return out
