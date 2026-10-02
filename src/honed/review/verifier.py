"""The verifier: lead judgment (ARCHITECTURE.md section 4, step 5; pstack `lead-judgment`, `blast-radius`).

One call checks every proposed finding against the code, as a pragmatic senior engineer rather than an aggregator:
what it confirmed in the code itself (`checked`) and the evidence level that supports (1 asserted, 2 cites the line,
3 traced; 4 and 5 need a sandbox, so the model can't assign them), the pstack filters, a bucket with a one-line
reason, the severity it judges, a confidence, and duplicates. The consensus signal (how many panel members raised a
finding independently) is shown to it.

Evidence levels are the verifier's own (ARCHITECTURE.md section 4, step 5): whatever level a proposed finding carries
(a check's, a finder's) is dropped before verification, the finder's trace is shown as a claim to check, and a level
above 1 counts only with a non-empty `checked`. Without a verdict, or with the verifier off, a finding stays at 1.

Rules applied to its answer, in code:
- Important needs evidence level `important_min_evidence` or higher; below that the finding becomes a Nit, and an
  "act on" becomes "consider".
- The safety invariant: a lesson cited to dismiss or downgrade a finding in a high-risk category has no effect.
- A finding it returns no verdict for is noted, not posted.
With the verifier off (a policy setting), findings are bucketed from their proposed severity, unfiltered.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from typing import Any

from honed.core.policy import Policy
from honed.core.reviews import ReviewRequest, StageUsage
from honed.core.types import Bucket, Finding, Lesson, Severity
from honed.ports.llm import LLM, LLMCall
from honed.review.context import ReviewContext
from honed.review.rank import SEVERITY_RANK, merge
from honed.review.text import GUIDANCE, INTENT, UNTRUSTED, AnswerError, ask, block, fill, listing, obj

STAGE = "verifier"
MAX_MODEL_LEVEL = 3  # levels 4 and 5 need code to run
UNVERIFIED_LEVEL = 1  # asserted: nothing the lead reviewer checked supports it


def schema(labels: Sequence[str], categories: Sequence[str]) -> dict[str, Any]:
    verdict = obj({
        "id": {"type": "string", "enum": list(labels)},
        "bucket": {"type": "string", "enum": [b.value for b in Bucket]},
        "reason": {"type": "string"},
        "checked": {"type": "string"},
        "evidence_level": {"type": "integer"},
        "severity": {"type": "string", "enum": [s.value for s in Severity]},
        "confidence": {"type": "number"},
        "duplicate_of": {"type": "string"},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
    })  # fmt: skip
    return obj({"verdicts": {"type": "array", "items": verdict}})


def _source(finding: Finding, members: int) -> str:
    panel = [r for r in finding.raised_by if not r.startswith("check:")]
    checks = [r.removeprefix("check:") for r in finding.raised_by if r.startswith("check:")]
    parts = []
    if panel:
        parts.append(f"{len(panel)} of {members} reviewers, independently" if len(panel) > 1
                     else f"1 of {members} reviewers")  # fmt: skip
    if checks:
        parts.append("an automatic check (" + ", ".join(f"`{c}`" for c in checks) + ")")
    return " and ".join(parts)


def proposed_block(findings: Sequence[Finding], members: int) -> str:
    out = []
    for n, f in enumerate(findings, 1):
        lines = f"line {f.start_line}" if f.start_line == f.end_line else f"lines {f.start_line}-{f.end_line}"
        entry = [f"### P{n}", f"Raised by: {_source(f, members)}", f"Location: {f.path}, {lines}",
                 f"Proposed severity: {f.severity.value}; category: {f.category}", f"Title: {f.title}"]  # fmt: skip
        if f.body:
            entry.append(f"Body: {f.body}")
        if f.trace:
            entry.append(f"Reviewer's trace (a claim to check): {f.trace}")
        if f.lessons_cited:
            entry.append("Lessons cited: " + ", ".join(f.lessons_cited))
        out.append("\n".join(entry))
    return "\n\n".join(out)


def lessons_block(lessons: Sequence[Lesson]) -> str:
    if not lessons:
        return ""
    lines = ["## Team lessons that can justify a dismissal"]
    for lesson in lessons:
        lines.append(f"- `{lesson.id}` (for {listing(list(lesson.categories))} findings): {lesson.text} "
                     f"Skip when: {lesson.skip_when} Do not skip when: {lesson.do_not_skip_when}")  # fmt: skip
    return "\n".join(lines)


class Verifier:
    def __init__(self, llm: LLM, policy: Policy, *, categories: Sequence[str], high_risk: Collection[str]) -> None:
        self._llm = llm
        self._policy = policy
        self._categories = tuple(categories)
        self._high_risk = frozenset(high_risk)
        rules = policy.config.rank
        self._system = fill(policy.prompts["verifier"], {
            "act_on_flag": str(rules.act_on_flag), "important_min_evidence": str(rules.important_min_evidence),
            "categories": listing(list(categories)),
        }) + "\n\n" + policy.prompts["writing"]  # fmt: skip

    @property
    def enabled(self) -> bool:
        return self._policy.config.verifier.enabled

    def run(self, request: ReviewRequest, context: ReviewContext, intent: str, findings: Sequence[Finding],
            lessons: Sequence[Lesson], *, sample: int = 0,
            focus: str = "") -> tuple[list[Finding], StageUsage | None]:  # fmt: skip
        if not findings:
            return [], None
        findings = [drop_claims(f) for f in findings]
        if not self.enabled:
            return [unverified(f) for f in findings], None
        stage = self._policy.config.verifier
        labels = [f"P{n}" for n in range(1, len(findings) + 1)]
        members = len(self._policy.config.members)
        parts = [block(UNTRUSTED, context.text)]
        if context.guidance:
            parts.append(block(GUIDANCE, context.guidance))
        parts += [block(INTENT, intent)]
        if focus:  # the self-review: what this change is checked for, and what evidence means for policy text
            parts.append(focus.strip())
        parts += ["## Proposed findings", block(UNTRUSTED, proposed_block(findings, members))]
        if lessons:
            parts.append(lessons_block(lessons))
        parts.append("Give a verdict for every proposed finding.")
        call = LLMCall(model=stage.model, system=self._system, user="\n\n".join(parts),
                       schema=schema(labels, self._categories), max_tokens=stage.max_tokens, effort=stage.effort,
                       sample=sample, stage=STAGE, pr=request.ref)  # fmt: skip
        answer = ask(self._llm, call)
        verdicts = answer.data.get("verdicts")
        if not isinstance(verdicts, list):
            raise AnswerError("verifier: `verdicts` is not a list")
        return self.apply(findings, labels, verdicts, {lesson.id: lesson for lesson in lessons}), answer.usage

    def apply(self, findings: Sequence[Finding], labels: Sequence[str], verdicts: Sequence[Mapping[str, Any]],
              lessons: Mapping[str, Lesson]) -> list[Finding]:  # fmt: skip
        by_label = dict(zip(labels, findings, strict=True))
        judged: dict[str, Finding] = {}
        duplicates: dict[str, str] = {}
        for verdict in verdicts:
            label = verdict.get("id")
            if label not in by_label or label in judged:
                continue
            try:
                judged[label] = self._one(by_label[label], verdict, lessons)
            except (KeyError, TypeError, ValueError):
                continue
            target = str(verdict.get("duplicate_of") or "")
            if target in by_label and target != label:
                duplicates[label] = target
        out: dict[str, Finding] = {}
        for label in labels:
            if label in judged:
                out[label] = judged[label]
            else:
                out[label] = replace(by_label[label], bucket=Bucket.NOTED,
                                     bucket_reason="no verdict from the lead reviewer")  # fmt: skip
        for label, target in duplicates.items():
            root = target
            seen = {label}
            while root in duplicates and root not in seen:
                seen.add(root)
                root = duplicates[root]
            if root != label and root in out and label in out:
                out[root] = merge(out[root], out.pop(label))
        return list(out.values())

    def _one(self, finding: Finding, verdict: Mapping[str, Any], lessons: Mapping[str, Lesson]) -> Finding:
        bucket = Bucket(verdict["bucket"])
        severity = Severity(verdict["severity"])
        checked = str(verdict.get("checked") or "").strip()
        level = max(UNVERIFIED_LEVEL, min(MAX_MODEL_LEVEL, int(verdict.get("evidence_level") or 1)))
        if not checked:
            level = UNVERIFIED_LEVEL  # a level is only as good as what the lead reviewer says it checked
        confidence = max(0.0, min(1.0, float(verdict.get("confidence") or 0.0)))
        reason = str(verdict.get("reason", "")).strip()
        cited = tuple(i for i in verdict.get("lessons") or () if i in lessons)
        suppressing = [i for i in cited if lessons[i].suppresses]
        if finding.category in self._high_risk and suppressing:
            # The safety invariant: a lesson's effect on a high-risk finding is undone.
            if bucket in (Bucket.DISMISSED, Bucket.NOTED):
                bucket = Bucket.CONSIDER
            if SEVERITY_RANK[severity] > SEVERITY_RANK[finding.severity]:
                severity = finding.severity
            reason += f" (restored: lessons {suppressing} may not dismiss or downgrade a high-risk finding)"
        minimum = self._policy.config.rank.important_min_evidence
        if severity is Severity.IMPORTANT and level < minimum:
            severity = Severity.NIT
            if bucket is Bucket.ACT_ON:
                bucket = Bucket.CONSIDER
            reason += f" (Nit: evidence level {level} is below {minimum})"
        title = str(verdict.get("title") or "").strip() or finding.title
        body = str(verdict.get("body") or "").strip() or finding.body
        return replace(finding, bucket=bucket, bucket_reason=reason, evidence_level=level, severity=severity,
                       confidence=confidence, title=title, body=body, checked=checked,
                       lessons_cited=tuple(dict.fromkeys((*finding.lessons_cited, *cited))))  # fmt: skip


def drop_claims(finding: Finding) -> Finding:
    """A proposed finding before verification: no evidence level of its own (the verifier assigns it)."""
    return replace(finding, evidence_level=UNVERIFIED_LEVEL, checked="")


def unverified(finding: Finding) -> Finding:
    """No lead review: every proposed finding is posted at its proposed severity."""
    bucket = Bucket.CONSIDER if finding.severity is Severity.NIT else Bucket.ACT_ON
    return replace(finding, bucket=bucket, bucket_reason="posted without lead review", confidence=1.0)
