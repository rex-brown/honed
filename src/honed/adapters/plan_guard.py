"""Keeps `claude -p` runs inside the subscription's included limits, and writes a heartbeat file after every call.

Headless Claude Code reports plan usage in `rate_limit_event`s on its stream-json output (`rate_limit_info`: status
allowed / allowed_warning / rejected, `resetsAt`, `rateLimitType`, `overageStatus`, `isUsingOverage`, and per-window
`unifiedWindows.<window>.utilization`). The guard trips, and every later call raises `UsageLimitReached` before it
starts, when any of these holds:
- status "rejected" (a plan window at its limit), or a status the guard doesn't know;
- any overage or usage-credit indicator: overage in use, or overage available (going over a limit would spend
  usage credits), or a non-null `fallback_credit` in the result's usage;
- a window's utilization at or above its threshold: `stop_at_weekly_utilization` for every window whose name starts
  with `seven_day` (the weekly windows, the model-specific ones included), `stop_at_utilization` for the 5-hour
  window and any other;
- a call that reported no usage signal at all, when `require_usage_signal` is set (fail closed);
- a limit or quota error.
Status "allowed_warning" is advisory: the reading records it (`warning`, and in the heartbeat) and the run carries on;
only the thresholds and "rejected" stop it for a plan window. It also enforces the run's cap on live calls. The guard
never enables overage or usage credits.

Stops for a plan window alone (a `rejected` status, a window at its threshold, a limit error with a reset time) can be
waited out (`PlanWindow`, `--wait-for-reset`) until the reset of the window that stopped the run; any other reason
makes the stop permanent, also when it arrives after a window stop.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from honed.ports.llm import CallCapReached, UsageLimitReached

_OVERAGE_OFF = {"rejected", "disabled", None}
_ALLOWED = "allowed"
_WARNING = "allowed_warning"  # a plan window nearing its limit: advisory, recorded, never a stop by itself
_AT_LIMIT = "rejected"  # a plan window at its limit: over at its reset
_WEEKLY = "seven_day"  # a window whose name starts with this uses the weekly threshold


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat()


def _latest(times: Iterable[str | None]) -> str | None:
    """The latest of some ISO times, None when there are none."""
    return max((t for t in times if t), key=dt.datetime.fromisoformat, default=None)


def _later(a: str, b: str | None) -> bool:
    """Whether ISO time `a` is after `b`: True when `b` is missing, False when either can't be read."""
    try:
        return b is None or dt.datetime.fromisoformat(a) > dt.datetime.fromisoformat(b)
    except ValueError:
        return False


@dataclass(frozen=True)
class PlanReading:
    """One call's view of the plan limits, and why it means stop (None: carry on)."""

    status: str | None
    utilization: float | None  # the highest window utilization reported
    resets_at: str | None  # ISO time the window that stops the run resets (else the reported limit's)
    overage: bool | None  # True: overage in use or available; False: explicitly off; None: not reported
    stop_reason: str | None
    permanent: bool = False  # the stop has a reason no plan-window reset clears
    warning: bool = False  # status "allowed_warning": a window nears its limit (advisory, not a stop)
    window_utilizations: Mapping[str, float] = field(default_factory=dict, hash=False)  # window -> utilization


def _windows(info: Mapping[str, Any]) -> dict[str, tuple[float, Any]]:
    """Each reported window's (utilization, resetsAt epoch): the `unifiedWindows`, and the top-level `utilization`
    under its `rateLimitType` (the higher utilization when both report the same window)."""
    found: dict[str, tuple[float, Any]] = {}

    def add(name: str, utilization: Any, resets: Any) -> None:
        if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
            return
        seen = found.get(name)
        if seen is None:
            found[name] = (utilization, resets)
        else:
            found[name] = (max(seen[0], utilization), seen[1] if seen[1] is not None else resets)

    for name, window in (info.get("unifiedWindows") or {}).items():
        if isinstance(window, Mapping):
            add(str(name), window.get("utilization"), window.get("resetsAt"))
    add(str(info.get("rateLimitType") or "unknown window"), info.get("utilization"), info.get("resetsAt"))
    return found


def read_plan(info: Mapping[str, Any] | None, *, stop_at_utilization: float, stop_at_weekly_utilization: float,
              fallback_credit: Any = None, require_signal: bool = True) -> PlanReading:  # fmt: skip
    """Decide from a `rate_limit_info` object (None when the call reported none)."""
    if not info:
        reason = "no plan-usage signal in the call's output" if require_signal else None
        return PlanReading(None, None, None, None, reason, permanent=reason is not None)

    def threshold(window: str) -> float:
        return stop_at_weekly_utilization if window.startswith(_WEEKLY) else stop_at_utilization

    status = info.get("status")
    windows = _windows(info)
    utilization = max((u for u, _ in windows.values()), default=None)
    tripped = {name: windows[name] for name in sorted(windows) if windows[name][0] >= threshold(name)}
    # The reset that matters: the latest among the windows that stop the run, else the reported limit's.
    stopping = [_iso(resets) for _, resets in tripped.values()]
    if status == _AT_LIMIT:
        stopping.append(_iso(info.get("resetsAt")))
    fullest = max(windows.values(), key=lambda w: w[0], default=(0, None))
    resets_at = _latest(stopping) or _iso(info.get("resetsAt")) or _iso(fullest[1])
    using = info.get("isUsingOverage")
    available = info.get("overageStatus") not in _OVERAGE_OFF
    overage = True if (using or available) else (False if using is False or "overageStatus" in info else None)
    reasons: list[tuple[str, bool]] = []  # (reason, cleared by the window's reset)
    if status not in (_ALLOWED, _WARNING):
        what = f"plan status {status!r} ({info.get('rateLimitType') or 'unknown window'})"
        reasons.append((what, status == _AT_LIMIT))
    if using:
        reasons.append(("usage is spilling into overage (usage credits)", False))
    elif available:
        state = info.get("overageStatus")
        reasons.append((f"overage is available (overageStatus {state!r}): going over a limit would spend credits",
                        False))  # fmt: skip
    if fallback_credit is not None:
        reasons.append((f"the call reports a usage-credit fallback ({fallback_credit!r})", False))
    for name, (used, _) in tripped.items():
        reasons.append((f"plan utilization {used:.2f} >= {threshold(name):.2f} ({name})", True))
    return PlanReading(status, utilization, resets_at, overage, "; ".join(r for r, _ in reasons) or None,
                       permanent=not all(window for _, window in reasons), warning=status == _WARNING,
                       window_utilizations={name: used for name, (used, _) in windows.items()})  # fmt: skip


