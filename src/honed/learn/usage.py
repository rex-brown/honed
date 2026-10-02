"""Model usage from the call ledger: calls, tokens and list-price shadow cost per stage, per PR and per run.
Pure over `LedgerEntry` rows."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Sequence

from honed.learn.stats import Table
from honed.ports.call_store import LedgerEntry

_HEADERS = ("calls", "cached", "failed", "input_tok", "cache_read_tok", "output_tok", "shadow_usd", "s_per_call")


def _row(label: str, entries: Sequence[LedgerEntry]) -> tuple[str, ...]:
    live = [e for e in entries if not e.cached]
    ok = [e for e in live if e.ok]
    seconds = sum(e.duration_s for e in ok)
    return (
        label,
        str(len(live)),
        str(sum(e.cached for e in entries)),
        str(sum(not e.ok for e in live)),
        str(sum(e.input_tokens + e.cache_write_tokens for e in live)),
        str(sum(e.cache_read_tokens for e in live)),
        str(sum(e.output_tokens for e in live)),
        f"{sum(e.cost_usd for e in live):.2f}",
        f"{seconds / len(ok):.1f}" if ok else "-",
    )


def _grouped(entries: Sequence[LedgerEntry], key: Callable[[LedgerEntry], str]) -> tuple[tuple[str, ...], ...]:
    groups: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        groups[key(entry)].append(entry)
    rows = [_row(name, group) for name, group in sorted(groups.items())]
    return (*rows, _row("total", entries)) if len(groups) > 1 else tuple(rows)


def _wall_clock(entries: Sequence[LedgerEntry]) -> str:
    times = sorted(dt.datetime.fromisoformat(e.at) for e in entries)
    return f"{(times[-1] - times[0]).total_seconds() / 60:.1f} min" if len(times) > 1 else "-"


def tables(entries: Sequence[LedgerEntry], run_id: str | None = None) -> list[Table]:
    if run_id is not None:
        entries = [e for e in entries if e.run_id == run_id]
    by_run: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        by_run[entry.run_id].append(entry)
    run_rows = tuple(
        (run, group[0].at[:19], _wall_clock(group), *_row("", group)[1:])
        for run, group in sorted(by_run.items(), key=lambda item: item[1][0].at)
    )
    return [
        Table("Model usage by stage", ("stage", *_HEADERS), _grouped(entries, lambda e: e.stage)),
        Table("Model usage by PR", ("pr", *_HEADERS), _grouped(entries, lambda e: e.pr or "-")),
        Table("Model usage by run", ("run", "started", "wall_clock", *_HEADERS), run_rows),
    ]
