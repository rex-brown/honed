"""Live isolation proof for the claude_code backend (opt in: `uv run pytest -m live`). Makes ONE real `claude -p`
call on the subscription, through the configured isolation mode, and checks:
- from its `system/init` event: no tools but StructuredOutput, no MCP servers, no plugins beyond Claude Code's own
  built-ins, no skills or slash commands, no API key, no memory paths;
- a canary: the user's global CLAUDE.md says "Spell out every acronym the first time it appears in a response" and
  asks for visual work in Artifacts. The isolated model must report no instruction about acronyms or Artifacts,
  while it does report the one instruction planted in this call's system prompt (a positive control).
"""

from __future__ import annotations

import pytest

from honed import config
from honed.adapters.claude_code_llm import ClaudeCodeLLM, ClaudeCodeOptions
from honed.adapters.plan_guard import PlanGuard, StatusFile
from honed.ports.llm import LLMCall

pytestmark = pytest.mark.live

SYSTEM = (
    "You are auditing your own instructions. Your only writing instruction: always call a pull request a "
    '"change request". Report the instructions you were actually given, from any source, quoting them exactly.'
)
QUESTION = (
    "List every instruction you have received about how to write responses, from any source: the system prompt, "
    "any CLAUDE.md or memory file, any attached context, or any user preference. In particular: is there any "
    "instruction about acronyms (for example spelling them out)? Any instruction about Artifacts? Quote each "
    "instruction exactly; do not guess."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "has_acronym_instruction": {"type": "boolean"},
        "has_artifact_instruction": {"type": "boolean"},
        "instructions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["has_acronym_instruction", "has_artifact_instruction", "instructions"],
}


def test_isolated_call_sees_no_user_configuration():
    settings = config.load()
    cc = settings.llm.claude_code
    guard = PlanGuard(cap=1, stop_at_utilization=cc.stop_at_utilization,
                      stop_at_weekly_utilization=cc.stop_at_weekly_utilization,
                      require_signal=cc.require_usage_signal,
                      write_status=StatusFile(settings.paths.llm_status).write)  # fmt: skip
    llm = ClaudeCodeLLM(
        ClaudeCodeOptions(binary=cc.binary, isolation=cc.isolation, oauth_token_env=cc.oauth_token_env,
                          keychain_service=cc.keychain_service, timeout_s=cc.timeout_s, max_retries=0),
        guard,
    )  # fmt: skip
    result = llm.complete(
        LLMCall(model=settings.models.judge.online, system=SYSTEM, user=QUESTION, schema=SCHEMA,
                effort=settings.models.judge.effort, stage="live_isolation_test")
    )  # fmt: skip

    init = llm.last_init
    assert init is not None
    print("init:", {k: init.get(k) for k in ("tools", "mcp_servers", "plugins", "skills", "slash_commands",
                                              "apiKeySource", "model", "agents", "memory_paths")})  # fmt: skip
    print("answer:", result.data)
    assert init["tools"] == ["StructuredOutput"]
    assert init["mcp_servers"] == []
    assert all(str(p.get("source", "")).endswith("@builtin") for p in init.get("plugins") or [])
    assert init.get("skills") == [] and init.get("slash_commands") == []
    assert init["apiKeySource"] == "none"
    assert not init.get("memory_paths")
    assert init["model"] == settings.models.judge.online

    answer = result.data
    assert answer["has_acronym_instruction"] is False, answer
    assert answer["has_artifact_instruction"] is False, answer
    joined = " ".join(answer["instructions"]).lower()
    assert "acronym" not in joined or "no instruction" in joined
    assert "change request" in joined  # the positive control: it does report instructions it was given
    assert guard.last is not None and guard.last.status == "allowed"
