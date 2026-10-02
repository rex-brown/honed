"""The `replay` LLM backend: answers only from the call cache and raises `ReplayMiss` otherwise. Used by tests and
by offline, deterministic re-scoring (ARCHITECTURE.md section 8)."""

from __future__ import annotations

from honed.ports.call_store import CallStore
from honed.ports.llm import LLMCall, LLMResult, ReplayMiss


class ReplayLLM:
    def __init__(self, store: CallStore) -> None:
        self._store = store

    def complete(self, call: LLMCall) -> LLMResult:
        hit = self._store.get(call.key)
        if hit is None:
            where = " ".join(part for part in (call.stage, call.pr) if part)
            raise ReplayMiss(f"no cached answer for {where or 'this call'} ({call.model}, key {call.key.digest[:12]})")
        return hit
