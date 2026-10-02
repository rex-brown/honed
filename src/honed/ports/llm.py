"""LLM: one model call, and the errors every backend raises (ARCHITECTURE.md section 8).

A call is a model, a system prompt, user content, an optional JSON schema (structured output) and a max-tokens hint.
It returns the text or the structured object, plus usage. `stage` and `pr` tag the call for usage accounting only, and
`cache_prefix` is a prompt-caching hint; none of them is part of what the model sees or of the cache key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_schema(schema: Mapping[str, Any] | None) -> str:
    """The schema as canonical JSON ("" when absent), so equal schemas hash equally."""
    return "" if schema is None else json.dumps(schema, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CallKey:
    """What identifies a call's answer: (model, system-prompt hash, input hash, schema hash), plus the effort level
    and the sample index, which also change the answer. `sample` > 0 asks for an independent sample of the same
    prompt (consistency audits)."""

    model: str
    system_hash: str
    input_hash: str
    schema_hash: str
    effort: str = ""
    sample: int = 0

    @property
    def digest(self) -> str:
        parts = (self.model, self.effort, str(self.sample), self.system_hash, self.input_hash, self.schema_hash)
        return _digest("\x1f".join(parts))


@dataclass(frozen=True)
class LLMCall:
    model: str
    system: str
    user: str
    schema: Mapping[str, Any] | None = None  # a JSON schema: the answer is a structured object
    max_tokens: int = 8192  # a hint; backends that cannot cap output ignore it
    effort: str | None = None  # low, medium, high, xhigh, max; None: the backend's default
    sample: int = 0
    stage: str = ""  # accounting only
    pr: str | None = None  # accounting only
    # A hint: the first `cache_prefix` characters of `user` are shared with other calls (the finder panel's context),
    # so a backend with prompt caching puts a cache breakpoint there. Not part of the cache key.
    cache_prefix: int = 0

    @property
    def key(self) -> CallKey:
        return CallKey(
            model=self.model,
            system_hash=_digest(self.system),
            input_hash=_digest(self.user),
            schema_hash=_digest(canonical_schema(self.schema)),
            effort=self.effort or "",
            sample=self.sample,
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0  # includes thinking
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0  # list-price token cost: a *shadow* cost on a flat-rate plan (METRICS.md section 3)
    duration_s: float = 0.0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class LLMResult:
    text: str
    data: Any = None  # the structured object, when the call had a schema
    usage: Usage = field(default_factory=Usage)
    model: str = ""  # the model that answered
    stop_reason: str | None = None
    error_subtype: str | None = None  # the backend's error kind, when the answer is an error
    cached: bool = False  # served from the call cache


class LLM(Protocol):
    def complete(self, call: LLMCall) -> LLMResult: ...


class CallBatch(Protocol):
    """What a job runner needs from a batching backend (`[llm.anthropic] use_batches`): while collecting, a call that
    misses the call cache is queued and raises `CallDeferred`; `flush` sends the queue as one batch, waits until it
    has ended and keeps the answers, so the deferred jobs, run again, find theirs (ARCHITECTURE.md section 8)."""

    def collecting(self, on: bool) -> None:
        """Start (True) or stop (False) queueing calls instead of making them."""
        ...

    def pending(self) -> int:
        """Calls queued for the next batch."""
        ...

    def flush(self) -> str:
        """Send the queued calls as one batch and wait for every answer or failure; a one-line summary. Raises
        `StopRun` when the run must stop (the budget, the call cap, the backend)."""
        ...


class PlanWindow(Protocol):
    """What a job runner needs from a backend's usage guard to wait out a plan-window stop (`--wait-for-reset`)."""

    def resumable_at(self) -> str | None:
        """The ISO time the plan window resets, when the run stopped only for that window (utilization at the
        threshold, an `allowed_warning`, a limit error with a reset time). None when not stopped, or stopped for a
        reason no reset clears (overage, usage credits, an isolation breach, the call cap, no usage signal)."""
        ...

    def wait_until(self, until: str) -> None:
        """Note, in the heartbeat, that the run is waiting until `until` (ISO)."""
        ...

    def resume(self) -> None:
        """Re-arm after the wait; the next live call re-checks the plan."""
        ...

    def rechecked(self) -> bool:
        """True once a live call since `resume` has reported the plan's state (always True before any wait)."""
        ...


# ---- errors ------------------------------------------------------------------------------------------------


class LLMError(RuntimeError):
    """One call failed (after the backend's retries). The job fails; other jobs may continue."""


class ReplayMiss(LLMError):
    """The replay backend has no cached answer for this call."""


class StopRun(LLMError):
    """The whole run must stop now. Everything done so far is kept; running again resumes."""


class UsageLimitReached(StopRun):
    """A subscription usage or rate limit was reached, or is close (see `[llm.claude_code]`)."""

    def __init__(self, message: str, *, resets_at: str | None = None) -> None:
        super().__init__(message + (f" (resets at {resets_at})" if resets_at else ""))
        self.resets_at = resets_at


class BackendUnavailable(StopRun):
    """The backend cannot be reached (network down, CLI missing or logged out). Retries would only burn time."""


class CallCapReached(StopRun):
    """The run's cap on live model calls is spent."""


class IsolationBreach(StopRun):
    """A call ran with context it must not have (tools, MCP servers, plugins, memory, an API key)."""


class BudgetExhausted(StopRun):
    """The run's dollar budget (`[llm.anthropic] run_budget_usd`) is spent."""


class CallDeferred(StopRun):
    """The call was queued for a batch (`CallBatch`): the job stops here, and runs again once the batch has answered.
    A job runner counts the job as neither done nor failed; nothing was spent."""
