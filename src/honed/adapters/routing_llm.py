"""LLM decorators that decide which model answers a call (ARCHITECTURE.md section 8, offline mode).

- `RoutedLLM` sends every call to one model: it rewrites the call's model before the call cache sees it, so an
  answer is cached under the model that gave it and a local answer can never pass for a Claude one. The effort is
  dropped (the local backend has none), which also keeps it out of the cache key.
- `ReplayFirstLLM` answers from a replay backend (the call cache under the call's own model, Fable's cached judge
  answers) and only on a miss asks the fallback: the offline judge.
"""

from __future__ import annotations

from dataclasses import replace

from honed.ports.llm import LLM, LLMCall, LLMResult, ReplayMiss


class RoutedLLM:
    def __init__(self, inner: LLM, model: str) -> None:
        self._inner = inner
        self.model = model

    def complete(self, call: LLMCall) -> LLMResult:
        return self._inner.complete(replace(call, model=self.model, effort=None))


class ReplayFirstLLM:
    def __init__(self, replay: LLM, fallback: LLM) -> None:
        self._replay = replay
        self._fallback = fallback
        self.replayed = 0
        self.fell_back = 0

    def complete(self, call: LLMCall) -> LLMResult:
        try:
            result = self._replay.complete(call)
        except ReplayMiss:
            self.fell_back += 1
            return self._fallback.complete(call)
        self.replayed += 1
        return result
