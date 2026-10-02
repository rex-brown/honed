"""Context packs (ARCHITECTURE.md section 8): the code a review of one PR may read, saved at the reviewed commit.

A pack holds, within `PackBudget`, in this priority order:
1. the files the PR changed, in full, at the reviewed commit (head of the first review round) and at its merge base;
2. the repo's review guidance (REVIEW.md, CLAUDE.md at the root and in the changed files' directories), at the
   merge base, so a change can't loosen its own review;
3. tests named after a changed file, closest first;
4. source files referencing a symbol the diff defines or changes outside tests (callers, importers, includers of
   changed headers), ranked by how many of the symbols they mention, then by directory distance to the change.
Every file records why it was included, and every file left out records why it was left out.

Round packs (ARCHITECTURE.md section 6): the same, at a later review round's commit, from the diff between that
commit and its merge base (from the code host), which the pack keeps so the round replays offline.
"""

from __future__ import annotations

import logging
import posixpath
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from honed.core import patches, symbols, testfiles
from honed.core.rounds import ReviewRound
from honed.core.types import (
    Compare,
    ContextPack,
    FilePatch,
    HarvestedPR,
    PackBudget,
    PackFile,
    PackRole,
    PRKey,
    SkippedFile,
)
from honed.ports.code_reader import CodeReader, ReaderError
from honed.ports.store import Store

log = logging.getLogger(__name__)


GUIDANCE_FILES = ("REVIEW.md", "CLAUDE.md")


class PackError(RuntimeError):
    pass


def guidance_paths(tree: Sequence[str], changed: Sequence[str]) -> list[str]:
    """REVIEW.md and CLAUDE.md at the root and in every directory above a changed file, root first."""
    dirs = [""]
    for path in changed:
        parts = posixpath.dirname(path).split("/")
        dirs += ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1) if parts[0]]
    present = set(tree)
    out: list[str] = []
    for d in dict.fromkeys(dirs):
        out += [p for name in GUIDANCE_FILES if (p := posixpath.join(d, name) if d else name) in present]
    return out


def grep_scope(tree: Sequence[str], changed: Sequence[str], budget: PackBudget) -> tuple[str, ...]:
    """Pathspecs to search for referencing files; () means the whole tree. A big tree is searched in the changed
    files' directories, widened toward the root while the scope stays within `grep_scope_max_files`; if even those
    directories are too big, only the files directly in them."""
    if len(tree) <= budget.grep_full_tree_max_files:
        return ()
    under: Counter[str] = Counter()
    for path in tree:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            under["/".join(parts[:depth])] += 1

    def minimal(dirs: set[str]) -> set[str]:
        return {d for d in dirs if not any(o != d and d.startswith(o + "/") for o in dirs)}

    current = minimal({posixpath.dirname(p) for p in changed})
    if "" in current or sum(under[d] for d in current) > budget.grep_scope_max_files:
        dirs = {posixpath.dirname(p) for p in changed}
        direct = [p for p in tree if posixpath.dirname(p) in dirs]
        return tuple(sorted(direct[: budget.grep_scope_max_files]))
    while True:
        wider = minimal({posixpath.dirname(d) for d in current})
        if wider == current or "" in wider or sum(under[d] for d in wider) > budget.grep_scope_max_files:
            return tuple(sorted(current))
        current = wider


class _Packer:
    """Accumulates pack files within the budget, storing contents as blobs."""

    def __init__(self, budget: PackBudget, put_blob: Callable[[bytes], str]) -> None:
        self._budget = budget
        self._put = put_blob
        self.files: list[PackFile] = []
        self.skipped: list[SkippedFile] = []
        self.bytes = 0
        self._seen: set[tuple[str, str]] = set()

    def has_room(self) -> bool:
        return len(self.files) < self._budget.max_files and self.bytes < self._budget.max_bytes

    def skip(self, path: str, reason: str) -> None:
        self.skipped.append(SkippedFile(path, reason))

    def add(self, path: str, commit: str, role: PackRole, reason: str, read: Callable[[], str | None]) -> str | None:
        """Read and add a file unless the budget is spent; returns its text when added."""
        if (path, commit) in self._seen:
            return None
        self._seen.add((path, commit))
        if not self.has_room():
            self.skip(path, f"{role}: pack full (max_files or max_bytes)")
            return None
        text = read()
        if text is None:
            self.skip(path, f"{role}: binary or unreadable")
            return None
        data = text.encode()
        if len(data) > self._budget.max_file_bytes:
            self.skip(path, f"{role}: {len(data)} bytes > max_file_bytes")
            return None
        if self.bytes + len(data) > self._budget.max_bytes:
            self.skip(path, f"{role}: would exceed max_bytes")
            return None
        self.files.append(PackFile(path, commit, role, reason, self._put(data), len(data)))
        self.bytes += len(data)
        return text


