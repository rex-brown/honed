"""An LLM decorator that answers from the call cache when it can, caches every successful live answer, and records
each call (live or cached) in the usage ledger with its stage and PR (ARCHITECTURE.md section 8)."""

from __future__ import annotations

import datetime as dt
import logging

from honed.ports.call_store import CallStore, LedgerEntry
from honed.ports.llm import LLM, LLMCall, LLMError, LLMResult, ReplayMiss, StopRun

log = logging.getLogger(__name__)


class CachedLLM:
    def __init__(self, inner: LLM, store: CallStore, *, run_id: str) -> None:
        self._inner = inner
        self._store = store
        self._run_id = run_id

    def complete(self, call: LLMCall) -> LLMResult:
        key = call.key
        hit = self._store.get(key)
        if hit is not None:
            self._record(call, key.digest, hit.model, cached=True, ok=True)
            return hit
        try:
            result = self._inner.complete(call)
        except (StopRun, ReplayMiss):
            raise  # the run stops (the guard's heartbeat has the reason), or nothing was asked: no model call
        except LLMError as error:
            self._record(call, key.digest, call.model, cached=False, ok=False, error=str(error)[:500])
            raise
        self._store.put(key, call, result)
        self._record(call, key.digest, result.model or call.model, cached=False, ok=True, result=result)
        return result

    def _record(
        self, call: LLMCall, key: str, model: str, *, cached: bool, ok: bool, result: LLMResult | None = None,
        error: str | None = None,
    ) -> None:  # fmt: skip
        u = result.usage if result is not None else None
        self._store.record(
            LedgerEntry(
                at=dt.datetime.now(dt.UTC).isoformat(), run_id=self._run_id, stage=call.stage or "unstaged",
                pr=call.pr, model=model, key=key, cached=cached, ok=ok,
                input_tokens=u.input_tokens if u else 0, output_tokens=u.output_tokens if u else 0,
                cache_read_tokens=u.cache_read_tokens if u else 0, cache_write_tokens=u.cache_write_tokens if u else 0,
                cost_usd=u.cost_usd if u else 0.0, duration_s=u.duration_s if u else 0.0, error=error,
            )
        )  # fmt: skip
