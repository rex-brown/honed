"""The finder panel (ARCHITECTURE.md section 4, step 3): every member reviews the change in parallel, through the job
runner, and proposes structured `Finding`s tagged with the member that raised them.

Composition comes from the policy: specialists (one lens each), one shared checklist on several models, or a mix.
Every member's call has the same system prompt and starts with the same context, so members on one model share a
cached prefix; the member's lenses and lessons come last.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from honed.core.jobs import Job
from honed.core.policy import LANGUAGE_LENSES, LENSES, Member, Policy
from honed.core.reviews import ReviewRequest, StageUsage
from honed.core.types import Finding, Lesson, Severity
from honed.ports.jobs import JobRunner
from honed.ports.llm import LLM, LLMCall, LLMError
from honed.review.context import ReviewContext
from honed.review.text import GUIDANCE, INTENT, UNTRUSTED, AnswerError, ask, block, fill, listing, obj

log = logging.getLogger(__name__)


@dataclass
class PanelResult:
    findings: list[Finding] = field(default_factory=list)
    usage: list[StageUsage] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    malformed: int = 0
    parsed: int = 0  # proposals that parsed, including any beyond a member's max_findings


def schema(categories: Sequence[str]) -> dict[str, Any]:
    finding = obj({
        "path": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "severity": {"type": "string", "enum": [s.value for s in Severity]},
        "category": {"type": "string", "enum": list(categories)},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "trace": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
    })  # fmt: skip
    return obj({"findings": {"type": "array", "items": finding}})


def system_prompt(policy: Policy, categories: Sequence[str]) -> str:
    values = {"categories": listing(list(categories)), "max_findings": str(policy.config.max_findings)}
    return fill(policy.prompts["finder"], values) + "\n\n" + policy.prompts["writing"]


def lessons_block(lessons: Sequence[Lesson]) -> str:
    if not lessons:
        return ""
    lines = [
        "## Team lessons",
        "Rules this team learned from earlier reviews. Cite a lesson's id when a finding applies it.",
    ]
    for lesson in lessons:
        text = lesson.text + (f" Applies when: {lesson.applies_when}" if lesson.applies_when else "")
        text += f" Example: {lesson.example_signal}" if lesson.example_signal else ""
        lines.append(f"- `{lesson.id}`: {text}")
    return "\n".join(lines)


def member_instructions(policy: Policy, member: Member, language: str, lessons: Sequence[Lesson],
                        focus: str = "") -> str:  # fmt: skip
    """The member's part of the prompt: its lenses (all of them for the shared checklist), the language lens, and
    the prompt lessons for finders; or, with a `focus` (the self-review's policy-change lens), that alone."""
    if focus:
        return focus.strip()
    if member.lenses:
        parts = [policy.prompts["specialist"], *(policy.prompts[f"lens_{lens}"] for lens in member.lenses)]
    else:
        parts = [policy.prompts["shared"], *(policy.prompts[f"lens_{lens}"] for lens in LENSES)]
    if policy.config.language_lens and language in LANGUAGE_LENSES:
        parts.append(policy.prompts[LANGUAGE_LENSES[language]])
    parts.append(lessons_block(lessons))
    return "\n\n".join(p.strip() for p in parts if p.strip())


def shared_prefix(context: ReviewContext, intent: str) -> str:
    """The start every member's prompt shares: the change, the repo's guidance and the intent. Backends with prompt
    caching put a breakpoint after it (`LLMCall.cache_prefix`), so members after the first read it from the cache."""
    parts = [block(UNTRUSTED, context.text)]
    if context.guidance:
        parts.append(block(GUIDANCE, context.guidance))
    parts.append(block(INTENT, intent))
    return "\n\n".join(parts)


def user_prompt(context: ReviewContext, intent: str, instructions: str) -> str:
    return "\n\n".join([shared_prefix(context, intent), instructions, "Review the change now."])


def parse_findings(data: Mapping[str, Any], member: str, categories: Sequence[str],
                   lesson_ids: set[str]) -> tuple[list[Finding], int]:  # fmt: skip
    """The member's findings, and how many proposed findings were malformed (dropped)."""
    raw = data.get("findings")
    if not isinstance(raw, list):
        raise AnswerError(f"finder {member}: `findings` is not a list")
    out, malformed = [], 0
    for n, item in enumerate(raw, 1):
        try:
            start, end = int(item["start_line"]), int(item["end_line"])
            severity = Severity(item["severity"])
            category, path, title = str(item["category"]), str(item["path"]).strip(), str(item["title"]).strip()
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        if category not in categories or not path or not title or start < 1:
            malformed += 1
            continue
        out.append(Finding(
            id=f"{member}-{n}", path=path.removeprefix("./"), start_line=start, end_line=max(start, end),
            severity=severity, category=category, title=title, body=str(item.get("body", "")).strip(),
            raised_by=(member,), trace=str(item.get("trace", "")).strip(),
            lessons_cited=tuple(i for i in item.get("lessons") or () if i in lesson_ids),
        ))  # fmt: skip
    return out, malformed


class FinderPanel:
    def __init__(self, llm: LLM, policy: Policy, runner: JobRunner, *, categories: Sequence[str]) -> None:
        self._llm = llm
        self._policy = policy
        self._runner = runner
        self._categories = tuple(categories)
        self._schema = schema(self._categories)
        self._system = system_prompt(policy, self._categories)

    def run(self, request: ReviewRequest, context: ReviewContext, intent: str, lessons: Sequence[Lesson], *,
            sample: int = 0, focus: str = "") -> PanelResult:  # fmt: skip
        result = PanelResult()
        lock = threading.Lock()
        lesson_ids = {lesson.id for lesson in lessons}
        config = self._policy.config

        def job(member: Member) -> Job:
            def run() -> None:
                user = user_prompt(context, intent, member_instructions(self._policy, member, request.language,
                                                                        lessons, focus))  # fmt: skip
                call = LLMCall(model=member.model, system=self._system, user=user, schema=self._schema,
                               max_tokens=config.finders.max_tokens, effort=member.effort, sample=sample,
                               stage=f"finder:{member.id}", pr=request.ref,
                               cache_prefix=len(shared_prefix(context, intent)))  # fmt: skip
                answer = ask(self._llm, call)
                findings, malformed = parse_findings(answer.data, member.id, self._categories, lesson_ids)
                with lock:
                    result.findings += findings[: config.max_findings]
                    result.usage.append(answer.usage)
                    result.malformed += malformed
                    result.parsed += len(findings)

            return Job(f"finder:{member.id}", "finder", run, pr=request.ref)

        report = self._runner([job(m) for m in config.members], concurrency=len(config.members))
        if report.stop_error is not None:
            raise report.stop_error
        result.failures = [f"{job_id}: {error}" for job_id, error in sorted(report.failed.items())]
        if len(result.failures) == len(config.members):
            raise LLMError(f"every panel member failed: {'; '.join(result.failures)[:500]}")
        order = {m.id: n for n, m in enumerate(config.members)}
        result.findings.sort(key=lambda f: (order.get(f.raised_by[0], 0), int(f.id.rsplit("-", 1)[1])))
        return result
