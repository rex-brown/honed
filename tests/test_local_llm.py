"""The `local` backend on a fake generator: structured output by prompting, validation and retries, usage counted from
the tokenizer, shadow cost 0; the JSON answer helpers; the offline routing decorators. One live test loads the real
model (`uv run pytest -m live`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from honed.adapters import json_answer
from honed.adapters.cached_llm import CachedLLM
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.local_llm import Generated, LocalLLM, LocalOptions, MLXGenerator, model_dir, model_id
from honed.adapters.replay_llm import ReplayLLM
from honed.adapters.routing_llm import ReplayFirstLLM, RoutedLLM
from honed.ports.llm import CallCapReached, LLMCall, LLMError, LLMResult, ReplayMiss
from reviewkit import ROOT, SETTINGS

SCHEMA = {"type": "object", "properties": {
    "verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string", "enum": ["F1", "F2"]}, "valid": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["id", "valid", "reason"], "additionalProperties": False}}},
    "required": ["verdicts"], "additionalProperties": False}  # fmt: skip
GOOD = '{"verdicts": [{"id": "F1", "valid": true, "reason": "reaches items[0]"}]}'


class FakeGenerator:
    """Answers in turn; a prompt token per word of the messages, an output token per word of the answer."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    def count(self, messages) -> int:
        return sum(len(m["content"].split()) for m in messages)

    def generate(self, messages, *, max_tokens, temperature, top_p, seed) -> Generated:
        self.calls.append({"messages": [dict(m) for m in messages], "max_tokens": max_tokens,
                           "temperature": temperature, "seed": seed})  # fmt: skip
        text = self.answers.pop(0)
        return Generated(text, self.count(messages), len(text.split()), 0.5, 120.0, 15.0)


def local(answers: list[str], **overrides) -> tuple[LocalLLM, FakeGenerator]:
    generator = FakeGenerator(answers)
    values = dict(repo="org/model-4bit", max_context=4000, max_tokens=500, temperature=0.0, sample_temperature=0.7,
                  top_p=0.95, parse_retries=2)  # fmt: skip
    values.update(overrides)
    return LocalLLM(generator, LocalOptions(**values)), generator


def call(**kw) -> LLMCall:
    values = dict(model="claude-fable-5-1", system="Judge the comments.", schema=SCHEMA, max_tokens=800,
                  user="<untrusted_pr_data>x</untrusted_pr_data>", stage="validity")  # fmt: skip
    values.update(kw)
    return LLMCall(**values)


def test_a_structured_call_gets_the_schema_and_parses_the_answer():
    answer = f"<think>\n</think>\n```json\n{GOOD}\n```"
    llm, gen = local([answer])
    result = llm.complete(call())
    assert result.data == json.loads(GOOD) and result.model == "local:org/model-4bit" == llm.model
    assert result.usage.cost_usd == 0.0 and result.usage.output_tokens == len(answer.split())
    assert result.usage.input_tokens == gen.count(gen.calls[0]["messages"]) and result.usage.duration_s >= 0
    system = gen.calls[0]["messages"][0]["content"]
    assert system.startswith("Judge the comments.") and "## Answer format" in system and '"verdicts"' in system
    assert gen.calls[0]["messages"][1]["content"] == call().user
    assert gen.calls[0]["max_tokens"] == 500 and gen.calls[0]["temperature"] == 0.0 and gen.calls[0]["seed"] is None
    assert llm.stats.structured == 1 and llm.stats.first_try_failures == 0 and llm.stats.parse_failure_rate == 0.0


def test_an_invalid_answer_is_sent_back_with_its_errors():
    echo = json.dumps(SCHEMA)  # small models tend to echo the schema back
    llm, gen = local([echo, GOOD])
    result = llm.complete(call())
    assert result.data == json.loads(GOOD)
    retry = gen.calls[1]["messages"]
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    assert retry[2]["content"] == echo and "not valid" in retry[3]["content"] and "unexpected" in retry[3]["content"]
    assert gen.calls[1]["seed"] == 1  # a retry is a fresh, seeded sample
    assert result.usage.input_tokens == sum(gen.count(c["messages"]) for c in gen.calls)
    assert (llm.stats.first_try_failures, llm.stats.retries, llm.stats.failures) == (1, 1, 0)


def test_after_every_retry_the_call_fails_and_is_counted():
    llm, _ = local(["no json here", '{"verdicts": [{"id": "F9"}]}', "[]"])
    with pytest.raises(LLMError, match="no valid JSON after 3 attempts"):
        llm.complete(call())
    assert llm.stats.failures == 1 and llm.stats.parse_failure_rate == 1.0 and llm.stats.errors


def test_samples_context_and_the_call_cap():
    llm, gen = local([GOOD, "free text"], call_cap=2)
    llm.complete(call(sample=2))
    assert gen.calls[0]["temperature"] == 0.7 and gen.calls[0]["seed"] == 2000
    assert llm.complete(call(schema=None)).text == "free text" and llm.stats.structured == 1
    with pytest.raises(CallCapReached):
        llm.complete(call())
    tight, _ = local([GOOD], max_context=510)
    with pytest.raises(LLMError, match="over the local context"):
        tight.complete(call())


