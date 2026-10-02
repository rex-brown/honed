"""The review pipeline (ARCHITECTURE.md section 4), as a `ports.reviewer.Reviewer`:

    context + intent -> finder panel || checks -> verifier (lead judgment) -> dedup + rank -> render

Every prompt and setting comes from the loaded policy; the categories and the high-risk list come from
`honed.toml`, outside the policy.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from honed.core.policy import Policy
from honed.core.reviews import ReviewRequest, ReviewResult, StageUsage
from honed.core.types import LessonKind
from honed.ports.code_reader import CodeReader
from honed.ports.jobs import JobRunner
from honed.ports.llm import LLM
from honed.ports.store import Store
from honed.review import checks, rank, render
from honed.review.context import ContextBuilder
from honed.review.finders import FinderPanel
from honed.review.intent import write_intent
from honed.review.text import fill, listing
from honed.review.verifier import Verifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewOptions:
    categories: tuple[str, ...]  # `[label] categories`: the vocabulary findings and gold issues share
    high_risk: frozenset[str]  # `[safety] high_risk_categories`
    # A prompt of the policy that replaces the code lenses (the self-review's `policy_change`): every member applies
    # it alone, the verifier reads it too, and no lessons or checks run (they are about code).
    focus: str | None = None


def _latency(usage: Sequence[StageUsage]) -> float:
    """Intent, then the slowest panel member, then the verifier: the review's critical path."""
    panel = [u.seconds for u in usage if u.stage.startswith("finder:")]
    rest = sum(u.seconds for u in usage if not u.stage.startswith("finder:"))
    return rest + max(panel, default=0.0)


class ReviewPipeline:
    def __init__(self, llm: LLM, policy: Policy, runner: JobRunner, options: ReviewOptions,
                 store: Store | None = None) -> None:  # fmt: skip
        self._llm = llm
        self._policy = policy
        self._options = options
        self._context = ContextBuilder(policy.config.context, store)
        self._panel = FinderPanel(llm, policy, runner, categories=options.categories)
        self._verifier = Verifier(llm, policy, categories=options.categories, high_risk=options.high_risk)
        self._focus = ""
        if options.focus:
            self._focus = fill(policy.prompts[options.focus], {"high_risk": listing(sorted(options.high_risk))})

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def policy_hash(self) -> str:
        return self._policy.content_hash

    def review(self, request: ReviewRequest, reader: CodeReader, *, sample: int = 0) -> ReviewResult:
        policy, options = self._policy, self._options
        context = self._context.build(request, reader)
        lessons = () if self._focus else policy.lessons_for(language=request.language, repo=request.repo,
                                                             paths=context.paths)  # fmt: skip
        prompt_lessons = [lesson for lesson in lessons if lesson.kind is LessonKind.PROMPT]
        check_lessons = [lesson for lesson in lessons if lesson.kind is LessonKind.CHECK]
        intent, intent_usage = write_intent(self._llm, policy, request, context, sample=sample)
        finder_lessons = [lesson for lesson in prompt_lessons if not lesson.suppresses]
        panel = self._panel.run(request, context, intent, finder_lessons, sample=sample, focus=self._focus)
        flagged = checks.run_checks(check_lessons, request, context)
        proposed = rank.merge_duplicates([*panel.findings, *flagged], policy.config.rank.dedup_line_slack)
        verified, verifier_usage = self._verifier.run(
            request, context, intent, proposed, [lesson for lesson in prompt_lessons if lesson.suppresses],
            sample=sample, focus=self._focus,
        )  # fmt: skip
        verified = checks.suppress(check_lessons, verified, options.high_risk)
        ranked = rank.rank(verified, request, policy.config.rank, options.high_risk, policy.content_hash)
        usage = (intent_usage, *panel.usage, *((verifier_usage,) if verifier_usage else ()))
        result = ReviewResult(
            policy_hash=policy.content_hash, ref=request.ref, head_commit=request.head_commit, intent=intent,
            findings=ranked.findings, noted=ranked.noted, act_on_flagged=ranked.act_on_flagged,
            lint=render.lint(ranked.findings), usage=usage, latency_s=_latency(usage), context=context.stats,
            failures=tuple(panel.failures), malformed=panel.malformed, rereview=request.rereview,
            proposed=panel.parsed + len(flagged),
        )  # fmt: skip
        log.info("%s@%s: %d proposed, %d posted, %d noted, %d dismissed; $%.3f", request.ref,
                 request.head_commit[:8], len(proposed), len(result.posted), result.noted,
                 sum(f.bucket is not None and f.bucket.value == "dismissed" for f in result.findings),
                 result.cost_usd)  # fmt: skip
        return result