@dataclass
class PackReport:
    repo: str
    built: list[ContextPack] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # PR -> error

    @property
    def seconds(self) -> list[float]:
        return [p.build_seconds for p in self.built]


class PackBuilder:
    def __init__(self, reader: CodeReader, store: Store, budget: PackBudget) -> None:
        self._reader = reader
        self._store = store
        self._budget = budget

    def build_all(self, keys: Sequence[PRKey], *, rebuild: bool = False) -> PackReport:
        report = PackReport(keys[0].repo if keys else "")
        for key in keys:
            if not rebuild and self._store.get_pack(key) is not None:
                continue
            item = self._store.get_pr(key)
            if item is None:
                continue
            try:
                pack = self.build(item)
            except (PackError, ReaderError) as error:
                report.failed[str(key)] = str(error)
                log.warning("%s: no pack: %s", key, error)
                continue
            self._store.save_pack(pack)
            report.built.append(pack)
            roles = Counter(f.role.value for f in pack.files)
            log.info("%s: %d files, %d KB, %s, %.1fs", key, len(pack.files), pack.total_bytes // 1024, dict(roles),
                     pack.build_seconds)  # fmt: skip
        return report

    def build_rounds(self, item: HarvestedPR, rounds: Sequence[ReviewRound], compare: Callable[[str, str], Compare],
                     *, rebuild: bool = False) -> list[ContextPack]:  # fmt: skip
        """Packs at the later rounds' commits (round 1 is the PR's own pack), with each round's diff from `compare`
        (base, head: the code host's compare, whose merge base is the round's)."""
        built = []
        for round_ in rounds:
            if round_.commit == item.reviewed_commit:
                continue
            if not rebuild and self._store.get_round_pack(item.key, round_.commit) is not None:
                continue
            diff = compare(item.pr.base_oid, round_.commit)
            pack = self.build(item, head=round_.commit, diff=diff)
            self._store.save_round_pack(pack)
            built.append(pack)
        return built

    def build(self, item: HarvestedPR, *, head: str | None = None, diff: Compare | None = None) -> ContextPack:
        """The pack at `head` (default: the reviewed commit), from `diff` (default: the PR's reviewed diff)."""
        started = time.monotonic()
        own = head is None or head == item.reviewed_commit
        diff = diff if diff is not None else item.pr.reviewed_diff
        if diff is None or not diff.merge_base:
            raise PackError("the PR has no reviewed diff (merge base unknown)")
        head, base = head or item.reviewed_commit, diff.merge_base
        packer = _Packer(self._budget, self._store.put_blob)
        heads = [f.path for f in diff.files if f.status != "removed"]
        changed = sorted({f.path for f in diff.files} | {f.previous_path for f in diff.files if f.previous_path})

        texts = self._add_changed(packer, diff.files, head, base, heads)
        ranked = symbols.rank(self._symbols(diff.files, texts), self._budget.max_symbols)
        tree = self._reader.list_files(head)
        scope = grep_scope(tree, heads, self._budget)
        for path in guidance_paths(tree, changed):
            packer.add(path, base, PackRole.GUIDANCE, "review guidance, at the merge base",
                       lambda p=path: self._reader.read(p, base))  # fmt: skip

        tests = testfiles.near_tests(heads, tree, self._budget.max_tests)
        self._reader.prefetch(head, [path for path, _ in tests])
        for path, reason in tests:
            packer.add(path, head, PackRole.TEST, reason, lambda p=path: self._reader.read(p, head))

        referencing, common = self._referencing(ranked, head, scope, set(changed))
        ordered = sorted(
            referencing,
            key=lambda p: (-len(referencing[p]), min(testfiles.distance(p, c) for c in heads or changed), p),
        )
        for rank, path in enumerate(ordered):
            if rank >= self._budget.max_referencing:
                packer.skip(path, "referencing: over max_referencing")
                continue
            role = PackRole.TEST if testfiles.is_test(path) else PackRole.REFERENCING
            reason = "; ".join(s.reason for s in referencing[path][:3])
            packer.add(path, head, role, reason, lambda p=path: self._reader.read(p, head))

        tree_blob = self._store.put_blob("\n".join(tree).encode()) if self._budget.tree_listing else None
        return ContextPack(
            repo=item.pr.repo,
            number=item.pr.number,
            base_commit=base,
            head_commit=head,
            files=tuple(packer.files),
            changed_paths=tuple(changed),
            symbols=tuple(s.name for s in ranked if s.name not in common),
            common_symbols=tuple(common),
            grep_scope=scope,
            skipped=tuple(packer.skipped),
            tree_blob=tree_blob,
            build_seconds=round(time.monotonic() - started, 2),
            diff=None if own else diff,
        )

    def _add_changed(
        self, packer: _Packer, files: Sequence[FilePatch], head: str, base: str, heads: list[str]
    ) -> dict[tuple[str, str], str]:
        """Changed files at head, then at base. Returns the texts read, keyed by (path, "head" or "base")."""
        bases = [f.previous_path or f.path for f in files if f.status != "added"]
        self._reader.prefetch(head, heads)
        self._reader.prefetch(base, bases)
        texts: dict[tuple[str, str], str] = {}
        for f in files:
            if f.status != "removed":
                reason = f"changed by the PR ({f.status}), at the reviewed commit"
                text = packer.add(f.path, head, PackRole.CHANGED, reason, lambda p=f.path: self._reader.read(p, head))
                if text is not None:
                    texts[(f.path, "head")] = text
        for f in files:
            if f.status != "added":
                old = f.previous_path or f.path
                reason = f"changed by the PR ({f.status}), before it (merge base)"
                text = packer.add(old, base, PackRole.CHANGED, reason, lambda p=old: self._reader.read(p, base))
                if text is not None:
                    texts[(f.path, "base")] = text
        return texts

    @staticmethod
    def _symbols(files: Sequence[FilePatch], texts: dict[tuple[str, str], str]) -> list[symbols.Symbol]:
        found: list[symbols.Symbol] = []
        for f in files:
            if testfiles.is_test(f.path):
                continue  # nothing calls a test, and test modules are not imported
            patch = f.patch
            if patch is None and (f.path, "head") in texts and (f.path, "base") in texts:
                patch = patches.unified_diff(texts[(f.path, "base")], texts[(f.path, "head")], f.path)
            found += symbols.from_patch(f.path, patch or "") + symbols.from_path(f.path)
        return found

    def _referencing(
        self, ranked: Sequence[symbols.Symbol], head: str, scope: tuple[str, ...], changed: set[str]
    ) -> tuple[dict[str, list[symbols.Symbol]], list[str]]:
        """Source files mentioning each symbol (whole word), minus changed files; a symbol found in too many files
        is dropped. Non-source matches (docs, changelogs, data) are not callers and are ignored."""
        found: dict[str, list[symbols.Symbol]] = {}
        dropped: list[str] = []
        for symbol in ranked:
            hits = self._reader.grep(symbol.pattern, head, list(scope) or None, word=True, max_per_file=1)
            paths = {h.path for h in hits if symbols.language_of(h.path)} - changed
            if len(paths) > self._budget.max_files_per_symbol:
                dropped.append(symbol.name)
                continue
            for path in paths:
                found.setdefault(path, []).append(symbol)
        return found, dropped


def summarize(packs: Sequence[ContextPack]) -> dict[str, object]:
    """Size and composition statistics over packs."""
    if not packs:
        return {"packs": 0}

    def p90(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]

    files = [float(len(p.files)) for p in packs]
    sizes = [float(p.total_bytes) for p in packs]
    seconds = [p.build_seconds for p in packs]
    roles = Counter(f.role.value for p in packs for f in p.files)
    skips = Counter(s.reason.split(":", 1)[-1].strip() if ":" in s.reason else s.reason for p in packs
                    for s in p.skipped)  # fmt: skip
    return {
        "packs": len(packs),
        "files_median": statistics.median(files),
        "files_p90": p90(files),
        "bytes_median": statistics.median(sizes),
        "bytes_p90": p90(sizes),
        "seconds_median": statistics.median(seconds),
        "seconds_p90": p90(seconds),
        "roles": dict(roles),
        "whole_tree_grep": sum(not p.grep_scope for p in packs),
        "skipped": dict(skips.most_common(8)),
    }
