"""The claude_code backend against a scripted stand-in for the `claude` CLI: isolation flags and environment,
output parsing, the plan-usage guard and its heartbeat, limits, network failures and retries. No network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import claude_events as ev
from honed.adapters.claude_code_llm import (
    ClaudeCodeLLM,
    ClaudeCodeOptions,
    build_command,
    isolation_problems,
    parse_stream,
)
from honed.adapters.plan_guard import PlanGuard, PlanReading, StatusFile, read_plan
from honed.ports.llm import (
    BackendUnavailable,
    CallCapReached,
    IsolationBreach,
    LLMCall,
    LLMError,
    UsageLimitReached,
)

FAKE = Path(__file__).parent / "fake_claude.py"
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
HEARTBEAT_KEYS = {"updated_at", "calls", "cap", "rate_limit_status", "warning", "utilization", "window_utilizations",
                  "resets_at", "overage", "stopped", "waiting_until"}  # fmt: skip


class Rig:
    def __init__(self, tmp_path: Path, runs: list[dict], *, cap: int = 10, isolation: str = "safe_mode",
                 environ: dict[str, str] | None = None, max_retries: int = 2) -> None:  # fmt: skip
        self.scenario = tmp_path / "scenario.json"
        self.scenario.write_text(json.dumps({"runs": runs}))
        self.log = tmp_path / "invocations.jsonl"
        self.status = tmp_path / "llm_status.json"
        binary = tmp_path / "claude"
        binary.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
        binary.chmod(0o755)
        base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_CLAUDE_SCENARIO": str(self.scenario),
                "FAKE_CLAUDE_LOG": str(self.log)}  # fmt: skip
        base.update(environ or {})
        self.guard = PlanGuard(cap=cap, stop_at_utilization=0.8, stop_at_weekly_utilization=0.85, require_signal=True,
                               write_status=StatusFile(self.status).write)  # fmt: skip
        options = ClaudeCodeOptions(binary=str(binary), isolation=isolation, backoff_s=0.0, max_retries=max_retries,
                                    keychain_service="")  # fmt: skip
        self.llm = ClaudeCodeLLM(options, self.guard, environ=base)

    @property
    def invocations(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @property
    def heartbeat(self) -> dict:
        return json.loads(self.status.read_text())


def call(schema=SCHEMA, **kw) -> LLMCall:
    return LLMCall(model="claude-fable-5-1", system="You judge.", user="<untrusted_pr_data>x</untrusted_pr_data>",
                   schema=schema, effort="medium", stage="addressed", pr="o/r#1", **kw)  # fmt: skip


def test_a_call_without_a_schema_has_one_turn(tmp_path):
    argv = build_command(LLMCall(model="m", system="s", user="u"), ClaudeCodeOptions(), tmp_path / "system.md")
    assert argv[argv.index("--max-turns") + 1] == "1" and "--json-schema" not in argv


def test_a_call_runs_isolated_and_parses_structured_output(tmp_path):
    rig = Rig(tmp_path, [{"events": ev.success({"verdict": "addressed"})}],
              environ={"ANTHROPIC_API_KEY": "sk-should-never-pass", "CLAUDE_EFFORT": "max", "CLAUDECODE": "1",
                       "CLAUDE_CODE_SESSION_ID": "parent"})  # fmt: skip
    result = rig.llm.complete(call(max_tokens=4000))
    assert result.data == {"verdict": "addressed"} and result.model == "claude-fable-5-1"
    assert result.usage.output_tokens == 1149 and result.usage.cache_write_tokens == 1088
    assert result.usage.cost_usd == pytest.approx(0.0792) and result.usage.duration_s == pytest.approx(1.234)

    (inv,) = rig.invocations
    argv = inv["argv"]
    for flag in ("-p", "--verbose", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
                 "--safe-mode"):  # fmt: skip
        assert flag in argv
    pairs = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
    assert pairs["--model"] == "claude-fable-5-1" and pairs["--output-format"] == "stream-json"
    assert pairs["--tools"] == "" and pairs["--max-turns"] == "2" and pairs["--setting-sources"] == ""
    assert json.loads(pairs["--mcp-config"]) == {"mcpServers": {}}
    assert json.loads(pairs["--json-schema"]) == SCHEMA and pairs["--effort"] == "medium"
    assert Path(pairs["--system-prompt-file"]).name == "system.md"  # the system prompt is replaced from a file
    assert inv["stdin"] == "<untrusted_pr_data>x</untrusted_pr_data>"  # user content on stdin, not argv
    assert inv["cwd_files"] == []  # a fresh, empty working directory

    env = inv["env"]
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_EFFORT" not in env and "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env and "CLAUDE_CONFIG_DIR" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4000"

    beat = rig.heartbeat
    assert set(beat) == HEARTBEAT_KEYS
    assert beat["calls"] == 1 and beat["cap"] == 10 and beat["rate_limit_status"] == "allowed"
    assert beat["utilization"] == pytest.approx(0.15) and beat["overage"] is False and beat["stopped"] is None
    assert beat["window_utilizations"] == {"five_hour": 0.07, "seven_day": 0.15} and beat["warning"] is False
    assert beat["resets_at"].startswith("2026-09-")


@pytest.mark.parametrize(
    ("init", "problem"),
    [
        (ev.init(tools=["StructuredOutput", "Bash"]), "tools"),
        (ev.init(mcp_servers=[{"name": "github", "status": "connected"}]), "MCP"),
        (ev.init(plugins=[{"name": "x", "path": "/p", "source": "x@my-marketplace"}]), "plugins"),
        (ev.init(skills=["deploy"]), "skills"),
        (ev.init(apiKeySource="ANTHROPIC_API_KEY"), "API key"),
        (ev.init(memory_paths={"auto": "/Users/x/.claude/memory"}), "memory"),
    ],
)
def test_a_leaky_init_event_stops_the_run(tmp_path, init, problem):
    rig = Rig(tmp_path, [{"events": [init, ev.rate_limit(), ev.result({"verdict": "x"})]}])
    with pytest.raises(IsolationBreach, match=problem):
        rig.llm.complete(call())
    assert problem.split()[0] in rig.heartbeat["stopped"]
    with pytest.raises(UsageLimitReached):  # the guard stays tripped: nothing else runs
        rig.llm.complete(call())
    assert len(rig.invocations) == 1


def test_isolation_allows_only_claude_codes_own_builtin_plugins():
    assert isolation_problems(ev.init(), structured=True) == []
    assert isolation_problems(ev.init(tools=[]), structured=False) == []
    assert "tools" in isolation_problems(ev.init(), structured=False)[0]  # no schema, no StructuredOutput tool
    assert isolation_problems(None, structured=True) == ["no system/init event"]


def test_a_rejected_status_raises_usage_limit_with_the_reset_time(tmp_path):
    events = [ev.init(), ev.rate_limit("rejected"), ev.result(text="You've hit your limit · resets 5pm",
                                                              subtype="success", is_error=True)]  # fmt: skip
    rig = Rig(tmp_path, [{"events": events}])
    with pytest.raises(UsageLimitReached) as caught:
        rig.llm.complete(call())
    assert caught.value.resets_at and caught.value.resets_at.startswith("2026-09-")
    assert len(rig.invocations) == 1  # never retried
    assert rig.heartbeat["rate_limit_status"] == "rejected" and rig.heartbeat["stopped"]
    assert rig.guard.resumable_at() == caught.value.resets_at  # a plan window: --wait-for-reset may wait it out


@pytest.mark.parametrize(
    ("text", "window"),
    [("Claude AI usage limit reached|1790733000", True), ("Your extra usage is exhausted|1790733000", False),
     ("Credit balance is too low|1790733000", False)],
)  # fmt: skip
def test_a_limit_error_is_a_window_stop_only_with_a_reset_and_no_credits(tmp_path, text, window):
    rig = Rig(tmp_path, [{"events": [ev.init(), ev.result(text=text, is_error=True, api_error_status=429)]}])
    with pytest.raises(UsageLimitReached) as caught:
        rig.llm.complete(call())
    assert caught.value.resets_at == "2026-09-30T01:50:00Z"
    assert rig.guard.resumable_at() == (caught.value.resets_at if window else None)


@pytest.mark.parametrize(
    ("rate", "reason"),
    [
        ({"status": "rejected"}, "rejected"),
        ({"five_hour": 0.85}, "utilization 0.85 >= 0.80 (five_hour)"),
        ({"seven_day": 0.9}, "utilization 0.90 >= 0.85 (seven_day)"),
        ({"overage_status": "allowed"}, "overage is available"),
        ({"using_overage": True}, "overage"),
    ],
)
def test_warning_signs_finish_the_call_then_stop_the_next(tmp_path, rate, reason):
    rig = Rig(tmp_path, [{"events": ev.success({"verdict": "x"}, **rate)}])
    assert rig.llm.complete(call()).data == {"verdict": "x"}  # the answer is kept
    assert reason in rig.heartbeat["stopped"]
    with pytest.raises(UsageLimitReached, match=reason.split()[0]):
        rig.llm.complete(call())
    assert len(rig.invocations) == 1


def test_an_allowed_warning_is_recorded_and_the_run_carries_on(tmp_path):
    warned = {"events": ev.success({"verdict": "x"}, status="allowed_warning", seven_day=0.63)}
    rig = Rig(tmp_path, [warned, warned])
    assert rig.llm.complete(call()).data == {"verdict": "x"}
    assert rig.llm.complete(call()).data == {"verdict": "x"}  # the warning didn't stop the next call
    beat = rig.heartbeat
    assert beat["warning"] is True and beat["rate_limit_status"] == "allowed_warning" and beat["stopped"] is None
    assert beat["utilization"] == pytest.approx(0.63) and len(rig.invocations) == 2


def test_a_call_without_a_usage_signal_fails_closed(tmp_path):
    rig = Rig(tmp_path, [{"events": [ev.init(), ev.result({"verdict": "x"})]}])
    rig.llm.complete(call())
    assert "no plan-usage signal" in rig.heartbeat["stopped"]
    with pytest.raises(UsageLimitReached):
        rig.llm.complete(call())


def test_a_usage_credit_fallback_stops_the_run(tmp_path):
    events = [ev.init(), ev.rate_limit(), ev.result({"verdict": "x"}, fallback_credit={"used": True})]
    rig = Rig(tmp_path, [{"events": events}])
    rig.llm.complete(call())
    assert "usage-credit" in rig.heartbeat["stopped"]


def test_network_failures_stop_without_retrying(tmp_path):
    offline = "API Error: Connection error. (ENOTFOUND api.anthropic.com)"
    rig = Rig(tmp_path, [{"events": [], "exit": 1, "stderr": offline}])
    with pytest.raises(BackendUnavailable, match="network"):
        rig.llm.complete(call())
    assert len(rig.invocations) == 1
    assert rig.heartbeat["stopped"].startswith("network unavailable")


def test_transient_failures_are_retried_with_backoff(tmp_path):
    overloaded = [ev.init(), ev.result(text="API Error: 529 overloaded_error", is_error=True, api_error_status=529)]
    rig = Rig(tmp_path, [{"events": overloaded}, {"events": ev.success({"verdict": "ok"})}])
    assert rig.llm.complete(call()).data == {"verdict": "ok"}
    assert len(rig.invocations) == 2 and rig.guard.calls == 2 and rig.heartbeat["stopped"] is None


def test_persistent_transient_failures_fail_the_call_not_the_run(tmp_path):
    overloaded = [ev.init(), ev.result(text="API Error: 500", is_error=True, api_error_status=500)]
    rig = Rig(tmp_path, [{"events": overloaded}], max_retries=1)
    with pytest.raises(LLMError, match="after 2 attempts"):
        rig.llm.complete(call())
    assert rig.heartbeat["stopped"] is None


def test_the_call_cap_stops_before_spawning(tmp_path):
    rig = Rig(tmp_path, [{"events": ev.success({"verdict": "x"})}], cap=1)
    rig.llm.complete(call())
    with pytest.raises(CallCapReached):
        rig.llm.complete(call())
    assert len(rig.invocations) == 1 and "call cap" in rig.heartbeat["stopped"]


def test_a_missing_cli_is_unavailable(tmp_path):
    guard = PlanGuard(cap=5, stop_at_utilization=0.8, stop_at_weekly_utilization=0.85, require_signal=True)
    llm = ClaudeCodeLLM(ClaudeCodeOptions(binary=str(tmp_path / "nope")), guard, environ={"PATH": "/usr/bin"})
    with pytest.raises(BackendUnavailable, match="not found"):
        llm.complete(call())


def test_config_dir_mode_uses_a_fresh_config_dir_and_the_token(tmp_path):
    environ = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-test", "CLAUDE_CONFIG_DIR": "/Users/x/.claude"}
    rig = Rig(tmp_path, [{"events": ev.success({"verdict": "x"})}], isolation="config_dir", environ=environ)
    rig.llm.complete(call())
    env = rig.invocations[0]["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
    assert env["CLAUDE_CONFIG_DIR"] != "/Users/x/.claude" and Path(env["CLAUDE_CONFIG_DIR"]).name == "config"
    assert "--safe-mode" in rig.invocations[0]["argv"]


def test_config_dir_mode_without_a_token_is_unavailable(tmp_path):
    rig = Rig(tmp_path, [{"events": ev.success({"verdict": "x"})}], isolation="config_dir")
    with pytest.raises(BackendUnavailable, match="setup-token"):
        rig.llm.complete(call())
    assert rig.invocations == []


FIVE_HOUR_RESET, WEEKLY_RESET = "2026-09-30T01:50:00+00:00", "2026-10-06T01:00:00+00:00"  # `claude_events.rate_limit`


def plan(info: dict) -> PlanReading:
    return read_plan(info, stop_at_utilization=0.8, stop_at_weekly_utilization=0.85)


def rate_info(status: str = "allowed", *, windows: dict[str, float] | None = None, **rate) -> dict:
    """A `rate_limit_info`; `windows` adds or replaces `unifiedWindows` entries (they reset with the weekly window)."""
    info = ev.rate_limit(status, **rate)["rate_limit_info"]
    for name, used in (windows or {}).items():
        info["unifiedWindows"][name] = {"utilization": used, "resetsAt": 1791248400}
    return info


def test_read_plan_reports_the_fullest_window():
    reading = plan(rate_info(five_hour=0.3, seven_day=0.9))
    assert reading.utilization == 0.9 and reading.stop_reason == "plan utilization 0.90 >= 0.85 (seven_day)"
    assert reading.resets_at == WEEKLY_RESET  # the weekly window's reset, the one that matters
    assert reading.window_utilizations == {"five_hour": 0.3, "seven_day": 0.9} and not reading.permanent
    assert plan(rate_info()).stop_reason is None


@pytest.mark.parametrize(
    ("info", "stopped_by"),
    [
        (rate_info(five_hour=0.79, seven_day=0.84), None),
        (rate_info(five_hour=0.8), "plan utilization 0.80 >= 0.80 (five_hour)"),
        (rate_info(five_hour=0.3, seven_day=0.82), None),  # the 5-hour threshold doesn't apply to a weekly window
        (rate_info(seven_day=0.85), "plan utilization 0.85 >= 0.85 (seven_day)"),
        (rate_info(windows={"seven_day_opus": 0.84}), None),  # model-specific weekly windows: the weekly threshold
        (rate_info(windows={"seven_day_opus": 0.86}), "plan utilization 0.86 >= 0.85 (seven_day_opus)"),
        (rate_info(windows={"seven_day_sonnet": 0.9, "seven_day": 0.86}),
         "plan utilization 0.86 >= 0.85 (seven_day); plan utilization 0.90 >= 0.85 (seven_day_sonnet)"),
        (rate_info(windows={"one_hour": 0.81}), "plan utilization 0.81 >= 0.80 (one_hour)"),  # others: the default
    ],
)  # fmt: skip
def test_each_window_has_its_own_threshold(info, stopped_by):
    reading = plan(info)
    assert reading.stop_reason == stopped_by and not reading.permanent


def test_the_top_level_utilization_belongs_to_its_rate_limit_type():
    info = {"status": "allowed", "rateLimitType": "seven_day_sonnet", "utilization": 0.84, "resetsAt": 1791248400}
    assert plan(info).stop_reason is None and plan(info).window_utilizations == {"seven_day_sonnet": 0.84}
    info["rateLimitType"] = "five_hour"
    assert plan(info).stop_reason == "plan utilization 0.84 >= 0.80 (five_hour)"


@pytest.mark.parametrize(
    ("info", "resets_at"),
    [
        (rate_info(five_hour=0.85, seven_day=0.2), FIVE_HOUR_RESET),
        (rate_info(five_hour=0.2, seven_day=0.9), WEEKLY_RESET),
        (rate_info(five_hour=0.9, seven_day=0.86), WEEKLY_RESET),  # both: the later reset
        (rate_info(five_hour=0.1, seven_day=0.82), FIVE_HOUR_RESET),  # none: the reported limit's
    ],
)  # fmt: skip
def test_the_reset_is_the_one_of_the_window_that_tripped(info, resets_at):
    assert plan(info).resets_at == resets_at


def test_an_allowed_warning_is_advisory_and_rejected_is_a_window_stop():
    warned = plan(rate_info("allowed_warning", seven_day=0.63))
    assert warned.stop_reason is None and warned.warning and warned.status == "allowed_warning"
    rejected = plan(rate_info("rejected"))
    assert rejected.stop_reason == "plan status 'rejected' (five_hour)" and not rejected.permanent
    assert rejected.resets_at == FIVE_HOUR_RESET and not rejected.warning


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (rate_info("allowed_warning", overage_status="allowed"), "overage is available"),
        (rate_info("allowed_warning", seven_day=0.9, using_overage=True), "overage"),
        (rate_info("blocked"), "plan status 'blocked'"),
        ({"rateLimitType": "seven_day"}, "plan status None"),
    ],
)  # fmt: skip
def test_overage_and_unknown_statuses_stay_permanent_stops(info, reason):
    reading = plan(info)
    assert reason in reading.stop_reason and reading.permanent


def test_parse_stream_skips_noise():
    out = parse_stream("not json\n" + "\n".join(json.dumps(e) for e in ev.success({"a": 1})) + "\n{broken")
    assert out.init["subtype"] == "init" and out.result["structured_output"] == {"a": 1}
    assert out.rate_limit["status"] == "allowed" and out.assistant_model == "claude-fable-5-1"
