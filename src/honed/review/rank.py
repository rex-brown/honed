"""Dedup and rank (ARCHITECTURE.md section 4, step 6): merge duplicates while recording every panel member or check
that raised them, apply the severity rules, the confidence threshold and the nit cap, flag a review with too many
"act on" findings, and in re-review mode post only new Important findings, never repeating a dismissed one.

The safety invariant is enforced again here: nothing in this module lowers the severity of, or stops posting, a
finding in a `[safety] high_risk_categories` category because of a lesson.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from honed.core.policy import RankRules
from honed.core.reviews import ReviewRequest
from honed.core.types import Bucket, Finding, Severity

_WORD = re.compile(r"[a-z0-9_]{3,}")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "when",
        "not",
        "are",
        "was",
        "were",
        "has",
        "have",
        "its",
        "can",
        "will",
        "than",
        "then",
    }
)
SEVERITY_RANK = {Severity.IMPORTANT: 0, Severity.PRE_EXISTING: 1, Severity.NIT: 2}
BUCKET_ORDER = {Bucket.ACT_ON: 0, Bucket.CONSIDER: 1, Bucket.NOTED: 2, Bucket.DISMISSED: 3, None: 4}
SIMILARITY = 0.25  # word overlap (Jaccard) of two findings' titles and first lines that makes them one issue


def _words(finding: Finding) -> set[str]:
    first = finding.body.split(". ")[0] if finding.body else ""
    return {w for w in _WORD.findall(f"{finding.title} {first}".lower()) if w not in _STOP}


def overlaps(a: Finding, b: Finding, slack: int) -> bool:
    return a.path == b.path and a.start_line - slack <= b.end_line and b.start_line - slack <= a.end_line


def similar(a: Finding, b: Finding, slack: int) -> bool:
    """Same place (overlapping lines, within `slack`) and the same problem (enough shared words)."""
    if not overlaps(a, b, slack):
        return False
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= SIMILARITY


def _union(*groups: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for group in groups:
        out += [x for x in group if x not in out]
    return tuple(out)


def merge(keep: Finding, other: Finding) -> Finding:
    """`keep`, also raised by whoever raised `other`."""
    return replace(keep, raised_by=_union(keep.raised_by, other.raised_by),
                   lessons_cited=_union(keep.lessons_cited, other.lessons_cited))  # fmt: skip


def merge_duplicates(findings: Sequence[Finding], slack: int) -> list[Finding]:
    """One finding per issue, keeping the most severe and most detailed wording, raised_by the union."""
    groups: list[list[Finding]] = []
    for finding in findings:
        group = next((g for g in groups if any(similar(finding, other, slack) for other in g)), None)
        if group is None:
            groups.append([finding])
        else:
            group.append(finding)
    out = []
    for group in groups:
        best = min(group, key=lambda f: (SEVERITY_RANK[f.severity], f.raised_by[0].startswith("check:"),
                                         -len(f.body) - len(f.trace)))  # fmt: skip
        for other in group:
            if other is not best:
                best = merge(best, other)
        out.append(best)
    return out


def finding_id(finding: Finding, head_commit: str) -> str:
    key = f"{head_commit}\0{finding.path}\0{finding.start_line}\0{finding.title}"
    return "f" + hashlib.sha1(key.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class Ranked:
    findings: tuple[Finding, ...]
    noted: int
    act_on_flagged: bool


def _note(finding: Finding, reason: str) -> Finding:
    return replace(finding, bucket=Bucket.NOTED, bucket_reason=reason)


def _severity_rules(finding: Finding, rules: RankRules, high_risk: Collection[str]) -> Finding:
    if finding.category in rules.nit_only_categories and finding.category not in high_risk:
        finding = replace(finding, severity=Severity.NIT)
    if finding.bucket is Bucket.ACT_ON and finding.severity is Severity.NIT:
        finding = replace(finding, bucket=Bucket.CONSIDER)
    if finding.bucket is Bucket.CONSIDER and finding.severity is not Severity.NIT:
        finding = replace(finding, severity=Severity.NIT)  # "consider" is posted as a Nit
    return finding


def _rereview(findings: list[Finding], request: ReviewRequest, slack: int) -> list[Finding]:
    """Re-review: post only new Important findings, and never repeat one a human dismissed."""
    out = []
    for finding in findings:
        repeats = [p for p in request.prior_findings if similar(finding, p.finding, slack)
                   or (overlaps(finding, p.finding, 0) and finding.category == p.finding.category)]  # fmt: skip
        if not finding.posted:
            out.append(finding)
        elif any(p.dismissed for p in repeats):
            out.append(replace(finding, bucket=Bucket.DISMISSED,
                               bucket_reason="a human dismissed this finding in an earlier review"))  # fmt: skip
        elif repeats:
            out.append(_note(finding, "already raised in an earlier review"))
        elif finding.severity is not Severity.IMPORTANT:
            out.append(_note(finding, "a re-review posts only new Important findings"))
        else:
            out.append(finding)
    return out


def rank(findings: Sequence[Finding], request: ReviewRequest, rules: RankRules, high_risk: Collection[str],
         policy_hash: str) -> Ranked:  # fmt: skip
    """Final buckets, severities, ids and order."""
    out = [_severity_rules(f, rules, high_risk) for f in findings]
    out = [_note(f, f"confidence {f.confidence:.2f} is below the threshold") if f.posted
           and f.confidence < rules.confidence_threshold else f for f in out]  # fmt: skip
    if request.rereview:
        out = _rereview(out, request, rules.dedup_line_slack)
    nits = sorted((f for f in out if f.bucket is Bucket.CONSIDER),
                  key=lambda f: (-f.confidence, not f.consensus, -f.evidence_level))  # fmt: skip
    over = {id(f) for f in nits[rules.nit_cap :]}
    out = [_note(f, "over the nit cap") if id(f) in over else f for f in out]
    seen: set[str] = set()
    final = []
    for f in out:
        fid = finding_id(f, request.head_commit)
        while fid in seen:
            fid += "x"
        seen.add(fid)
        final.append(replace(f, id=fid, policy_hash=policy_hash))
    final.sort(key=lambda f: (BUCKET_ORDER[f.bucket], SEVERITY_RANK[f.severity], -f.confidence, f.path, f.start_line))
    act_on = sum(f.bucket is Bucket.ACT_ON for f in final)
    return Ranked(tuple(final), sum(f.bucket is Bucket.NOTED for f in final), act_on > rules.act_on_flag)
