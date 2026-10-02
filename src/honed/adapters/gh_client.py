"""Runs `gh api` (GraphQL and REST) with retries, backoff, page shrinking on timeouts, rate-limit pauses and a
per-run point budget."""

from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from honed.ports.code_host import BudgetExhausted, HostError, NotFound

log = logging.getLogger(__name__)

_TRANSIENT = (
    "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "timeout", "timed out", "Something went wrong",
    "connection reset", "EOF", "TLS handshake", "temporarily", "couldn't respond",
)  # fmt: skip
_RATE_LIMITED = ("rate limit", "HTTP 429", "abuse")


class _Failure(Enum):
    TRANSIENT = auto()
    RATE_LIMITED = auto()
    NOT_FOUND = auto()
    FATAL = auto()


@dataclass(frozen=True)
class GhOptions:
    max_retries: int
    backoff_s: float
    min_remaining: int
    point_budget: int
    min_page: int
    timeout_s: float


class GhClient:
    def __init__(
        self,
        options: GhOptions,
        *,
        run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._options = options
        self._run = run
        self._sleep = sleep
        self.points_used = 0
        self.rest_calls = 0

    # ---- GraphQL -------------------------------------------------------------------------------------------

    def graphql(self, query: str, variables: dict[str, Any], *, shrink: Sequence[str] = (),
                partial: bool = False) -> dict[str, Any]:  # fmt: skip
        """The query's `data`. On a timeout the page-size variables named in `shrink` are halved before retrying.
        With `partial`, an answer whose only errors are NOT_FOUND ones (some of the objects asked for are gone) is
        returned as it is, with null where those objects would be."""
        variables = dict(variables)
        last = ""
        for attempt in range(self._options.max_retries + 1):
            if self.points_used >= self._options.point_budget:
                raise BudgetExhausted(f"GraphQL budget of {self._options.point_budget} points spent")
            proc = self._run(
                ["gh", "api", "graphql", "--input", "-"],
                input=json.dumps({"query": query, "variables": variables}),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._options.timeout_s,
            )
            payload = _json(proc.stdout)
            data, errors = payload.get("data"), payload.get("errors") or []
            if data and ((not errors and proc.returncode == 0)
                         or (partial and errors and all(e.get("type") == "NOT_FOUND" for e in errors))):  # fmt: skip
                self._account(data.get("rateLimit"))
                return data
            last = (json.dumps(errors)[:400] if errors else proc.stderr.strip()[:400]) or "empty response"
            failure = _classify(proc.stderr, errors)
            if failure is _Failure.NOT_FOUND:
                raise NotFound(last)
            if failure is _Failure.FATAL:
                raise HostError(last)
            if failure is _Failure.RATE_LIMITED:
                self._wait_for_reset(attempt)
                continue
            for key in shrink:
                variables[key] = max(self._options.min_page, variables[key] // 2)
            log.warning("GraphQL retry %d (%s); pages %s", attempt + 1, last[:120], {k: variables[k] for k in shrink})
            self._sleep(self._options.backoff_s * 2**attempt)
        raise HostError(f"GraphQL failed after {self._options.max_retries + 1} attempts: {last}")

    def _account(self, rate: dict[str, Any] | None) -> None:
        if not rate:
            return
        self.points_used += int(rate.get("cost") or 0)
        remaining = int(rate.get("remaining") or 0)
        if remaining < self._options.min_remaining:
            reset = dt.datetime.fromisoformat(rate["resetAt"].replace("Z", "+00:00"))
            wait = max(0.0, (reset - dt.datetime.now(dt.UTC)).total_seconds()) + 5
            log.warning("GraphQL points low (%d left); pausing %.0fs until the reset", remaining, wait)
            self._sleep(wait)

    def _wait_for_reset(self, attempt: int) -> None:
        wait = 60.0 * (attempt + 1)
        try:
            limits = json.loads(self._gh(["rate_limit"]).decode())
            reset = limits["resources"]["graphql"]["reset"] if limits["resources"]["graphql"]["remaining"] < 1 else 0
            if reset:
                wait = max(wait, reset - time.time() + 5)
        except (HostError, KeyError, ValueError):
            pass
        log.warning("rate limited; pausing %.0fs", wait)
        self._sleep(wait)

    # ---- REST ----------------------------------------------------------------------------------------------

    def rest(self, path: str, *, accept: str | None = None) -> bytes:
        """The raw response body of a REST GET."""
        args = [path] + (["-H", f"Accept: {accept}"] if accept else [])
        last = ""
        for attempt in range(self._options.max_retries + 1):
            try:
                return self._gh(args)
            except _GhFailed as failed:
                last = failed.message
                if failed.kind is _Failure.NOT_FOUND:
                    raise NotFound(last) from None
                if failed.kind is _Failure.FATAL:
                    raise HostError(last) from None
                if failed.kind is _Failure.RATE_LIMITED:
                    self._wait_for_reset(attempt)
                    continue
                log.warning("REST retry %d for %s (%s)", attempt + 1, path, last[:120])
                self._sleep(self._options.backoff_s * 2**attempt)
        raise HostError(f"REST {path} failed after {self._options.max_retries + 1} attempts: {last}")

    def _gh(self, args: list[str]) -> bytes:
        self.rest_calls += 1
        proc = self._run(["gh", "api", *args], capture_output=True, timeout=self._options.timeout_s)
        if proc.returncode == 0:
            return proc.stdout
        stderr = proc.stderr.decode(errors="replace") if isinstance(proc.stderr, bytes) else proc.stderr
        raise _GhFailed(_classify(stderr, []), stderr.strip()[:400])


class _GhFailed(Exception):
    def __init__(self, kind: _Failure, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _classify(stderr: str, errors: list[dict[str, Any]]) -> _Failure:
    types = {e.get("type") for e in errors}
    messages = " ".join(str(e.get("message", "")) for e in errors) + " " + stderr
    if "NOT_FOUND" in types or "HTTP 404" in stderr:
        return _Failure.NOT_FOUND
    if "RATE_LIMITED" in types or any(s in messages for s in _RATE_LIMITED):
        return _Failure.RATE_LIMITED
    if any(s.lower() in messages.lower() for s in _TRANSIENT) or (not errors and not stderr.strip()):
        return _Failure.TRANSIENT
    return _Failure.FATAL
