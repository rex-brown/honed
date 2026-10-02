"""Labeling reports: the addressed-check split, stances, high-risk dismissals and gold sets. Pure over stored labels."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from honed.core.labels import REVIEW_KINDS
from honed.core.types import Addressed, GoldSet, JudgedLabel, Outcome, Polarity, PRKey, Severity, Stance
from honed.learn.stats import Table


def _pct(part: int, whole: int) -> str:
    return f"{part} ({100 * part / whole:.0f}%)" if whole else "0"


def tables(judged: Mapping[PRKey, Sequence[JudgedLabel]], gold: Mapping[PRKey, GoldSet | None]) -> list[Table]:
    findings = [lab for labs in judged.values() for lab in labs if lab.author_kind in REVIEW_KINDS]
    kinds = sorted({lab.author_kind for lab in findings}, key=lambda k: k.value)

    addressed_rows = []
    for kind in kinds:
        mine = [lab for lab in findings if lab.author_kind is kind]
        fixed = [lab for lab in mine if lab.outcome in (Outcome.FIXED, Outcome.CHANGED_UNADDRESSED)]
        applied = sum(lab.applied_suggestion for lab in fixed)
        verdicts = Counter(lab.addressed for lab in fixed if not lab.applied_suggestion)
        judged_n = sum(verdicts[a] for a in Addressed)
        confirmed = applied + verdicts[Addressed.ADDRESSED] + verdicts[Addressed.PARTIALLY]
        addressed_rows.append((
            kind.value, str(len(fixed)), str(applied), str(judged_n), _pct(verdicts[Addressed.ADDRESSED], judged_n),
            _pct(verdicts[Addressed.PARTIALLY], judged_n), _pct(verdicts[Addressed.NOT_ADDRESSED], judged_n),
            str(verdicts[None]), _pct(confirmed, len(fixed)),
        ))  # fmt: skip

    signal_rows = []
    for kind in kinds:
        mine = [lab for lab in findings if lab.author_kind is kind]
        polarity = Counter(lab.polarity for lab in mine)
        stance = Counter(lab.stance for lab in mine if lab.stance)
        signal_rows.append((
            kind.value, str(len(mine)), *(str(polarity[p]) for p in Polarity), str(polarity[None]),
            *(str(stance[s]) for s in Stance), str(sum(lab.high_risk_dismissal for lab in mine)),
        ))  # fmt: skip

    gold_rows = []
    for key in sorted(gold, key=str):
        g = gold[key]
        if g is None:
            gold_rows.append((str(key), "-", "-", "-", "not built", "-", "-", "-"))
            continue
        sev = Counter(i.severity for i in g.issues)
        gold_rows.append((
            str(key), str(g.candidates), str(len(g.excluded_later_round)), str(len(g.excluded_unreadable)),
            str(len(g.issues)), *(str(sev[s]) for s in Severity),
        ))  # fmt: skip
    built = [g for g in gold.values() if g is not None]
    issues = [i for g in built for i in g.issues]
    if len(gold_rows) > 1:
        sev = Counter(i.severity for i in issues)
        gold_rows.append((
            "total", str(sum(g.candidates for g in built)), str(sum(len(g.excluded_later_round) for g in built)),
            str(sum(len(g.excluded_unreadable) for g in built)), str(len(issues)), *(str(sev[s]) for s in Severity),
        ))  # fmt: skip

    def mix(title: str, name: str, counts: Counter[str]) -> Table:
        rows = tuple((k, _pct(v, len(issues))) for k, v in counts.most_common())
        return Table(title, (name, "issues"), rows)

    return [
        Table("Addressed check on fixed findings",
              ("author", "fixed", "applied_sugg", "judged", "addressed", "partially", "not_addressed", "pending",
               "confirmed"), tuple(addressed_rows)),
        Table("Judged labels of review findings",
              ("author", "n", *(p.value for p in Polarity), "pending", *(s.value for s in Stance),
               "high_risk_dismissals"), tuple(signal_rows)),
        Table("Gold sets by PR",
              ("pr", "candidates", "later_round", "unreadable", "issues", *(s.value for s in Severity)),
              tuple(gold_rows)),
        mix("Gold issues by severity", "severity", Counter(i.severity.value for i in issues)),
        mix("Gold issues by category", "category", Counter(i.category for i in issues)),
        mix("Gold issues by provenance", "provenance", Counter(i.provenance.value for i in issues)),
    ]  # fmt: skip
