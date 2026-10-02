"""Render a review (ARCHITECTURE.md section 4, step 7): JSON for offline and eval runs, and a Markdown summary.

- "act on" findings are inline Important or Pre-existing comments; "consider" findings are inline Nits;
  "noted" findings are a count; "dismissed" ones are a collapsed list with reasons, so a human can override them.
- Every comment carries a hidden marker, `<!-- honed finding=<id> policy=<hash> -->`, so feedback traces back to
  the policy version. An empty review is valid.
- A deterministic lint holds comment text to the writing standard (pstack `unslop`): no hedging, filler, praise,
  chatbot phrases, stock AI vocabulary, em dashes or passive voice; a title is one sentence. Target: 0 violations.
"""

from __future__ import annotations

import re
from typing import Any

from honed.core.reviews import LintViolation, ReviewResult
from honed.core.types import Bucket, Finding, Severity

LINT_RULES: dict[str, re.Pattern[str]] = {
    "hedging": re.compile(
        r"\b(might|may potentially|could potentially|possibly|perhaps|maybe|it seems|seems to|"
        r"arguably|somewhat|I think|I believe|probably)\b",
        re.I,
    ),
    "filler": re.compile(
        r"\b(in order to|it is (important|worth) (to note|noting)|note that|basically|simply|"
        r"just to|due to the fact that|at the end of the day)\b",
        re.I,
    ),
    "praise": re.compile(
        r"\b(great (job|work|catch)|nice (work|job|catch)|well done|looks good|good job|"
        r"thanks for|thank you|kudos|awesome|excellent)\b",
        re.I,
    ),
    "chatbot": re.compile(
        r"\b(I hope this helps|let me know|feel free|happy to help|certainly|of course|"
        r"as an AI|I'd be happy)\b",
        re.I,
    ),
    "ai_vocabulary": re.compile(
        r"\b(delve|crucial|pivotal|leverag(e|es|ed|ing)|utiliz(e|es|ed|ing)|seamless(ly)?|"
        r"robust(ness)?|comprehensive|showcas(e|es|ing)|underscor(e|es|ing)|tapestry|"
        r"testament|intricate|enhance[sd]?|facilitat(e|es|ing))\b",
        re.I,
    ),
    "em_dash": re.compile("\u2014|\u2013| -- "),  # em dash, en dash, or a double hyphen standing in for one
    "passive_voice": re.compile(
        r"\b(is|are|was|were|be|been|being)\s+(\w+ly\s+)?(\w{3,}ed|thrown|written|"
        r"taken|given|done|made|shown|known|seen|broken|hidden)\b(?!\s+(to|by the time))",
        re.I,
    ),
}
TITLE_RULES = {"title_period": re.compile(r"\.\s*$"), "title_sentences": re.compile(r"[.!?]\s+[A-Z]")}
_ADJECTIVES = frozenset(
    {
        "needed",
        "required",
        "expected",
        "supported",
        "allowed",
        "intended",
        "used",
        "based",
        "related",
        "unused",
        "unrelated",
        "unexpected",
        "undefined",
        "unhandled",
        "unchecked",
    }
)


def lint_text(text: str, *, title: bool = False) -> list[tuple[str, str]]:
    """(rule, excerpt) for each writing-standard violation in `text`; code in backticks is ignored."""
    prose = re.sub(r"```.*?```|`[^`]*`", "`code`", text, flags=re.S)
    found = []
    for rule, pattern in LINT_RULES.items():
        for match in pattern.finditer(prose):
            if rule == "passive_voice" and match.group(3).lower() in _ADJECTIVES:
                continue
            found.append((rule, match.group(0)))
    if title:
        found += [(rule, text.strip()[-40:]) for rule, pattern in TITLE_RULES.items() if pattern.search(prose)]
    return found


