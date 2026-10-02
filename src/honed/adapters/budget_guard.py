"""Keeps an `anthropic` run inside its dollar budget and live-call cap, and writes the heartbeat after every call
(ARCHITECTURE.md section 8). The API-key counterpart of `PlanGuard`: there is no plan window to wait out, so every
stop is final for the run.

The budget (`[llm.anthropic] run_budget_usd`) is a ceiling, not a target: before a live call the guard reserves the
call's worst case (its input, all written to the prompt cache, plus `max_tokens` of output, at the requested model's
prices), and refuses with `BudgetExhausted` when the cost so far plus every reservation in flight plus this one would
pass the budget. When the call ends, its reservation is replaced by its real cost, from the response's usage and
`[llm.anthropic.prices]`. A message batch is admitted the same way, as the longest prefix of its requests that fits.
(A refusal answered by a pricier fallback model is charged at that model's prices, which the reservation did not
foresee.) The heartbeat (`[paths] llm_status`) holds `updated_at`, `backend`, `calls`, `cap`, `spent_usd`,
`reserved_usd`, `budget_usd`, `stopped` and, while a batch is out, `batch` (its id, status and counts).
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from honed.ports.llm import BudgetExhausted, CallCapReached, StopRun


class BudgetGuard:
    """Thread-safe: reserves live calls against the cap and the budget, settles them at their real cost, trips once
    and stays tripped."""

    def __init__(self, *, budget_usd: float, cap: int,
                 write_status: Callable[[Mapping[str, Any]], None] | None = None) -> None:  # fmt: skip
        self.budget_usd = budget_usd
        self.cap = cap
        self._write = write_status
        self._lock = threading.Lock()
        self.calls = 0
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self.stopped: str | None = None
        self._stop: StopRun | None = None
        self.batch: Mapping[str, Any] | None = None

    def _heartbeat(self) -> None:
        if self._write is None:
            return
        self._write({
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            "backend": "anthropic",
            "calls": self.calls,
            "cap": self.cap,
            "spent_usd": round(self.spent_usd, 6),
            "reserved_usd": round(self.reserved_usd, 6),
            "budget_usd": self.budget_usd,
            "stopped": self.stopped,
            "batch": dict(self.batch) if self.batch else None,
        })  # fmt: skip

    def _trip(self, error: StopRun) -> None:
        self.stopped, self._stop = str(error), error
        self._heartbeat()
        raise error

    def _raise_if_stopped(self) -> None:
        if self._stop is not None:
            raise type(self._stop)(str(self._stop))

    def _left(self) -> float:
        return self.budget_usd - self.spent_usd - self.reserved_usd

    def start(self) -> None:
        """Write the first heartbeat of a run."""
        with self._lock:
            self._heartbeat()

    def reserve(self, worst_usd: float) -> None:
        """Reserve one live call costing at most `worst_usd`, or raise when the cap or the budget won't allow it."""
        with self._lock:
            self._raise_if_stopped()
            if self.calls >= self.cap:
                self._trip(CallCapReached(f"call cap reached ({self.cap} live calls this run)"))
            if worst_usd > self._left():
                self._trip(
                    BudgetExhausted(
                        f"budget: ${self.spent_usd:.2f} spent and ${self.reserved_usd:.2f} reserved of "
                        f"${self.budget_usd:.2f} (run_budget_usd); the next call could cost up to ${worst_usd:.2f}"
                    )
                )
            self.calls += 1
            self.reserved_usd += worst_usd

    def admit(self, worst_usd: Sequence[float]) -> int:
        """Reserve a batch: the longest prefix of requests (worst cases `worst_usd`) the cap and the budget allow.
        Raises when not even one fits."""
        with self._lock:
            self._raise_if_stopped()
            room, total, left = 0, 0.0, self._left()
            for worst in worst_usd[: max(0, self.cap - self.calls)]:
                if total + worst > left:
                    break
                room, total = room + 1, total + worst
            if room == 0:
                if self.calls >= self.cap:
                    self._trip(CallCapReached(f"call cap reached ({self.cap} live calls this run)"))
                self._trip(BudgetExhausted(
                    f"budget: ${self.spent_usd:.2f} spent of ${self.budget_usd:.2f} (run_budget_usd); a batch "
                    f"request could cost up to ${worst_usd[0]:.2f}"))  # fmt: skip
            self.calls += room
            self.reserved_usd += total
            self._heartbeat()
            return room

    def settle(self, reserved_usd: float, cost_usd: float) -> None:
        """Replace a reservation with the real cost (0 for a call that cost nothing)."""
        with self._lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reserved_usd)
            self.spent_usd += cost_usd
            self._heartbeat()

    def note_batch(self, info: Mapping[str, Any] | None) -> None:
        """The batch in flight (id, status, counts) for the heartbeat; None once it has ended."""
        with self._lock:
            self.batch = info
            self._heartbeat()

    def stop(self, error: StopRun) -> None:
        """Trip for a reason found outside the budget (the key was refused, the network); the first reason stays."""
        with self._lock:
            if self._stop is None:
                self.stopped, self._stop = str(error), error
            self._heartbeat()
