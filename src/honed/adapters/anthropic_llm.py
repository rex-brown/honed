"""LLM backend on the Anthropic API with an API key (ARCHITECTURE.md sections 8 and 9), through the official SDK.

API-key only: the key comes from `ANTHROPIC_API_KEY` and is passed to the client explicitly, so the SDK never falls
back to an OAuth token, a profile or any other credential; without a key (or with an OAuth token in its place) the
backend refuses to start. A Claude subscription is used only through the `claude_code` backend.

One call:
- the system prompt is one block with a `cache_control` breakpoint at its end, and when the call names a shared
  prefix of its user content (`LLMCall.cache_prefix`: the finder panel's context, intent and guidance), a second
  breakpoint ends that prefix, so the panel members after the first read it from the prompt cache;
- a JSON schema becomes `output_config.format` (never a prefill), the effort `output_config.effort`; thinking is left
  at the model's default (adaptive);
- sent as a stream (large `max_tokens` would time out otherwise) with server-side refusal fallbacks (beta
  `server-side-fallback-2026-07-01`, `fallbacks: "default"`), unless `[llm.anthropic] fallbacks = false`;
- usage from the response (per iteration when a fallback model served it), priced from `[llm.anthropic.prices]`:
  the cost metric's shadow cost is the real cost here.
The SDK retries rate limits, overloads and server errors; what is left after its retries maps to the port's errors:
a refused key, a missing model or a billing problem stops the run (`BackendUnavailable`), as does the network; a rate
limit stops it too (`UsageLimitReached`); a bad request, a server error, a refusal that every fallback also refused,
or an answer that isn't the requested JSON fails just that call (`LLMError`).

Batch mode (`[llm.anthropic] use_batches`, the `CallBatch` port): while the job runner collects, a call is queued and
its job deferred; `flush` sends the queue as one Message Batch at half price (`batch_discount`), keyed by the call's
cache-key digest as `custom_id`, polls until it has ended (heartbeat `batch`), and keeps every answer and per-item
failure for the jobs' next pass. The Batches API takes no `fallbacks`, so a refused item is asked again on its own,
with fallbacks. Every live call and batch is admitted by the `BudgetGuard` (the run's dollar budget and call cap).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import anthropic

from honed.adapters.budget_guard import BudgetGuard
from honed.ports.llm import (
    BackendUnavailable,
    CallDeferred,
    LLMCall,
    LLMError,
    LLMResult,
    StopRun,
    Usage,
    UsageLimitReached,
)

log = logging.getLogger(__name__)

API_KEY_ENV = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_EPHEMERAL = {"type": "ephemeral"}
_OAUTH_PREFIX = "sk-ant-oat"  # Claude subscription OAuth tokens (`claude setup-token`): never sent to the API
# JSON-schema keywords structured outputs don't take (our parsers still check the answers).
_UNSUPPORTED = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
                          "maxLength", "maxItems"})  # fmt: skip
_CHARS_PER_TOKEN = 3  # the worst-case estimate's input tokens: code and JSON run near 3 characters per token


@dataclass(frozen=True)
class ModelPrice:
    """List prices in USD per million tokens. `cache_write` is the 5-minute-TTL write price."""

    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(self, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output + cache_read * self.cache_read
                + cache_write * self.cache_write) / 1e6  # fmt: skip


@dataclass(frozen=True)
class AnthropicOptions:
    prices: Mapping[str, ModelPrice]
    fallbacks: bool = True  # server-side refusal fallbacks on live calls
    batch_discount: float = 0.5  # message batches cost this fraction of list price
    timeout_s: float = 900.0
    max_retries: int = 2  # the SDK's retries of rate limits, overloads, server and connection errors
    batch_poll_s: float = 30.0
    batch_max_wait_s: float = 86400.0  # a batch still running after this is cancelled; its unfinished items fail


def api_schema(schema: Any) -> Any:
    """`schema` without the keywords structured outputs reject (numeric and length bounds, `minItems` above 1)."""
    if not isinstance(schema, Mapping):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED or (key == "minItems" and isinstance(value, int) and value > 1):
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, Mapping):
            out[key] = {name: api_schema(sub) for name, sub in value.items()}
        elif key in ("items", "not") and isinstance(value, Mapping):
            out[key] = api_schema(value)
        elif key in ("anyOf", "allOf", "oneOf") and isinstance(value, list):
            out[key] = [api_schema(sub) for sub in value]
        else:
            out[key] = value
    return out


def message_params(call: LLMCall) -> dict[str, Any]:
    """The Messages API request for one call (no fallbacks: the Batches API takes none)."""
    user = call.user
    if 0 < call.cache_prefix < len(user) and user[call.cache_prefix :].strip():
        content = [{"type": "text", "text": user[: call.cache_prefix], "cache_control": _EPHEMERAL},
                   {"type": "text", "text": user[call.cache_prefix :]}]  # fmt: skip
    else:
        content = [{"type": "text", "text": user}]
    params: dict[str, Any] = {"model": call.model, "max_tokens": call.max_tokens,
                              "messages": [{"role": "user", "content": content}]}  # fmt: skip
    if call.system:
        params["system"] = [{"type": "text", "text": call.system, "cache_control": _EPHEMERAL}]
    output: dict[str, Any] = {}
    if call.effort:
        output["effort"] = call.effort
    if call.schema is not None:
        output["format"] = {"type": "json_schema", "schema": api_schema(call.schema)}
    if output:
        params["output_config"] = output
    return params


class Prices:
    """Price lookup; a model missing from the table is priced at the table's highest price of each kind."""

    def __init__(self, table: Mapping[str, ModelPrice]) -> None:
        if not table:
            raise ValueError("[llm.anthropic.prices] is empty")
        self._table = dict(table)
        self._ceiling = ModelPrice(*(max(getattr(p, f) for p in table.values())
                                     for f in ("input", "output", "cache_write", "cache_read")))  # fmt: skip
        self._warned: set[str] = set()

    def __call__(self, model: str) -> ModelPrice:
        price = self._table.get(model)
        if price is None:
            if model not in self._warned:
                self._warned.add(model)
                log.warning("no price for %s in [llm.anthropic.prices]: charging the table's highest prices", model)
            return self._ceiling
        return price

    def worst_case(self, call: LLMCall, discount: float = 1.0) -> float:
        """The most `call` can cost: its input, all written to the cache, plus `max_tokens` of output."""
        price = self(call.model)
        chars = len(call.system) + len(call.user) + (len(json.dumps(call.schema)) if call.schema else 0)
        input_tokens = chars // _CHARS_PER_TOKEN + 64
        return discount * price.cost(0, call.max_tokens, 0, input_tokens) + discount * input_tokens * max(
            0.0, price.input - price.cache_write) / 1e6  # fmt: skip


