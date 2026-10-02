"""`honed rehydrate --comments`: refetch the text a stripped bundle left out (ARCHITECTURE.md section 8).

A stripped bundle (`bundle export --strip-comments`) carries each review comment's and PR description's attribution
and the SHA-256 of its text as exported, not the text. Import marks every such text on its PR (`core.marks`). This
fetches the current text from the code host by comment id (and the description by PR), runs the same redaction the
export ran (`core.redaction`), and keeps it only when its hash matches: the text the labels and gold were made on.
A text that changed since (edited, or deleted) keeps its mark, so the PR is not replayed on text it wasn't labeled
on; `accept_changed` stores the current text anyway (an empty one for a deleted comment) and clears the mark.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from honed.core import marks, redaction
from honed.core.types import HarvestedPR, PRKey
from honed.ports.code_host import TextSource
from honed.ports.store import Store

log = logging.getLogger(__name__)


@dataclass
class TextReport:
    prs: int = 0  # PRs with text to restore
    restored: int = 0  # texts whose hash matched
    accepted: int = 0  # changed texts stored anyway (`accept_changed`)
    changed: list[str] = field(default_factory=list)  # `<pr> comment <id>` / `<pr> body`: the hash didn't match
    missing: list[str] = field(default_factory=list)  # the code host no longer has it
    done: list[str] = field(default_factory=list)  # PRs with no text left to restore


class TextRehydrator:
    def __init__(self, store: Store, source: TextSource, *, accept_changed: bool = False) -> None:
        self._store = store
        self._source = source
        self._accept = accept_changed

    def run(self, keys: Sequence[PRKey]) -> TextReport:
        """Repo by repo, so what was fetched is stored before a stop (the code host's budget) and a re-run resumes."""
        report = TextReport()
        by_repo: dict[str, dict[PRKey, list[str]]] = {}
        for key in sorted(keys, key=lambda k: (k.repo, k.number)):
            found = marks.text_marks(self._store.stripped(key))
            if found:
                by_repo.setdefault(key.repo, {})[key] = found
        report.prs = sum(len(group) for group in by_repo.values())
        for repo, group in sorted(by_repo.items()):
            parsed = {key: [marks.parse_text(m) for m in found] for key, found in group.items()}
            ids = [cid for pairs in parsed.values() for cid, _ in pairs if cid is not None]
            bodies = self._source.comment_bodies(ids) if ids else {}
            numbers = [key.number for key, pairs in parsed.items() if any(cid is None for cid, _ in pairs)]
            descriptions = self._source.pr_bodies(repo, numbers) if numbers else {}
            for key, found in group.items():
                self._one(key, found, bodies, descriptions.get(key.number), report)
        return report

    def _one(self, key: PRKey, text_marks: list[str], bodies: Mapping[str, str | None], description: str | None,
             report: TextReport) -> None:  # fmt: skip
        item = self._store.get_pr(key)
        if item is None:
            return
        texts: dict[str | None, str] = {}  # comment id (None: the description) -> the text to store
        left: list[str] = []
        for mark in text_marks:
            comment_id, digest = marks.parse_text(mark)
            current = description if comment_id is None else bodies.get(comment_id)
            where = f"{key} " + ("body" if comment_id is None else f"comment {comment_id}")
            text = redaction.redact(current).text if current is not None else None
            if text is not None and redaction.sha256(text) == digest:
                texts[comment_id] = text
                report.restored += 1
                continue
            (report.missing if current is None else report.changed).append(where)
            if self._accept:
                texts[comment_id] = text or ""
                report.accepted += 1
            else:
                left.append(mark)
        if texts:
            self._store.upsert_pr(_with_texts(item, texts))
        self._store.mark_stripped(key, [*marks.code_marks(self._store.stripped(key)), *left])
        if not left:
            report.done.append(str(key))
        log.info("%s: %d texts restored, %d left", key, len(texts), len(left))


def _with_texts(item: HarvestedPR, texts: dict[str | None, str]) -> HarvestedPR:
    pr = item.pr
    threads = tuple(replace(t, comments=tuple(replace(c, body=texts[c.id]) if c.id in texts else c
                                              for c in t.comments)) for t in pr.threads)  # fmt: skip
    body = texts.get(None, pr.body)
    return replace(item, pr=replace(pr, body=body, threads=threads))