def test_json_answer_helpers():
    assert json_answer.extract('Sure: {"a": 1} and more') == {"a": 1}
    assert json_answer.extract("<think>{nope}</think>[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        json_answer.extract("nothing")
    assert json_answer.errors({"verdicts": [{"id": "F3", "valid": "yes", "reason": "r", "x": 1}]}, SCHEMA) == [
        "$.verdicts[0]: unexpected `x`", "$.verdicts[0].id: 'F3' is not one of ['F1', 'F2']",
        "$.verdicts[0].valid: expected boolean, got str",
    ]  # fmt: skip
    assert json_answer.errors({"n": True}, {"type": "object", "properties": {"n": {"type": "integer"}}}) == [
        "$.n: expected integer, got bool"
    ]
    assert json_answer.skeleton(SCHEMA) == {"verdicts": [{"id": "F1", "valid": True, "reason": "..."}]}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[LLMCall] = []

    def complete(self, c: LLMCall) -> LLMResult:
        self.calls.append(c)
        return LLMResult(text="", data={"ok": c.model}, model=c.model)


def test_routing_rewrites_the_model_before_the_cache(tmp_path):
    store = SqliteCallStore(tmp_path / "calls.sqlite")
    inner = Recorder()
    routed = RoutedLLM(CachedLLM(inner, store, run_id="r"), "local:org/model")
    routed.complete(call(effort="high"))
    assert inner.calls[0].model == "local:org/model" and inner.calls[0].effort is None
    assert store.get(call(effort="high").key) is None  # never cached as a Claude answer
    assert store.get(call(model="local:org/model", effort=None).key) is not None

    fable = call(user="cached question")
    store.put(fable.key, fable, LLMResult(text="", data={"from": "fable"}, model="claude-fable-5-1"))
    judge = ReplayFirstLLM(CachedLLM(ReplayLLM(store), store, run_id="r"), routed)
    assert judge.complete(fable).data == {"from": "fable"}
    assert judge.complete(call(user="new question")).data == {"ok": "local:org/model"}
    assert (judge.replayed, judge.fell_back) == (1, 1)
    assert not [e for e in store.ledger() if not e.ok]  # a replay miss isn't a failed call
    with pytest.raises(ReplayMiss):
        CachedLLM(ReplayLLM(store), store, run_id="r").complete(call(user="never asked"))
    store.close()


def test_model_names_and_paths():
    assert model_id("mlx-community/X-4bit") == "local:mlx-community/X-4bit"
    assert model_dir(Path("/m"), "mlx-community/X-4bit") == Path("/m/mlx-community__X-4bit")


@pytest.mark.live
def test_the_local_model_answers_a_structured_question():
    local_settings = SETTINGS.llm.local
    path = model_dir(ROOT / "data-dev" / "models", local_settings.model)
    pytest.importorskip("mlx_lm")
    if not (path / "config.json").is_file():
        pytest.skip(f"no local model at {path} (`honed --data-dir data-dev fetch-local-model`)")
    generator = MLXGenerator(path)
    llm = LocalLLM(generator, LocalOptions(repo=local_settings.model, max_context=local_settings.max_context,
                                           max_tokens=600, temperature=0.0, sample_temperature=0.7, top_p=0.95,
                                           parse_retries=2))  # fmt: skip
    user = ("<untrusted_pr_data>\n=== F1 ===\nReview comment:\n`items[0]` raises IndexError when `items` is empty.\n"
            "Code:\n  1 | def first(items):\n> 2 |     return items[0]\n</untrusted_pr_data>")  # fmt: skip
    result = llm.complete(call(user=user, schema={**SCHEMA}))
    assert result.data["verdicts"] and result.data["verdicts"][0]["id"] == "F1"
    assert generator.load_seconds is not None and llm.stats.prompt_tps and llm.stats.generation_tps


def test_offline_routes_every_stage_and_both_sides_judge_to_local(tmp_path):
    from dataclasses import replace

    from honed import config
    from honed.cli.wiring import LLMWiring

    settings = replace(config.relocate_data(SETTINGS, tmp_path), offline=True)
    wiring = LLMWiring(settings, max_calls=5)
    try:
        assert wiring.backend == "local" and wiring.provisional and wiring.guard is None
        assert isinstance(wiring.llm, RoutedLLM) and wiring.llm.model == f"local:{SETTINGS.llm.local.model}"
        assert isinstance(wiring.judge_llm, ReplayFirstLLM)
        assert "offline" in wiring.status_line() and wiring.local is not None
        assert wiring.judge_model == f"local:{SETTINGS.llm.local.model}"

        # One judge for both sides offline (ARCHITECTURE.md section 8): a verdict Fable gave is never served to an
        # offline comparison; the local model's own earlier answer is. Gold labels still come from Fable's cache.
        wiring.local._generator = FakeGenerator([GOOD])
        fable = call(user="<untrusted_pr_data>an evaluated round</untrusted_pr_data>")
        fable_answer = LLMResult(text="", data={"verdicts": [{"id": "F1", "valid": False, "reason": "fable"}]},
                                 model="claude-fable-5-1")  # fmt: skip
        wiring.calls.put(fable.key, fable, fable_answer)
        assert wiring.judge_llm.complete(fable).data["verdicts"][0]["reason"] == "reaches items[0]"  # the local one
        assert wiring.judge_llm.complete(fable).data["verdicts"][0]["reason"] == "reaches items[0]"  # from cache
        assert (wiring.replay_first.replayed, wiring.replay_first.fell_back) == (1, 1)
        assert wiring.local.stats.calls == 1
        assert wiring.gold_llm.complete(fable).data == fable_answer.data  # the dataset's gold labels: Fable's
    finally:
        wiring.close()
    online = LLMWiring(replace(config.relocate_data(SETTINGS, tmp_path / "online")), max_calls=1)
    try:
        assert online.backend == "claude_code" and not online.provisional and online.judge_llm is online.llm
        assert online.gold_llm is online.llm and online.judge_model == SETTINGS.models.judge.online
    finally:
        online.close()
