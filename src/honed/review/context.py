"""What the reviewer reads (ARCHITECTURE.md section 4, step 1), within the policy's context budget.

- the diff, shown as numbered listings of the new file with added lines marked and removed lines inline;
- the code around each hunk, and lines elsewhere that reference a symbol the diff defines or changes (callers);
- the repo's REVIEW.md / CLAUDE.md, read at the merge base, so a change can't loosen its own review;
- review threads from earlier rounds of the same PR, as they stood when this round began;
- review threads on the same files from before the PR (pstack `why`: how the code got this way);
- the lessons whose scope matches the change.
Code is read only through the `CodeReader`, as of the request's commits. A read of a file that exists at the head
(per the reader's file listing) but that the reader can't serve is a miss: offline, that is the pack hit rate.
"""

from __future__ import annotations

import logging
import posixpath
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

from honed.core import patches, symbols, testfiles
from honed.core.policy import ContextBudget
from honed.core.reviews import ContextStats, ReviewRequest
from honed.core.types import FilePatch, Outcome, PRKey
from honed.ports.code_reader import CodeReader
from honed.ports.store import Store

log = logging.getLogger(__name__)

GUIDANCE_FILES = ("REVIEW.md", "CLAUDE.md")
LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum")
_GENERATED = (".min.js", ".map", ".snap", ".svg", ".lock")
MAX_COMMENT_CHARS = 1200
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)  # PR templates' hidden instructions to the author
_OUTCOME_WORDS = {
    Outcome.FIXED: "the code changed after it",
    Outcome.RESOLVED_NO_CHANGE: "resolved without a change",
    Outcome.OPEN_AT_MERGE: "discussed, left unchanged",
    Outcome.IGNORED: "no reply, left unchanged",
    Outcome.THUMBS_DOWN: "voted down",
    Outcome.CHANGED_UNADDRESSED: "the code changed for another reason",
}


@dataclass(frozen=True)
class FileView:
    """One changed file as the context shows it."""

    patch: FilePatch
    head: str | None  # the file at the head commit, when read
    base: str | None  # at the merge base
    added: frozenset[int]  # new-file line numbers the diff adds
    hunks: tuple[tuple[int, int], ...]  # new-file line ranges the hunks cover


@dataclass(frozen=True)
class ReviewContext:
    text: str  # the untrusted block's contents
    guidance: str  # the repo's review guidance, or ""
    files: tuple[FileView, ...]
    stats: ContextStats

    def file(self, path: str) -> FileView | None:
        return next((f for f in self.files if f.patch.path == path), None)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.patch.path for f in self.files)


@dataclass
class _Reads:
    """Reads of files that exist at the commit read, and how many the reader served."""

    tree: set[str] | None
    reads: int = 0
    served: int = 0
    missed: list[str] = field(default_factory=list)

    def read(self, reader: CodeReader, path: str, commit: str, *, known: bool = False) -> str | None:
        exists = known or self.tree is None or path in self.tree
        text = reader.read(path, commit)
        if exists:
            self.reads += 1
            if text is not None:
                self.served += 1
            else:
                self.missed.append(path)
        return text


def _skip_content(path: str) -> bool:
    return posixpath.basename(path) in LOCKFILES or path.endswith(_GENERATED)


def _hunk_lines(patch: str) -> tuple[frozenset[int], dict[int, list[str]], tuple[tuple[int, int], ...]]:
    """Added new-file lines, removed lines keyed by the new-file line they precede, and each hunk's new-file range."""
    added: set[int] = set()
    removed: dict[int, list[str]] = {}
    ranges = []
    for hunk in patches.parse_hunks(patch):
        new = hunk.new_start if hunk.new_count else hunk.new_start + 1
        first = new
        for line in hunk.lines:
            tag = line[:1]
            if tag == "+":
                added.add(new)
                new += 1
            elif tag == "-":
                removed.setdefault(new, []).append(line[1:])
            elif tag in (" ", ""):
                new += 1
        ranges.append((first, max(first, new - 1)))
    return frozenset(added), removed, tuple(ranges)


