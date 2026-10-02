"""`honed review` on a diff file through the real claude_code backend, against the fake `claude` binary, with
`--data-dir`; and a review replayed from the call cache alone. No network."""

from __future__ import annotations

import json
import sys

import claude_events as ev
from honed import cli
from honed.adapters.cached_llm import CachedLLM
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.replay_llm import ReplayLLM
from reviewkit import ROOT, STORE, ScriptedLLM, finding, make_pipeline, reader, request

DIFF = """diff --git a/app/items.py b/app/items.py
--- a/app/items.py
+++ b/app/items.py
@@ -1,2 +1,3 @@
 def first(items):
-    return None
+    value = items[0]
+    return value
"""
BUG = {"path": "app/items.py", "start_line": 2, "end_line": 2, "severity": "important", "category": "correctness",
       "title": "`items[0]` raises IndexError when `items` is empty", "body": "Return None for an empty list.",
       "trace": "first([]) -> items[0]", "lessons": []}  # fmt: skip


def test_review_a_diff_file_through_the_claude_code_backend(tmp_path, monkeypatch, capsys):
    fake = ROOT / "tests" / "fake_claude.py"
    binary = tmp_path / "claude"
    binary.write_text(f'#!/bin/sh\nexec {sys.executable} {fake} "$@"\n')
    binary.chmod(0o755)
    runs = [
        {"events": ev.success({"intent": "Makes first() return the first item."})},
        {"events": ev.success({"findings": [BUG]})},
        {"events": ev.success({"findings": [BUG]})},
        {"events": ev.success({"verdicts": [{
            "id": "P1", "bucket": "act_on", "reason": "first([]) reaches items[0]", "evidence_level": 3,
            "checked": "line 2 indexes items[0] with no length check",
            "severity": "important", "confidence": 0.9, "duplicate_of": "", "title": BUG["title"],
            "body": BUG["body"], "lessons": []}]})},
    ]  # fmt: skip
    (tmp_path / "scenario.json").write_text(json.dumps({"runs": runs}))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(tmp_path / "scenario.json"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    config = tmp_path / "honed.toml"
    config.write_text((ROOT / "honed.toml").read_text().replace('binary = "claude"', f'binary = "{binary}"'))
    (tmp_path / "change.diff").write_text(DIFF)
    data = tmp_path / "copy"

    code = cli.main(["--config", str(config), "--data-dir", str(data), "review", str(tmp_path / "change.diff"),
                     "--policy", str(ROOT / "policy"), "--json", "--max-calls", "4"])  # fmt: skip
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    (posted,) = out["posted"]
    assert posted["raised_by"] == ["a", "b"] and posted["severity"] == "important" and posted["consensus"]
    assert "<!-- honed finding=" in posted["comment"] and out["policy"] in posted["comment"]
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    models = sorted(c["argv"][c["argv"].index("--model") + 1] for c in calls)
    assert models == ["claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5"]
    assert all("--safe-mode" in c["argv"] and "<untrusted_pr_data>" in c["stdin"] for c in calls)
    assert json.loads((data / "llm_status.json").read_text())["calls"] == 4  # the heartbeat under --data-dir
    assert (data / STORE).exists() and not (tmp_path / "data").exists()


def test_a_review_replays_from_the_call_cache(tmp_path):
    calls = SqliteCallStore(tmp_path / "calls.sqlite")
    live = ScriptedLLM({"a": [finding(title="`data['items'][0]` raises IndexError on an empty list")], "b": []})
    first = make_pipeline(CachedLLM(live, calls, run_id="live")).review(request(), reader())  # type: ignore[arg-type]
    again = make_pipeline(CachedLLM(ReplayLLM(calls), calls, run_id="replay")).review(request(), reader())  # type: ignore[arg-type]
    assert again.findings == first.findings and again.intent == first.intent
    assert {u.cached for u in again.usage} == {1} and again.cost_usd == first.cost_usd  # list-price cost kept
    calls.close()
