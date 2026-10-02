"""How many PRs to harvest where: per-language quotas, spread over repos and over slices of the creation window."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence

from honed.core.types import DateWindow


def spread(total: int, slots: int) -> list[int]:
    """`total` split as evenly as possible over `slots`; earlier slots take the remainder."""
    if slots <= 0:
        return []
    base, extra = divmod(max(total, 0), slots)
    return [base + (1 if i < extra else 0) for i in range(slots)]


def share_of(total: int, share: float) -> int:
    """At least `share` (0 to 1) of `total`, in whole items."""
    return min(max(total, 0), math.ceil(round(max(total, 0) * share, 9)))


def allocate(total: int, caps: Sequence[int]) -> list[int]:
    """`total` split as evenly as possible over slots that each hold at most `caps[i]` (water-filling)."""
    shares = [0] * len(caps)
    remaining = max(total, 0)
    open_slots = [i for i, cap in enumerate(caps) if cap > 0]
    while remaining and open_slots:
        for i, extra in zip(open_slots, spread(remaining, len(open_slots)), strict=True):
            grant = min(extra, caps[i] - shares[i])
            shares[i] += grant
            remaining -= grant
        open_slots = [i for i in open_slots if shares[i] < caps[i]]
    return shares


def round_robin[T](groups: Sequence[Sequence[T]]) -> list[T]:
    """Every item, taking one from each group in turn (the first group first) until all are used up."""
    out: list[T] = []
    for i in range(max((len(g) for g in groups), default=0)):
        out += [g[i] for g in groups if i < len(g)]
    return out


def largest_remainder(total: int, weights: Mapping[str, float]) -> dict[str, int]:
    """Integer shares of `total` proportional to `weights` that sum exactly to `total`."""
    weight_sum = sum(weights.values())
    if total <= 0 or weight_sum <= 0:
        return {key: 0 for key in weights}
    exact = {key: total * w / weight_sum for key, w in weights.items()}
    shares = {key: int(value) for key, value in exact.items()}
    by_remainder = sorted(weights, key=lambda k: (-(exact[k] - shares[k]), k))
    for key in by_remainder[: total - sum(shares.values())]:
        shares[key] += 1
    return shares


def repo_quotas(groups: Mapping[str, Sequence[str]], weights: Mapping[str, float], total: int) -> dict[str, int]:
    """Per-repo PR quotas: `total` split across language groups by `weights`, then evenly across each group's repos."""
    missing = set(groups) - set(weights)
    if missing:
        raise ValueError(f"language groups without a weight: {sorted(missing)}")
    per_group = largest_remainder(total, {key: weights[key] for key in groups})
    quotas: dict[str, int] = {}
    for key, repos in groups.items():
        for repo, quota in zip(repos, spread(per_group[key], len(repos)), strict=True):
            quotas[repo] = quota
    return quotas


def split_window(window: DateWindow, slices: int) -> list[DateWindow]:
    """`slices` consecutive, non-overlapping date windows covering `window`."""
    start = dt.date.fromisoformat(window.start)
    end = dt.date.fromisoformat(window.end)
    days = (end - start).days + 1
    if days <= 0 or slices <= 0:
        raise ValueError(f"cannot split {window} into {slices} slices")
    slices = min(slices, days)
    bounds = [start + dt.timedelta(days=i * days // slices) for i in range(slices + 1)]
    return [
        DateWindow(bounds[i].isoformat(), (bounds[i + 1] - dt.timedelta(days=1)).isoformat()) for i in range(slices)
    ]
