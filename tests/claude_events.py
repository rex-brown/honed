"""Stream-json events shaped like `claude -p --output-format stream-json --verbose` (Claude Code 2.1.285)."""

from __future__ import annotations

from typing import Any


def init(**overrides: Any) -> dict[str, Any]:
    event = {
        "type": "system", "subtype": "init", "cwd": "/tmp/x", "tools": ["StructuredOutput"], "mcp_servers": [],
        "model": "claude-fable-5-1", "permissionMode": "default", "slash_commands": [], "apiKeySource": "none",
        "claude_code_version": "2.1.285", "output_style": "default", "agents": ["Explore", "general-purpose", "Plan"],
        "skills": [], "plugins": [{"name": "cc-plugin-telemetry", "path": "builtin",
                                   "source": "cc-plugin-telemetry@builtin"}],
    }  # fmt: skip
    event.update(overrides)
    return event


def rate_limit(status: str = "allowed", *, five_hour: float = 0.07, seven_day: float = 0.15,
               overage_status: str = "rejected", using_overage: bool = False) -> dict[str, Any]:  # fmt: skip
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status, "resetsAt": 1790733000, "rateLimitType": "five_hour", "overageStatus": overage_status,
            "overageDisabledReason": "org_level_disabled_until", "isUsingOverage": using_overage,
            "unifiedWindows": {
                "five_hour": {"utilization": five_hour, "resetsAt": 1790733000},
                "seven_day": {"utilization": seven_day, "resetsAt": 1791248400},
            },
        },
    }  # fmt: skip


def assistant(model: str = "claude-fable-5-1") -> dict[str, Any]:
    return {"type": "assistant", "message": {"model": model, "role": "assistant", "content": []}}


def result(data: Any = None, *, text: str | None = None, subtype: str = "success", is_error: bool = False,
           api_error_status: int | None = None, fallback_credit: Any = None) -> dict[str, Any]:  # fmt: skip
    import json

    return {
        "type": "result", "subtype": subtype, "is_error": is_error, "duration_ms": 1234, "num_turns": 2,
        "result": text if text is not None else json.dumps(data), "stop_reason": "tool_use",
        "total_cost_usd": 0.0792, "api_error_status": api_error_status,
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1088, "cache_read_input_tokens": 50,
                  "output_tokens": 1149, "fallback_credit": fallback_credit},
        "modelUsage": {"claude-fable-5-1": {"costUSD": 0.0792, "costBasis": "list"}},
        **({"structured_output": data} if data is not None else {}),
    }  # fmt: skip


def success(data: Any = None, **rate: Any) -> list[dict[str, Any]]:
    return [init(), assistant(), rate_limit(**rate), result(data)]