def usage_of(message: Any, model: str, prices: Prices, *, discount: float = 1.0, duration_s: float = 0.0) -> Usage:
    """Tokens and cost of a response: per iteration when the API reports them (a fallback model's attempt is
    priced at its own rates), else from the top-level usage."""
    usage = message.usage
    parts: list[tuple[str, int, int, int, int]] = []
    for it in getattr(usage, "iterations", None) or ():
        if getattr(it, "type", "") in ("message", "fallback_message"):
            parts.append((getattr(it, "model", None) or getattr(message, "model", None) or model,
                          it.input_tokens or 0, it.output_tokens or 0, it.cache_read_input_tokens or 0,
                          it.cache_creation_input_tokens or 0))  # fmt: skip
    if not parts:
        parts.append((getattr(message, "model", None) or model, usage.input_tokens or 0, usage.output_tokens or 0,
                      getattr(usage, "cache_read_input_tokens", 0) or 0,
                      getattr(usage, "cache_creation_input_tokens", 0) or 0))  # fmt: skip
    cost = discount * sum(prices(m).cost(i, o, r, w) for m, i, o, r, w in parts)
    return Usage(
        input_tokens=sum(p[1] for p in parts), output_tokens=sum(p[2] for p in parts),
        cache_read_tokens=sum(p[3] for p in parts), cache_write_tokens=sum(p[4] for p in parts),
        cost_usd=round(cost, 6), duration_s=round(duration_s, 3),
    )  # fmt: skip


def answer(message: Any, call: LLMCall, usage: Usage) -> LLMResult:
    """The call's result from a response, or `LLMError` (a refusal, or not the requested JSON)."""
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        raise LLMError(f"{call.stage or 'call'}: refused ({category or 'no category'}) by {message.model}")
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    data = None
    if call.schema is not None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise LLMError(
                f"{call.stage or 'call'}: the answer is not JSON (stop reason {message.stop_reason})"
            ) from None
    return LLMResult(text=text, data=data, usage=usage, model=message.model or call.model,
                     stop_reason=message.stop_reason)  # fmt: skip


def _api_error(error: Exception, what: str) -> StopRun | LLMError:
    """The port's error for an SDK error left after the SDK's own retries: a `StopRun` stops the run."""
    text = f"{what}: {type(error).__name__}: {str(error)[:300]}"
    if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return BackendUnavailable(f"the API key was refused ({text})")
    if isinstance(error, anthropic.NotFoundError):
        return BackendUnavailable(f"model or endpoint unavailable ({text})")
    if isinstance(error, anthropic.RateLimitError):
        return UsageLimitReached(f"rate limited after the SDK's retries ({text})")
    if isinstance(error, anthropic.BadRequestError):
        if "credit balance" in str(error).lower():
            return BackendUnavailable(f"billing: {text}")
        return LLMError(text)
    if isinstance(error, anthropic.APIStatusError):
        return BackendUnavailable(f"billing: {text}") if error.status_code == 402 else LLMError(text)
    if isinstance(error, anthropic.APITimeoutError):
        return LLMError(text)
    if isinstance(error, anthropic.APIConnectionError):
        return BackendUnavailable(f"network unavailable ({text})")
    return LLMError(text)


