"""Prompt text shared by the review stages: the untrusted-data blocks, JSON schemas, and the call wrapper that turns
a model answer into a `StageUsage`.

PR text reaches a model only inside `<untrusted_pr_data>` (or `<repo_guidance>` for the base branch's REVIEW.md),
and PR text can't close or open those blocks: their tags are neutralized inside the data (CLAUDE.md, untrusted
input). The `<pr_intent>` paragraph is derived from PR text, so it gets the same treatment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from honed.core.reviews import StageUsage
from honed.ports.llm import LLM, LLMCall, LLMError, LLMResult

UNTRUSTED, GUIDANCE, INTENT = "untrusted_pr_data", "repo_guidance", "pr_intent"
_TAGS = re.compile(rf"<(/?)({UNTRUSTED}|{GUIDANCE}|{INTENT})\b", re.I)


class AnswerError(LLMError):
    """A model's answer could not be used (not the requested shape)."""


def neutralize(text: str) -> str:
    """Data can't close or reopen a block: `</untrusted_pr_data>` inside it becomes `</untrusted-pr-data>`."""
    return _TAGS.sub(lambda m: f"<{m.group(1)}{m.group(2).replace('_', '-')}", text)


def block(tag: str, text: str) -> str:
    return f"<{tag}>\n{neutralize(text.strip())}\n</{tag}>"


def fill(template: str, values: Mapping[str, str]) -> str:
    """Replace `{{name}}` placeholders; an unknown placeholder is left as is."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def obj(properties: Mapping[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": required or list(properties),
            "additionalProperties": False}  # fmt: skip


def listing(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"`{v}`" for v in values)


@dataclass(frozen=True)
class Answer:
    data: Mapping[str, Any]
    usage: StageUsage


def ask(llm: LLM, call: LLMCall) -> Answer:
    """Make the call; the structured answer must be a JSON object."""
    result: LLMResult = llm.complete(call)
    if not isinstance(result.data, Mapping):
        raise AnswerError(f"{call.stage}: expected a JSON object, got {type(result.data).__name__}")
    usage = StageUsage(call.stage, 1, result.usage.cost_usd, result.usage.duration_s, int(result.cached))
    return Answer(result.data, usage)
