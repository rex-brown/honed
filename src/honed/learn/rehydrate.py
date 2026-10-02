"""`honed rehydrate`: rebuild, from git, the code an imported bundle leaves out (ARCHITECTURE.md section 8).

For each PR (of one repo, or all), in order:
1. the patches the bundle removed (the store lists them: `Store.stripped`): the reviewed diff's and the thread
   compares' from `git diff` between the same commits (merge base to head), the locally diffed ones (a force-pushed
   anchor) from the file contents, as the harvester made them;
2. the context pack at the reviewed commit, unless there is one (`--rebuild` rebuilds it);
3. a round pack for each later review round the bundle carried a diff for, with that diff's patches restored.
Then the PR's code marks are cleared, and it can be reviewed and replayed unless a stripped bundle also left its text
out (`honed rehydrate --comments`, `learn/rehydrate_text.py`). Clones are fetched as `honed pack` fetches them
(bare, blobless, commits on demand), so this needs the network once; afterwards everything runs offline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from honed.core import marks, patches
from honed.core.types import Compare, CompareSource, HarvestedPR, PackBudget, PRKey
from honed.learn.packs import PackBuilder, PackError
from honed.ports.code_reader import Differ, ReaderError, ReaderUnavailable, RepoReader
from honed.ports.store import Store

log = logging.getLogger(__name__)


@dataclass
class RehydrateReport:
    repo: str
    patched: int = 0  # PRs whose patches were restored
    patches: int = 0  # files restored
    unrestored: list[str] = field(default_factory=list)  # files git had no patch for
    packs: int = 0
    round_packs: int = 0
    failed: dict[str, str] = field(default_factory=dict)


def restore_compare(compare: Compare, prefix: str, pending: Sequence[str], reader: Differ) -> tuple[Compare, list[str]]:
    """`compare` with the patches listed in `pending` (as `<prefix><path>`) rebuilt; and the files not restored."""
    wanted = {p.removeprefix(prefix) for p in pending if p.startswith(prefix)}
    targets = [f for f in compare.files if f.path in wanted and f.patch is None]
    if not targets:
        return compare, []
    found: dict[str, str | None] = {}
    if compare.source is CompareSource.CONTENT_DIFF:  # the harvester diffed these from file contents
        for f in targets:
            old = reader.read(f.previous_path or f.path, compare.base)
            new = reader.read(f.path, compare.head)
            if old is not None and new is not None:
                found[f.path] = patches.unified_diff(old, new, f.path)
    else:
        paths = [f.path for f in targets] + [f.previous_path for f in targets if f.previous_path]
        found = reader.diff(compare.merge_base or compare.base, compare.head, paths)
    files = tuple(replace(f, patch=found.get(f.path)) if f in targets else f for f in compare.files)
    missing = [f"{prefix}{f.path}" for f in targets if found.get(f.path) is None]
    return replace(compare, files=files), missing


def restore_pr(item: HarvestedPR, pending: Sequence[str], reader: Differ) -> tuple[HarvestedPR, list[str]]:
    pr, missing = item.pr, []
    reviewed = pr.reviewed_diff
    if reviewed is not None:
        reviewed, lost = restore_compare(reviewed, "reviewed:", pending, reader)
        missing += lost
    compares = []
    for n, compare in enumerate(pr.compares):
        restored, lost = restore_compare(compare, f"thread:{n}:", pending, reader)
        compares.append(restored)
        missing += lost
    return replace(item, pr=replace(pr, reviewed_diff=reviewed, compares=tuple(compares))), missing


class Rehydrator:
    def __init__(self, store: Store, reader_for: Callable[[str], RepoReader], budget: PackBudget, *,
                 rebuild: bool = False) -> None:  # fmt: skip
        self._store = store
        self._reader_for = reader_for
        self._budget = budget
        self._rebuild = rebuild

    def run(self, keys: Sequence[PRKey]) -> list[RehydrateReport]:
        by_repo: dict[str, list[PRKey]] = {}
        for key in keys:
            by_repo.setdefault(key.repo, []).append(key)
        reports = []
        for repo, repo_keys in sorted(by_repo.items()):
            report = RehydrateReport(repo)
            reader = self._reader_for(repo)
            builder = PackBuilder(reader, self._store, self._budget)
            for key in repo_keys:
                try:
                    self._one(key, reader, builder, report)
                except ReaderUnavailable:
                    raise  # the network: stop, don't degrade; running again resumes
                except (PackError, ReaderError) as error:
                    report.failed[str(key)] = str(error)
                    log.warning("%s: not rehydrated: %s", key, error)
            reports.append(report)
        return reports

    def _one(self, key: PRKey, reader: RepoReader, builder: PackBuilder, report: RehydrateReport) -> None:
        store = self._store
        item = store.get_pr(key)
        if item is None:
            return
        pending = store.stripped(key)
        if any(p.startswith(("reviewed:", "thread:")) for p in pending):
            item, missing = restore_pr(item, pending, reader)
            store.upsert_pr(item)
            report.patched += 1
            report.patches += sum(p.startswith(("reviewed:", "thread:")) for p in pending) - len(missing)
            report.unrestored += [f"{key} {m}" for m in missing]
        diff = item.pr.reviewed_diff
        if (self._rebuild or store.get_pack(key) is None) and diff is not None and diff.merge_base:
            store.save_pack(builder.build(item))
            report.packs += 1
        for round_diff in store.round_diffs(key):
            if not self._rebuild and store.get_round_pack(key, round_diff.head) is not None:
                continue
            restored, missing = restore_compare(round_diff, f"round:{round_diff.head}:", pending, reader)
            report.unrestored += [f"{key} {m}" for m in missing]
            store.save_round_pack(builder.build(item, head=round_diff.head, diff=restored))
            report.round_packs += 1
        store.mark_stripped(key, marks.text_marks(pending))  # the code is back; text a stripped bundle left out isn't
        log.info("%s: rehydrated", key)
