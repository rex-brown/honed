"""Corpus statistics: counts by repo, language, outcome and author kind. Pure over the store's fact rows."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from honed.core.types import AuthorKind, LineBasis, Outcome, PRFact, ThreadFact


@dataclass(frozen=True)
class Table:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def _outcome_row(label: str, threads: Sequence[ThreadFact]) -> tuple[str, ...]:
    n = len(threads)
    outcomes = Counter(t.outcome for t in threads)
    changed = sum(t.lines_changed for t in threads)
    by_compare = sum(t.line_basis is LineBasis.COMPARE for t in threads)
    return (label, str(n), *(_pct(outcomes[o], n) for o in Outcome), _pct(changed, n), _pct(by_compare, n))


_OUTCOME_HEADERS = ("n", *(o.value for o in Outcome), "lines_changed", "by_compare")


def tables(prs: Sequence[PRFact], threads: Sequence[ThreadFact]) -> list[Table]:
    threads_by_repo: dict[str, list[ThreadFact]] = defaultdict(list)
    for t in threads:
        threads_by_repo[t.repo].append(t)
    prs_by_repo: dict[tuple[str, str, str], list[PRFact]] = defaultdict(list)
    for p in prs:
        prs_by_repo[(p.repo, p.language, p.corpus.value)].append(p)

    repo_rows = []
    for (repo, language, corpus), group in sorted(prs_by_repo.items()):
        mine = [t for t in threads_by_repo[repo] if t.corpus.value == corpus]
        kinds = Counter(t.author_kind for t in mine)
        repo_rows.append(
            (repo, language, corpus, str(len(group)), str(len(mine)), str(kinds[AuthorKind.HUMAN]),
             str(kinds[AuthorKind.AI]), str(kinds[AuthorKind.BOT] + kinds[AuthorKind.PR_AUTHOR]))
        )  # fmt: skip

    language_rows = []
    for language in sorted({p.language for p in prs}):
        group = [p for p in prs if p.language == language]
        language_rows.append((language, str(len(group)), str(sum(p.threads for p in group))))

    kind_rows = [
        _outcome_row(kind.value, [t for t in threads if t.author_kind is kind])
        for kind in AuthorKind
        if any(t.author_kind is kind for t in threads)
    ]
    human_rows = [
        _outcome_row(repo, [t for t in group if t.author_kind is AuthorKind.HUMAN])
        for repo, group in sorted(threads_by_repo.items())
        if any(t.author_kind is AuthorKind.HUMAN for t in group)
    ]
    ai_rows = [
        _outcome_row(repo, [t for t in group if t.author_kind is AuthorKind.AI])
        for repo, group in sorted(threads_by_repo.items())
        if any(t.author_kind is AuthorKind.AI for t in group)
    ]
    author_rows = [(kind.value, str(n)) for kind, n in sorted(Counter(p.author_kind for p in prs).items())]
    return [
        Table("PRs and threads by repo", ("repo", "language", "corpus", "prs", "threads", "human", "ai", "other"),
              tuple(repo_rows)),
        Table("PRs by language", ("language", "prs", "threads"), tuple(language_rows)),
        Table("PRs by author kind", ("author_kind", "prs"), tuple(author_rows)),
        Table("Thread outcomes by author kind", ("author_kind", *_OUTCOME_HEADERS), tuple(kind_rows)),
        Table("Human-thread outcomes by repo", ("repo", *_OUTCOME_HEADERS), tuple(human_rows)),
        Table("AI-thread outcomes by repo", ("repo", *_OUTCOME_HEADERS), tuple(ai_rows)),
    ]  # fmt: skip


def render(table: Table) -> str:
    """A plain-text, column-aligned table."""
    rows = [table.headers, *table.rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(table.headers))]
    lines = [f"## {table.title}"]
    for n, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)
