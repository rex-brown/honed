"""An LLM decorator that caps the live model calls in flight (`[llm] concurrency`) across every service sharing it:
nested job runners (evaluation rounds, each with a parallel finder panel) never exceed the cap. Placed under the call
cache, so cached answers don't wait for a slot."""

from __future__ import annotations

import threading

from honed.ports.llm import LLM, LLMCall, LLMResult


class ConcurrencyLimit:
    def __init__(self, inner: LLM, slots: int) -> None:
        self._inner = inner
        self._slots = threading.BoundedSemaphore(max(1, slots))

    def complete(self, call: LLMCall) -> LLMResult:
        with self._slots:
            return self._inner.complete(call)