class StatusFile:
    """`data/llm_status.json`, rewritten atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, status: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self._path.parent, delete=False, suffix=".tmp") as tmp:
            json.dump(status, tmp, indent=2)
        os.replace(tmp.name, self._path)


class PlanGuard:
    """Thread-safe: counts live calls against the cap, keeps the latest plan reading, trips once and stays tripped
    until `resume` (a waited-out plan window). Implements `ports.llm.PlanWindow`."""

    def __init__(
        self,
        *,
        cap: int,
        stop_at_utilization: float,
        stop_at_weekly_utilization: float,
        require_signal: bool,
        write_status: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.cap = cap
        self._threshold = stop_at_utilization
        self._weekly_threshold = stop_at_weekly_utilization
        self._require_signal = require_signal
        self._write = write_status
        self._lock = threading.Lock()
        self.calls = 0
        self.last: PlanReading | None = None
        self.stopped: str | None = None
        self.resets_at: str | None = None
        self.waiting_until: str | None = None
        self._permanent = False
        self._rechecked = True

    def _heartbeat(self) -> None:
        if self._write is None:
            return
        last = self.last
        self._write({
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            "calls": self.calls,
            "cap": self.cap,
            "rate_limit_status": last.status if last else None,
            "warning": last.warning if last else None,
            "utilization": last.utilization if last else None,
            "window_utilizations": dict(last.window_utilizations) if last else None,
            "resets_at": (last.resets_at if last else None) or self.resets_at,
            "overage": last.overage if last else None,
            "stopped": self.stopped,
            "waiting_until": self.waiting_until,
        })  # fmt: skip

    def _trip(self, reason: str, resets_at: str | None, *, window: bool) -> None:
        """Record a stop. The first reason is kept; a later one no reset clears makes the stop permanent, and a
        later window reason with a later reset moves the reset."""
        if self.stopped is None:
            self.stopped, self.resets_at, self._permanent = reason, resets_at, not window
        elif not window and not self._permanent:
            self.stopped, self._permanent = f"{self.stopped}; then {reason}", True
        elif window and resets_at and _later(resets_at, self.resets_at):
            self.resets_at = resets_at

    def start(self) -> None:
        """Write the first heartbeat of a run."""
        with self._lock:
            self._heartbeat()

    def before_call(self) -> None:
        """Reserve one live call, or raise when the guard has tripped or the cap is spent."""
        with self._lock:
            if self.stopped is not None:
                raise UsageLimitReached(self.stopped, resets_at=self.resets_at)
            if self.calls >= self.cap:
                self._trip(f"call cap reached ({self.cap} live calls this run)", None, window=False)
                self._heartbeat()
                raise CallCapReached(self.stopped or "call cap reached")
            self.calls += 1

    def observe(self, info: Mapping[str, Any] | None, *, fallback_credit: Any = None) -> PlanReading:
        """Record a finished call's plan reading; trips the guard when it says stop."""
        reading = read_plan(info, stop_at_utilization=self._threshold,
                            stop_at_weekly_utilization=self._weekly_threshold, fallback_credit=fallback_credit,
                            require_signal=self._require_signal)  # fmt: skip
        with self._lock:
            if info or self.last is None:
                self.last = reading
            if info:
                self._rechecked = True
            if reading.stop_reason:
                self._trip(reading.stop_reason, reading.resets_at, window=not reading.permanent)
            self._heartbeat()
        return reading

    def stop(self, reason: str, *, resets_at: str | None = None, window: bool = False) -> None:
        """Trip the guard for a reason found outside the plan reading: a limit error (`window` when its reset
        clears it), the network, an isolation breach."""
        with self._lock:
            self._trip(reason, resets_at, window=window)
            self._heartbeat()

    def after_failure(self) -> None:
        """Refresh the heartbeat after a call that failed without a plan reading."""
        with self._lock:
            self._heartbeat()

    # ---- PlanWindow ----------------------------------------------------------------------------------------

    def resumable_at(self) -> str | None:
        with self._lock:
            return None if self.stopped is None or self._permanent else self.resets_at

    def wait_until(self, until: str) -> None:
        with self._lock:
            self.waiting_until = until
            self._heartbeat()

    def resume(self) -> None:
        with self._lock:
            self.stopped = self.resets_at = self.waiting_until = None
            self._permanent, self._rechecked = False, False
            self._heartbeat()

    def rechecked(self) -> bool:
        with self._lock:
            return self._rechecked