def lint(findings: list[Finding] | tuple[Finding, ...]) -> tuple[LintViolation, ...]:
    """Violations in the posted findings' titles and bodies."""
    out = []
    for finding in findings:
        if not finding.posted:
            continue
        for rule, excerpt in lint_text(finding.title, title=True) + lint_text(finding.body):
            out.append(LintViolation(finding.id, rule, excerpt))
    return tuple(out)


SEVERITY_LABEL = {Severity.IMPORTANT: "Important", Severity.PRE_EXISTING: "Pre-existing", Severity.NIT: "Nit"}


def marker(finding: Finding) -> str:
    return f"<!-- honed finding={finding.id} policy={finding.policy_hash} -->"


def comment(finding: Finding) -> str:
    """The inline comment body for a posted finding."""
    return f"**{SEVERITY_LABEL[finding.severity]}:** {finding.title}\n\n{finding.body}\n\n{marker(finding)}"


def _where(finding: Finding) -> str:
    lines = f"{finding.start_line}" if finding.start_line == finding.end_line else \
        f"{finding.start_line}-{finding.end_line}"  # fmt: skip
    return f"`{finding.path}:{lines}`"


def markdown(result: ReviewResult) -> str:
    posted = result.posted
    important = sum(f.severity is not Severity.NIT for f in posted)
    out = [f"## Review of {result.ref}", "",
           f"{len(posted)} comments ({important} Important or Pre-existing, {len(posted) - important} Nits); "
           f"{result.noted} noted, not posted."]  # fmt: skip
    if result.act_on_flagged:
        out.append('\n> More "act on" findings than the policy\'s flag: the lead review may be filtering too little.')
    if result.rereview:
        out.append("\nRe-review: only new Important findings are posted.")
    if not posted:
        out.append("\nNo findings to post.")
    for finding in posted:
        out += ["", f"### {_where(finding)}", comment(finding)]
    dismissed = [f for f in result.findings if f.bucket is Bucket.DISMISSED]
    if dismissed:
        out += ["", f"<details><summary>Dismissed ({len(dismissed)})</summary>", ""]
        out += [f"- {_where(f)} {f.title}: {f.bucket_reason}" for f in dismissed]
        out += ["", "</details>"]
    out += ["", f"<!-- honed policy={result.policy_hash} -->"]
    return "\n".join(out) + "\n"


def to_json(result: ReviewResult) -> dict[str, Any]:
    def finding(f: Finding) -> dict[str, Any]:
        return {
            "id": f.id, "path": f.path, "start_line": f.start_line, "end_line": f.end_line,
            "severity": f.severity.value, "category": f.category, "title": f.title, "body": f.body,
            "bucket": f.bucket.value if f.bucket else None, "reason": f.bucket_reason,
            "evidence_level": f.evidence_level, "checked": f.checked, "confidence": f.confidence,
            "raised_by": list(f.raised_by), "consensus": f.consensus, "lessons": list(f.lessons_cited),
            "trace": f.trace, "comment": comment(f) if f.posted else None,
        }  # fmt: skip

    return {
        "ref": result.ref, "head_commit": result.head_commit, "policy": result.policy_hash, "intent": result.intent,
        "posted": [finding(f) for f in result.posted],
        "noted": [finding(f) for f in result.findings if f.bucket is Bucket.NOTED],
        "dismissed": [finding(f) for f in result.findings if f.bucket is Bucket.DISMISSED],
        "act_on_flagged": result.act_on_flagged, "rereview": result.rereview,
        "lint": [{"finding": v.finding_id, "rule": v.rule, "excerpt": v.excerpt} for v in result.lint],
        "usage": [{"stage": u.stage, "calls": u.calls, "cached": u.cached, "cost_usd": round(u.cost_usd, 5),
                   "seconds": round(u.seconds, 2)} for u in result.usage],
        "cost_usd": round(result.cost_usd, 5), "latency_s": round(result.latency_s, 2),
        "context": {"reads": result.context.reads, "served": result.context.served, "lines": result.context.lines,
                    "truncated": result.context.truncated},
        "failures": list(result.failures), "malformed": result.malformed,
    }  # fmt: skip
