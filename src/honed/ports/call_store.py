"""CallStore: the LLM call cache and the usage ledger (ARCHITECTURE.md section 8).

The cache maps a `CallKey` to the answer and its usage, so jobs are idempotent and offline runs replay answers.
The ledger has one row per call made through the cache, live or cached, tagged with its stage and PR.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol

from honed.ports.llm import CallKey, LLMCall, LLMResult


@dataclass(frozen=True)
class LedgerEntry:
    at: str  # ISO timestamp
    run_id: str
    stage: str
    pr: str | None
    model: str
    key: str  # CallKey digest
    cached: bool  # served from the cache: no model call was made
    ok: bool  # False when the call raised
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class CachedCall:
    """One call-cache row as it is stored (a bundle carries the judge's): the key's parts and digest, the answer
    and its usage. No prompt text: only hashes of it."""

    key: str  # CallKey digest
    model: str
    effort: str
    sample: int
    system_hash: str
    input_hash: str
    schema_hash: str
    stage: str
    text: str
    data: Any  # the structured answer, or None
    stop_reason: str | None
    answered_by: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    duration_s: float
    created_at: str


class CallStore(Protocol):
    def get(self, key: CallKey) -> LLMResult | None: ...

    def put(self, key: CallKey, call: LLMCall, result: LLMResult) -> None: ...

    def cached(self, models: Collection[str]) -> list[CachedCall]:
        """The cache rows of these models, oldest first."""
        ...

    def add_cached(self, entry: CachedCall) -> bool:
        """Add a row unless its key is already cached (the local answer stays); True when added."""
        ...

    def record(self, entry: LedgerEntry) -> None: ...

    def ledger(self) -> list[LedgerEntry]: ...
