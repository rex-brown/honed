"""LLM backend on headless Claude Code (`claude -p`) and the user's Claude subscription (ARCHITECTURE.md section 8).

This is the documented way to use a subscription from scripts. The subscription's OAuth token is never read here
and never sent to the Messages API: Claude Code authenticates itself. `ANTHROPIC_API_KEY` (and every other
`ANTHROPIC_*`/`CLAUDE*` variable of the parent process) is removed from the child's environment, so a call can never
bill an API key, and the init event must report `apiKeySource: none`.

Isolation, per call:
- a fresh, empty temporary working directory;
- no tools (`--tools ""`; `--json-schema` adds only the StructuredOutput tool), `--strict-mcp-config` with an empty
  MCP config, one turn (two with a JSON schema: room for one more StructuredOutput attempt after an invalid one),
  no session persistence, slash commands and skills disabled, a replaced system prompt,
  `--setting-sources ""`, auto memory and CLAUDE.md loading disabled by environment;
- `--safe-mode` (customizations off, the existing login), and in `config_dir` mode also a fresh `CLAUDE_CONFIG_DIR`
  with `CLAUDE_CODE_OAUTH_TOKEN` from the environment or a macOS Keychain item.
Every call is checked against its `system/init` event (`isolation_problems`), and a breach stops the run.

Output is `stream-json --verbose`: the final `result` event (text or `structured_output`, usage, `total_cost_usd`,
duration, error subtype) plus the `rate_limit_event`s that `PlanGuard` reads. Limit errors raise
`UsageLimitReached` (a plan-window stop when they carry a reset time and don't mention credits or overage);
network and login failures raise `BackendUnavailable` without retrying; other failures are retried with backoff.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from honed.adapters.plan_guard import PlanGuard
from honed.ports.llm import (
    BackendUnavailable,
    IsolationBreach,
    LLMCall,
    LLMError,
    LLMResult,
    Usage,
    UsageLimitReached,
    canonical_schema,
)

log = logging.getLogger(__name__)

SAFE_MODE, CONFIG_DIR = "safe_mode", "config_dir"
STRUCTURED_TOOL = "StructuredOutput"
# Turns per call. With a schema the answer is a StructuredOutput tool call, and one turn left no room to retry an
# invalid one (phase 3: about 1.5% of structured calls ended in error_max_turns). The only tool is still that one.
MAX_TURNS, STRUCTURED_MAX_TURNS = 1, 2
# Parent-process variables never passed to the child: credentials, and the parent Claude Code session's own state.
SCRUBBED_PREFIXES = ("ANTHROPIC", "CLAUDE", "AI_AGENT")

# Limit errors about usage credits or overage: no plan-window reset clears them.
_CREDIT = re.compile(r"usage credits|credit balance|credits_required|extra usage|overage|spend limit", re.I)
_LIMIT = re.compile(
    r"usage limit|hit your (?:\w+ )?limit|rate[ _-]?limit|limit reached|too many requests|quota|" + _CREDIT.pattern,
    re.I,
)
_NETWORK = re.compile(
    r"connection error|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH|getaddrinfo"
    r"|network (?:is )?unreachable|fetch failed|unable to connect|socket hang up|could not resolve|are you offline"
    r"|couldn't complete the request",
    re.I,
)
_LOGIN = re.compile(
    r"/login|not logged in|log in|invalid api key|authentication[_ ]error|oauth token|\b401\b|unauthorized", re.I
)
_MODEL = re.compile(r"not a recognized model|model.{0,40}(?:not available|unavailable|not found)|model_not_found", re.I)
_EPOCH = re.compile(r"\|(\d{10})\b")


@dataclass(frozen=True)
class ClaudeCodeOptions:
    binary: str = "claude"
    isolation: str = SAFE_MODE  # "safe_mode" or "config_dir"
    oauth_token_env: str = "CLAUDE_CODE_OAUTH_TOKEN"  # config_dir: the token's environment variable ...
    keychain_service: str = ""  # ... else this macOS Keychain generic-password item
    timeout_s: float = 600.0
    max_retries: int = 2  # our retries of transient failures
    backoff_s: float = 10.0
    cli_max_retries: int = 2  # CLAUDE_CODE_MAX_RETRIES: Claude Code's own API retries, kept low to fail fast
    extra_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamOutput:
    """What one `claude -p --output-format stream-json --verbose` run printed."""

    init: Mapping[str, Any] | None
    result: Mapping[str, Any] | None
    rate_limit: Mapping[str, Any] | None  # the last rate_limit_info
    assistant_model: str | None
    errors: tuple[str, ...]  # API error texts carried by assistant messages


def parse_stream(stdout: str) -> StreamOutput:
    init = result = rate_limit = None
    model = None
    errors: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = event
        elif kind == "result":
            result = event
        elif kind == "rate_limit_event":
            rate_limit = event.get("rate_limit_info") or rate_limit
        elif kind == "assistant":
            message = event.get("message") or {}
            model = message.get("model") or model
            if event.get("error") or message.get("error"):
                errors.append(str(event.get("error") or message.get("error")))
    return StreamOutput(init, result, rate_limit, model, tuple(errors))


def isolation_problems(init: Mapping[str, Any] | None, *, structured: bool) -> list[str]:
    """What the init event shows that an isolated call must not have. Claude Code's own built-in plugins
    (source `<name>@builtin`) are part of the program, not user customizations, and are allowed."""
    if init is None:
        return ["no system/init event"]
    problems = []
    extra = set(init.get("tools") or []) - ({STRUCTURED_TOOL} if structured else set())
    if extra:
        problems.append(f"tools {sorted(extra)}")
    if init.get("mcp_servers"):
        problems.append(f"MCP servers {[s.get('name') for s in init['mcp_servers']]}")
    user_plugins = [p.get("source") or p.get("name") for p in init.get("plugins") or []
                    if not str(p.get("source", "")).endswith("@builtin")]  # fmt: skip
    if user_plugins:
        problems.append(f"plugins {user_plugins}")
    for key in ("skills", "slash_commands"):
        if init.get(key):
            problems.append(f"{key} {init[key][:5]}")
    if init.get("apiKeySource") not in (None, "none"):
        problems.append(f"an API key is in use (apiKeySource {init.get('apiKeySource')!r})")
    memory = init.get("memory_paths")
    if memory and any(memory.values()):
        problems.append(f"memory paths {memory}")
    return problems


def child_env(
    base: Mapping[str, str], options: ClaudeCodeOptions, *, max_tokens: int, config_dir: Path | None,
    token: str | None,
) -> dict[str, str]:  # fmt: skip
    env = {k: v for k, v in base.items() if not k.upper().startswith(SCRUBBED_PREFIXES)}
    env.update(
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
        CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
        CLAUDE_CODE_MAX_RETRIES=str(options.cli_max_retries),
        CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(max_tokens),
    )
    env.update(options.extra_env)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def build_command(call: LLMCall, options: ClaudeCodeOptions, system_file: Path) -> list[str]:
    """The argv for one isolated call. The user content goes on stdin, never on the command line."""
    cmd = [
        options.binary, "-p",
        "--model", call.model,
        "--system-prompt-file", str(system_file),
        "--output-format", "stream-json", "--verbose",
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--max-turns", str(STRUCTURED_MAX_TURNS if call.schema is not None else MAX_TURNS),
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--safe-mode",
    ]  # fmt: skip
    if call.effort:
        cmd += ["--effort", call.effort]
    if call.schema is not None:
        cmd += ["--json-schema", canonical_schema(call.schema)]
    return cmd


def _usage(result: Mapping[str, Any]) -> Usage:
    usage = result.get("usage") or {}
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cost_usd=float(result.get("total_cost_usd") or 0.0),
        duration_s=float(result.get("duration_ms") or 0) / 1000,
    )


class _Transient(Exception):
    """A failure worth retrying."""


class ClaudeCodeLLM:
    def __init__(self, options: ClaudeCodeOptions, guard: PlanGuard, *, environ: Mapping[str, str] | None = None):
        self._options = options
        self._guard = guard
        self._environ = dict(os.environ if environ is None else environ)
        self._token: str | None = None
        self.last_init: Mapping[str, Any] | None = None  # the latest call's system/init event, for diagnostics
        if options.isolation not in (SAFE_MODE, CONFIG_DIR):
            raise ValueError(f"unknown isolation mode {options.isolation!r}")

    @property
    def guard(self) -> PlanGuard:
        return self._guard

    def complete(self, call: LLMCall) -> LLMResult:
        attempts = self._options.max_retries + 1
        for attempt in range(attempts):
            self._guard.before_call()
            try:
                return self._once(call)
            except _Transient as error:
                self._guard.after_failure()
                if attempt + 1 == attempts:
                    raise LLMError(f"{call.stage or 'call'} failed after {attempts} attempts: {error}") from None
                delay = self._options.backoff_s * 2**attempt
                log.warning("claude -p transient failure (%s); retrying in %.0fs", error, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")

    # ---- one process -------------------------------------------------------------------------------------

    def _once(self, call: LLMCall) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="honed-cc-") as private, \
                tempfile.TemporaryDirectory(prefix="honed-cwd-") as cwd:  # fmt: skip
            system_file = Path(private) / "system.md"
            system_file.write_text(call.system)
            config_dir = None
            if self._options.isolation == CONFIG_DIR:
                config_dir = Path(private) / "config"
                config_dir.mkdir()
            env = child_env(self._environ, self._options, max_tokens=call.max_tokens, config_dir=config_dir,
                            token=self._oauth_token() if config_dir else None)  # fmt: skip
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    build_command(call, self._options, system_file), cwd=cwd, env=env, input=call.user,
                    capture_output=True, encoding="utf-8", errors="replace", timeout=self._options.timeout_s,
                    check=False,
                )  # fmt: skip
            except FileNotFoundError:
                reason = f"Claude Code CLI not found: {self._options.binary!r}"
                self._guard.stop(reason)
                raise BackendUnavailable(reason) from None
            except subprocess.TimeoutExpired:
                raise _Transient(f"timed out after {self._options.timeout_s:.0f}s") from None
        elapsed = time.monotonic() - started
        out = parse_stream(proc.stdout)
        self.last_init = out.init
        result = out.result
        succeeded = result is not None and not result.get("is_error") and result.get("subtype") == "success"
        fallback = ((result or {}).get("usage") or {}).get("fallback_credit")
        # A successful call must carry a plan-usage signal; a failed one is read only when it has one.
        reading = self._guard.observe(out.rate_limit, fallback_credit=fallback) if succeeded or out.rate_limit else None
        if not succeeded:
            self._fail(out, proc, reading)
        assert result is not None
        problems = isolation_problems(out.init, structured=call.schema is not None)
        if problems:
            reason = "isolation breach: " + "; ".join(problems)
            self._guard.stop(reason)
            raise IsolationBreach(reason)
        data = result.get("structured_output")
        text = result.get("result") or ""
        if call.schema is not None and data is None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise _Transient("no structured output in the result") from None
        usage = _usage(result)
        if not usage.duration_s:
            usage = replace(usage, duration_s=round(elapsed, 3))
        model = out.assistant_model or next(iter(result.get("modelUsage") or {}), call.model)
        log.info("claude -p %s%s: %.1fs, in %d (+%d cached), out %d, $%.4f shadow", call.stage or "call",
                 f" {call.pr}" if call.pr else "", usage.duration_s, usage.input_tokens + usage.cache_write_tokens,
                 usage.cache_read_tokens, usage.output_tokens, usage.cost_usd)  # fmt: skip
        return LLMResult(text=text, data=data, usage=usage, model=model, stop_reason=result.get("stop_reason"))

    def _fail(self, out: StreamOutput, proc: subprocess.CompletedProcess, reading: Any) -> None:
        """Raise the error a failed run means."""
        result = out.result or {}
        text = " | ".join(
            str(part) for part in (result.get("result"), *out.errors, result.get("api_error"), proc.stderr[-2000:])
            if part
        )  # fmt: skip
        status = result.get("api_error_status")
        subtype = result.get("subtype") or f"exit {proc.returncode}"
        limited = (out.rate_limit or {}).get("status") == "rejected" or status == 429 or bool(_LIMIT.search(text))
        if limited:
            epoch = _EPOCH.search(text)
            resets = (reading.resets_at if reading else None) or (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch.group(1)))) if epoch else None
            )
            reason = f"usage or rate limit: {text[:300]}"
            self._guard.stop(reason, resets_at=resets, window=resets is not None and not _CREDIT.search(text))
            raise UsageLimitReached(reason, resets_at=resets)
        for pattern, what in ((_NETWORK, "network unavailable"), (_LOGIN, "not logged in"),
                              (_MODEL, "model unavailable")):  # fmt: skip
            if pattern.search(text):
                reason = f"{what}: {text[:300]}"
                self._guard.stop(reason)
                raise BackendUnavailable(reason)
        if not out.result and not proc.stdout.strip():
            reason = f"claude -p exited {proc.returncode} with no output: {proc.stderr.strip()[:300]}"
            self._guard.stop(reason)
            raise BackendUnavailable(reason)
        raise _Transient(f"{subtype} (api status {status}): {text[:300]}")

    def _oauth_token(self) -> str:
        """config_dir mode: the long-lived token from `claude setup-token`, from the environment or the Keychain.
        Never logged."""
        if self._token:
            return self._token
        token = self._environ.get(self._options.oauth_token_env, "").strip()
        if not token and self._options.keychain_service:
            try:
                found = subprocess.run(
                    ["security", "find-generic-password", "-s", self._options.keychain_service, "-w"],
                    capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=False,
                )  # fmt: skip
            except (FileNotFoundError, subprocess.TimeoutExpired):
                found = None
            token = found.stdout.strip() if found is not None and found.returncode == 0 else ""
        if not token:
            keychain = self._options.keychain_service
            reason = (
                f"config_dir isolation needs a token from `claude setup-token` in ${self._options.oauth_token_env}"
                + (f" or the Keychain item {keychain!r}" if keychain else " (or a Keychain item: keychain_service)")
            )
            self._guard.stop(reason)
            raise BackendUnavailable(reason)
        self._token = token
        return token


def version(binary: str = "claude") -> str | None:
    """The installed CLI's version line, or None when it is missing."""
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None
