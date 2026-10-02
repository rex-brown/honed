"""Agreement statistics for judge audits (METRICS.md section 5): accuracy, Cohen's kappa, Fleiss' kappa, repeat
consistency."""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Mapping, Sequence
from itertools import combinations


def accuracy(pairs: Sequence[tuple[Hashable, Hashable]]) -> float | None:
    """Share of (truth, verdict) pairs that agree; None without pairs."""
    return sum(t == v for t, v in pairs) / len(pairs) if pairs else None


def cohen_kappa(pairs: Sequence[tuple[Hashable, Hashable]]) -> float | None:
    """Cohen's kappa between truth and verdicts. None when undefined: no pairs, or chance agreement is 1 (both
    sides used a single, identical class, so agreement says nothing beyond chance)."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(t == v for t, v in pairs) / n
    truth = Counter(t for t, _ in pairs)
    verdicts = Counter(v for _, v in pairs)
    expected = sum(truth[c] * verdicts[c] for c in truth) / (n * n)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)


def fleiss_kappa(ratings: Sequence[Mapping[Hashable, int]]) -> float | None:
    """Fleiss' kappa among several raters: one mapping per item, from category to how many raters chose it.

    Items with fewer than two ratings are left out. Each item's agreement uses its own number of raters, so when every
    rater rated every item this is Fleiss' kappa exactly. None when undefined: no item with two ratings, or every
    rating in one category (chance agreement is 1)."""
    rated = [r for r in ratings if sum(r.values()) >= 2]
    if not rated:
        return None
    totals: Counter[Hashable] = Counter()
    per_item = []
    for counts in rated:
        n = sum(counts.values())
        per_item.append(sum(c * (c - 1) for c in counts.values()) / (n * (n - 1)))
        totals.update(counts)
    observed = sum(per_item) / len(per_item)
    everyone = sum(totals.values())
    expected = sum((c / everyone) ** 2 for c in totals.values())
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)


def consistency(repeats: Sequence[Sequence[Hashable]]) -> tuple[float | None, float | None]:
    """For items judged several times: (share of items with unanimous verdicts, share of agreeing verdict pairs)."""
    judged = [r for r in repeats if len(r) >= 2]
    if not judged:
        return None, None
    unanimous = sum(len(set(r)) == 1 for r in judged) / len(judged)
    pairs = [a == b for r in judged for a, b in combinations(r, 2)]
    return unanimous, sum(pairs) / len(pairs)


def set_f1(reference: set[Hashable], candidate: set[Hashable]) -> tuple[float | None, float | None, float | None]:
    """(precision, recall, F1) of `candidate` against `reference` (for example match pairs: the offline matcher's
    against the judge's). Precision is None with no candidates, recall None with no reference items, and F1 None when
    either is."""
    hits = len(reference & candidate)
    p = hits / len(candidate) if candidate else None
    r = hits / len(reference) if reference else None
    if p is None or r is None:
        return p, r, None
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)