class AnthropicLLM:
    """The `LLM` port on the Messages API; also the `CallBatch` port for the job runner (batch mode)."""

    def __init__(self, client: Any, options: AnthropicOptions, guard: BudgetGuard, *,
                 sleep: Callable[[float], None] = time.sleep) -> None:  # fmt: skip
        self._client = client
        self._o = options
        self._guard = guard
        self._prices = Prices(options.prices)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._collecting = False
        self._queue: dict[str, LLMCall] = {}  # cache-key digest -> call, for the next batch
        self._answered: dict[str, tuple[LLMResult, bool]] = {}  # digest -> (answer, not yet handed out)
        self._failed: dict[str, str] = {}  # digest -> why the batch item failed
        self.batches: list[str] = []

    @classmethod
    def from_environment(cls, options: AnthropicOptions, guard: BudgetGuard, *,
                         environ: Mapping[str, str] | None = None) -> AnthropicLLM:  # fmt: skip
        """The backend with the key from `ANTHROPIC_API_KEY`; refuses to start without one."""
        key = (os.environ if environ is None else environ).get(API_KEY_ENV, "").strip()
        if not key:
            raise BackendUnavailable(
                f"the anthropic backend needs an API key in ${API_KEY_ENV} and refuses to start without one (a Claude "
                "subscription works only through the claude_code backend)"
            )
        if key.startswith(_OAUTH_PREFIX):
            raise BackendUnavailable(f"${API_KEY_ENV} holds a Claude subscription OAuth token, not an API key; the "
                                     "anthropic backend never sends one to the API")  # fmt: skip
        client = anthropic.Anthropic(api_key=key, timeout=options.timeout_s, max_retries=options.max_retries)
        return cls(client, options, guard)

    @property
    def guard(self) -> BudgetGuard:
        return self._guard

    # ---- LLM -----------------------------------------------------------------------------------------------

    def complete(self, call: LLMCall) -> LLMResult:
        key = call.key.digest
        with self._lock:
            if key in self._answered:
                result, fresh = self._answered[key]
                self._answered[key] = (result, False)
                # An identical call answered by the same batch item: its tokens were paid for once.
                return result if fresh else replace(result, usage=Usage(duration_s=result.usage.duration_s))
            if key in self._failed:
                raise LLMError(self._failed[key])
            if self._collecting:
                self._queue.setdefault(key, call)
                raise CallDeferred(f"{call.stage or 'call'} queued for the next message batch")
        return self._live(call)

    def _live(self, call: LLMCall) -> LLMResult:
        worst = self._prices.worst_case(call)
        self._guard.reserve(worst)
        cost = 0.0
        started = time.monotonic()
        try:
            try:
                message = self._send(message_params(call))
            except anthropic.APIError as error:
                mapped = _api_error(error, call.stage or "call")
                if isinstance(mapped, StopRun):
                    self._guard.stop(mapped)
                raise mapped from None
            usage = usage_of(message, call.model, self._prices, duration_s=time.monotonic() - started)
            cost = usage.cost_usd
            result = answer(message, call, usage)
        finally:
            self._guard.settle(worst, cost)
        log.info("anthropic %s%s: %.1fs, in %d (+%d cached, %d written), out %d, $%.4f", call.stage or "call",
                 f" {call.pr}" if call.pr else "", usage.duration_s, usage.input_tokens, usage.cache_read_tokens,
                 usage.cache_write_tokens, usage.output_tokens, usage.cost_usd)  # fmt: skip
        return result

    def _send(self, params: Mapping[str, Any]) -> Any:
        if self._o.fallbacks:
            with self._client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default") as stream:
                return stream.get_final_message()
        with self._client.messages.stream(**params) as stream:
            return stream.get_final_message()

    # ---- CallBatch -------------------------------------------------------------------------------------------

    def collecting(self, on: bool) -> None:
        with self._lock:
            self._collecting = on

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def flush(self) -> str:
        with self._lock:
            queued = list(self._queue.items())
            self._queue.clear()
        if not queued:
            return "nothing queued"
        worst = [self._prices.worst_case(call, self._o.batch_discount) for _, call in queued]
        room = self._guard.admit(worst)  # raises when not even one request fits
        sent = dict(queued[:room])
        reserved, spent = sum(worst[:room]), 0.0
        refused: list[str] = []
        counts = {"answered": 0, "failed": 0}
        batch_id = "?"
        try:
            try:
                batch = self._client.messages.batches.create(
                    requests=[{"custom_id": key, "params": message_params(call)} for key, call in sent.items()]
                )
                batch_id = batch.id
                self.batches.append(batch_id)
                log.info("message batch %s: %d requests sent", batch_id, len(sent))
                try:
                    batch = self._wait(batch)
                except KeyboardInterrupt:  # don't leave a batch spending unseen; finished items are still billed
                    log.warning("interrupted: cancelling message batch %s", batch_id)
                    with contextlib.suppress(anthropic.APIError):
                        self._client.messages.batches.cancel(batch_id)
                    raise
                seen: set[str] = set()
                for item in self._client.messages.batches.results(batch_id):
                    key = item.custom_id
                    if key not in sent or key in seen:
                        continue
                    seen.add(key)
                    spent += self._take(key, sent[key], item.result, refused, counts)
            except anthropic.APIError as error:
                mapped = _api_error(error, f"message batch {batch_id}")
                if isinstance(mapped, StopRun):
                    self._guard.stop(mapped)
                raise mapped from None
            for key in sent.keys() - seen - set(refused):
                self._fail(key, f"message batch {batch_id}: no result for this request", counts)
        finally:
            self._guard.settle(reserved, spent)
            self._guard.note_batch(None)
        rescued = self._rescue(refused, sent, counts)
        left = len(queued) - room
        return (f"batch {batch_id}: {len(sent)} requests, {counts['answered']} answered, {counts['failed']} failed, "
                f"{len(refused)} refused ({rescued} answered by a fallback model), ${spent:.2f} at batch prices"
                + (f"; {left} more wait for the next batch (budget or call cap)" if left else ""))  # fmt: skip

    def _wait(self, batch: Any) -> Any:
        """Poll until the batch has ended; cancel it once it has run past `batch_max_wait_s`."""
        started = time.monotonic()
        cancelled = False
        while batch.processing_status != "ended":
            counts = getattr(batch, "request_counts", None)
            self._guard.note_batch({"id": batch.id, "status": batch.processing_status,
                                    **({k: getattr(counts, k, None) for k in ("processing", "succeeded", "errored",
                                                                              "canceled", "expired")}
                                       if counts is not None else {})})  # fmt: skip
            if not cancelled and time.monotonic() - started > self._o.batch_max_wait_s:
                log.warning(
                    "message batch %s still running after %.0fs: cancelling it", batch.id, self._o.batch_max_wait_s
                )
                self._client.messages.batches.cancel(batch.id)
                cancelled = True
            self._sleep(self._o.batch_poll_s)
            batch = self._client.messages.batches.retrieve(batch.id)
        return batch

    def _take(self, key: str, call: LLMCall, result: Any, refused: list[str], counts: dict[str, int]) -> float:
        """Keep one batch item's answer or failure; returns its cost."""
        kind = getattr(result, "type", "")
        if kind != "succeeded":
            error = getattr(getattr(result, "error", None), "error", None)
            detail = f"{getattr(error, 'type', '')}: {getattr(error, 'message', '')}" if error is not None else ""
            self._fail(key, f"message batch item {kind}{f' ({detail})' if detail else ''}", counts)
            return 0.0
        message = result.message
        usage = usage_of(message, call.model, self._prices, discount=self._o.batch_discount)
        if message.stop_reason == "refusal" and self._o.fallbacks:
            refused.append(key)
            return usage.cost_usd
        try:
            done = answer(message, call, usage)
        except LLMError as error:
            self._fail(key, str(error), counts)
            return usage.cost_usd
        with self._lock:
            self._answered[key] = (done, True)
        counts["answered"] += 1
        return usage.cost_usd

    def _fail(self, key: str, why: str, counts: dict[str, int]) -> None:
        with self._lock:
            self._failed[key] = why
        counts["failed"] += 1

    def _rescue(self, refused: Iterable[str], sent: Mapping[str, LLMCall], counts: dict[str, int]) -> int:
        """Ask each refused batch item again on its own, with server-side fallbacks. A stop (the budget) leaves the
        rest failed for this run; the answers already kept stay."""
        rescued = 0
        for key in refused:
            try:
                result = self._live(sent[key])
            except StopRun as error:
                self._fail(key, f"refused in the batch; not asked again: {error}", counts)
                continue
            except LLMError as error:
                self._fail(key, f"refused in the batch, and again with fallbacks: {error}", counts)
                continue
            with self._lock:
                self._answered[key] = (result, True)
            counts["answered"] += 1
            rescued += 1
        return rescued