def numbered_diff(head: str, patch: str, context: int) -> list[str]:
    """The new file around each hunk, numbered: `+` marks added lines, `-` lines show removed text in place."""
    lines = head.splitlines()
    added, removed, ranges = _hunk_lines(patch)
    windows: list[list[int]] = []
    for start, end in ranges:
        lo, hi = max(1, start - context), min(len(lines), end + context)
        if windows and lo <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    width = len(str(len(lines) or 1))
    out: list[str] = []
    for n, (lo, hi) in enumerate(windows):
        if n:
            out.append(" " * width + "   ...")
        for number in range(lo, hi + 2):
            out += [f"{'':>{width}} - | {text}" for text in removed.get(number, [])]
            if number <= hi:
                out.append(f"{number:>{width}} {'+' if number in added else ' '} | {lines[number - 1]}")
    return out


class ContextBuilder:
    def __init__(self, budget: ContextBudget, store: Store | None = None) -> None:
        self._budget = budget
        self._store = store
        self._store_lock = threading.Lock()  # the store's connection is shared by concurrent reviews

    def build(self, request: ReviewRequest, reader: CodeReader) -> ReviewContext:
        budget = self._budget
        try:
            tree = set(reader.list_files(request.head_commit))
        except Exception as error:
            log.info("no file listing at %s: %s", request.head_commit[:10], error)
            tree = None
        reads = _Reads(tree or None)
        ordered = sorted(request.files, key=lambda f: (_skip_content(f.path), testfiles.is_test(f.path), f.path))
        views: list[FileView] = []
        sections: list[str] = []
        used = 0
        truncated = False
        for n, patch in enumerate(ordered):
            view, lines = self._file(patch, request, reader, reads, n < budget.max_files)
            views.append(view)
            if used + len(lines) > budget.max_lines:
                keep = max(0, budget.max_lines - used)
                lines = [*lines[:keep], f"... ({len(lines) - keep} more lines of this file not shown)"] if keep else []
                truncated = True
            if lines:
                sections.append("\n".join(lines))
                used += len(lines)
        header = self._header(request)
        callers = self._callers(request, views, reader)
        earlier = self._earlier(request)
        prior = self._prior_threads(request)
        parts = [header, "## The diff and the code around it", *(sections or ["(no file contents available)"])]
        if callers:
            parts += ["## Other code that references symbols this change defines or changes", callers]
        if earlier:
            parts += ["## Earlier review discussion on this pull request", earlier]
        if prior:
            parts += ["## Review threads on these files from before this pull request", prior]
        guidance = self._guidance(request, reader, reads)
        if reads.missed:
            log.info("%s: %d file reads not served: %s", request.ref, len(reads.missed), reads.missed[:5])
        stats = ContextStats(reads=reads.reads, served=reads.served, lines=used, truncated=truncated)
        return ReviewContext("\n\n".join(parts), guidance, tuple(views), stats)

    # ---- pieces ------------------------------------------------------------------------------------------

    @staticmethod
    def _header(request: ReviewRequest) -> str:
        out = [f"Pull request: {request.ref}" if request.number is not None else f"Change in: {request.repo}",
               f"Title: {request.title}", f"Author: {request.author}"]  # fmt: skip
        body = _HTML_COMMENT.sub("", request.body).strip()
        if body:
            out += ["Description:", body[:6000]]
        if request.commit_messages:
            out += ["Commits, oldest first:", *(f"- {m}" for m in request.commit_messages[-30:])]
        out.append(f"Changed files ({len(request.files)}):")
        for f in request.files:
            renamed = f", renamed from {f.previous_path}" if f.previous_path and f.previous_path != f.path else ""
            out.append(f"- {f.path} ({f.status}{renamed})")
        return "\n".join(out)

    def _file(self, patch: FilePatch, request: ReviewRequest, reader: CodeReader, reads: _Reads,
              show: bool) -> tuple[FileView, list[str]]:  # fmt: skip
        head = base = None
        if show and not _skip_content(patch.path):
            if patch.status != "removed":
                head = reads.read(reader, patch.path, request.head_commit, known=True)
            if patch.status != "added":
                base = reads.read(reader, patch.previous_path or patch.path, request.base_commit, known=True)
        text = patch.patch
        if text is None and head is not None and base is not None:
            text = patches.unified_diff(base, head, patch.path)
        added, _, ranges = _hunk_lines(text or "")
        view = FileView(patch, head, base, added, ranges)
        moved = patch.previous_path and patch.previous_path != patch.path
        title = f"### {patch.path} ({patch.status}{f', renamed from {patch.previous_path}' if moved else ''})"
        if not show:
            return view, [f"{title}: not shown (over the file budget)"]
        if _skip_content(patch.path):
            return view, [f"{title}: generated or lock file, not shown"]
        if text is None:
            return view, [f"{title}: no diff available (binary or too large)"]
        if head is not None and patch.status != "removed":
            listing = numbered_diff(head, text, self._budget.hunk_context_lines)
            return view, [title, "New-file line numbers on the left; `+` marks added lines, `-` shows removed ones.",
                          *listing]  # fmt: skip
        return view, [title, "Unified diff:", *text.splitlines()]

    def _callers(self, request: ReviewRequest, views: Sequence[FileView], reader: CodeReader) -> str:
        if self._budget.max_callers <= 0:
            return ""
        found: list[symbols.Symbol] = []
        for view in views:
            if not testfiles.is_test(view.patch.path) and view.patch.patch:
                found += symbols.from_patch(view.patch.path, view.patch.patch)
        changed = {v.patch.path for v in views}
        lines: list[str] = []
        for symbol in symbols.rank(found, 8):
            try:
                hits = reader.grep(symbol.pattern, request.head_commit, None, word=True, max_per_file=2)
            except Exception as error:
                log.info("grep for %s failed: %s", symbol.name, error)
                continue
            for hit in hits:
                if hit.path in changed or not symbols.language_of(hit.path):
                    continue
                lines.append(f"{hit.path}:{hit.line} ({symbol.name}) | {hit.text.strip()[:200]}")
                if len(lines) >= self._budget.max_callers:
                    return "\n".join(lines)
        return "\n".join(lines)

    def _earlier(self, request: ReviewRequest) -> str:
        out = []
        for thread in request.earlier_threads[: self._budget.max_earlier_threads]:
            where = thread.path + (f", lines {thread.lines[0]}-{thread.lines[1]}" if thread.lines else "")
            comments = [f"  [{c.role}] {c.author}: {c.body.strip()[:MAX_COMMENT_CHARS]}" for c in thread.comments]
            out.append(f"- {where}\n" + "\n".join(comments))
        return "\n".join(out)

    def _prior_threads(self, request: ReviewRequest) -> str:
        if self._store is None or request.number is None or not request.created_at:
            return ""
        paths = [f.path for f in request.files]
        with self._store_lock:
            prior = self._store.threads_before(request.repo, paths, request.created_at)
            prior = [t for t in prior if t.author_kind.value in ("human", "ai")][-self._budget.max_prior_threads :]
            bodies = {}
            for key in {PRKey(t.repo, t.number) for t in prior}:
                item = self._store.get_pr(key)
                if item is not None:
                    bodies.update({(key, th.id): th.first.body for th in item.pr.threads if th.first})
        out = []
        for t in reversed(prior):
            body = bodies.get((PRKey(t.repo, t.number), t.thread_id), "").strip()
            if body:
                what = _OUTCOME_WORDS.get(t.outcome, t.outcome.value)
                out.append(f"- {t.path} (#{t.number}, {t.created_at[:10]}; {what}): {body[:MAX_COMMENT_CHARS]}")
        return "\n".join(out)

    def _guidance(self, request: ReviewRequest, reader: CodeReader, reads: _Reads) -> str:
        """REVIEW.md and CLAUDE.md at the merge base: at the root, then in each changed file's directories."""
        dirs: list[str] = [""]
        for f in request.files:
            parts = posixpath.dirname(f.path).split("/")
            for depth in range(1, len(parts) + 1):
                d = "/".join(parts[:depth])
                if d and d not in dirs:
                    dirs.append(d)
        candidates = [posixpath.join(d, name) if d else name for d in dirs for name in GUIDANCE_FILES]
        if reads.tree is not None:
            candidates = [c for c in candidates if c in reads.tree]
        else:
            candidates = candidates[: len(GUIDANCE_FILES)]  # without a listing, only the root files
        out, left = [], self._budget.max_guidance_chars
        for path in candidates:
            if left <= 0:
                break
            text = reads.read(reader, path, request.base_commit)
            if text:
                piece = text.strip()[:left]
                out.append(f"### {path}\n{piece}")
                left -= len(piece)
        return "\n\n".join(out)
