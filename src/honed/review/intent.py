"""Intent (pstack `interrogate` step 2): one paragraph on what the PR is meant to do, written before any finder runs,
so the finders judge whether the code achieves it rather than argue with it (ARCHITECTURE.md section 4, step 2)."""

from __future__ import annotations

from honed.core.policy import Policy
from honed.core.reviews import ReviewRequest, StageUsage
from honed.ports.llm import LLM, LLMCall
from honed.review.context import ReviewContext
from honed.review.text import UNTRUSTED, AnswerError, ask, block, obj

STAGE = "intent"
SCHEMA = obj({"intent": {"type": "string"}})


def write_intent(llm: LLM, policy: Policy, request: ReviewRequest, context: ReviewContext, *,
                 sample: int = 0) -> tuple[str, StageUsage]:  # fmt: skip
    stage = policy.config.intent
    user = block(UNTRUSTED, context.text) + "\n\nWrite the intent paragraph for this pull request."
    answer = ask(llm, LLMCall(model=stage.model, system=policy.prompts["intent"], user=user, schema=SCHEMA,
                              max_tokens=stage.max_tokens, effort=stage.effort, sample=sample, stage=STAGE,
                              pr=request.ref))  # fmt: skip
    intent = str(answer.data.get("intent", "")).strip()
    if not intent:
        raise AnswerError("intent: empty paragraph")
    return intent, answer.usage
