"""The `anthropic` backend against a fake SDK client: API-key only, request shape (cache breakpoints, structured
output, effort, fallbacks), cost from usage and the price table, the dollar budget, error mapping, and batch mode
through the job runner and the call cache. No network, no model calls."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from honed import cli, config
from honed.adapters.anthropic_llm import (
    FALLBACK_BETA,
    AnthropicLLM,
    AnthropicOptions,
    ModelPrice,
    Prices,
    api_schema,
    message_params,
)
from honed.adapters.budget_guard import BudgetGuard
from honed.adapters.cached_llm import CachedLLM
from honed.adapters.call_store import SqliteCallStore
from honed.cli.wiring import BackendRefused, LLMWiring
from honed.learn.jobs import Job, run_jobs
from honed.learn.label import STOP_ON
from honed.ports.llm import (
    BackendUnavailable,
    BudgetExhausted,
    CallCapReached,
    LLMCall,
    LLMError,
    UsageLimitReached,
)
from reviewkit import ROOT

PRICES = {
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 5.0, 0.20),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 6.25, 0.50),
    "claude-fable-5-1": ModelPrice(10.0, 50.0, 12.5, 0.25),
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
          "additionalProperties": False}  # fmt: skip


def usage(i: int = 100, o: int = 50, r: int = 1000, w: int = 200, iterations: Any = None) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=r, cache_creation_input_tokens=w,
                           iterations=iterations)  # fmt: skip


def message(text: str = '{"ok": true}', *, stop: str = "end_turn", model: str = "claude-opus-5-5",
            use: SimpleNamespace | None = None, category: str | None = None) -> SimpleNamespace:  # fmt: skip
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop, model=model, usage=use or usage(),
        stop_details=SimpleNamespace(category=category) if stop == "refusal" else None,
    )  # fmt: skip


class Stream:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def __enter__(self) -> Stream:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self.answer


class FakeClient:
    """`client.beta.messages.stream`, `client.messages.stream` and `client.messages.batches`, scripted."""

    def __init__(self, answers: list[Any] | None = None, batch_results: dict[str, Any] | None = None,
                 polls: int = 2) -> None:  # fmt: skip
        self.answers = list(answers or [])
        self.sent: list[dict[str, Any]] = []
        self.batch_requests: list[list[dict[str, Any]]] = []
        self.batch_results = batch_results or {}  # custom_id -> result, else succeeded with a default message
        self.polls = polls
        self.retrieved = 0
        outer = self

        class Messages:
            def stream(self, **params: Any) -> Stream:
                outer.sent.append(params)
                answer = outer.answers.pop(0) if outer.answers else message()
                if isinstance(answer, Exception):
                    raise answer
                return Stream(answer)

        class Batches:
            def create(self, *, requests: list[dict[str, Any]]) -> Any:
                outer.batch_requests.append(requests)
                outer.retrieved = 0
                counts = SimpleNamespace(processing=len(requests), succeeded=0)
                name = f"batch_{len(outer.batch_requests)}"
                return SimpleNamespace(id=name, processing_status="in_progress", request_counts=counts)

            def retrieve(self, batch_id: str) -> Any:
                outer.retrieved += 1
                status = "ended" if outer.retrieved >= outer.polls else "in_progress"
                return SimpleNamespace(id=batch_id, processing_status=status, request_counts=None)

            def results(self, batch_id: str) -> Any:
                for request in outer.batch_requests[-1]:
                    key = request["custom_id"]
                    result = outer.batch_results.get(key, SimpleNamespace(type="succeeded", message=message()))
                    yield SimpleNamespace(custom_id=key, result=result)

            def cancel(self, batch_id: str) -> None:
                pass

        self.messages = Messages()
        self.messages.batches = Batches()  # type: ignore[attr-defined]
        self.beta = SimpleNamespace(messages=Messages())


def backend(client: FakeClient, *, budget: float = 10.0, cap: int = 100, fallbacks: bool = True,
            beats: list | None = None) -> AnthropicLLM:  # fmt: skip
    guard = BudgetGuard(budget_usd=budget, cap=cap, write_status=beats.append if beats is not None else None)
    options = AnthropicOptions(prices=PRICES, fallbacks=fallbacks, batch_poll_s=0)
    return AnthropicLLM(client, options, guard, sleep=lambda s: None)


def call(user: str = "the diff", **kw: Any) -> LLMCall:
    return LLMCall(model=kw.pop("model", "claude-opus-5-5"), system="You review code.", user=user, **kw)


def status_error(cls: type, code: int, text: str = "nope") -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(text, response=httpx2.Response(code, request=request), body=None)


# ---- API key only -------------------------------------------------------------------------------------------


def test_the_backend_refuses_to_start_without_an_api_key():
    guard = BudgetGuard(budget_usd=1, cap=1)
    options = AnthropicOptions(prices=PRICES)
    with pytest.raises(BackendUnavailable, match="needs an API key in \\$ANTHROPIC_API_KEY"):
        AnthropicLLM.from_environment(options, guard, environ={"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-x"})
    with pytest.raises(BackendUnavailable, match="OAuth token"):
        AnthropicLLM.from_environment(options, guard, environ={"ANTHROPIC_API_KEY": "sk-ant-oat01-x"})
    built = AnthropicLLM.from_environment(options, guard, environ={"ANTHROPIC_API_KEY": "sk-ant-api03-test"})
    assert built._client.api_key == "sk-ant-api03-test" and built._client.auth_token is None  # never an OAuth token


def _anthropic_config(tmp_path, **anthropic_overrides: str):
    text = (ROOT / "honed.toml").read_text().replace('backend = "claude_code"', 'backend = "anthropic"', 1)
    for key, value in anthropic_overrides.items():
        text = text.replace(f"\n{key} = ", f"\n{key} = {value}  # was ", 1)
    path = tmp_path / "honed.toml"
    path.write_text(text)
    return path


def test_the_cli_refuses_to_start_the_anthropic_backend_without_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = _anthropic_config(tmp_path)
    settings = config.relocate_data(config.load(path), tmp_path / "data")
    with pytest.raises(BackendRefused):
        LLMWiring(settings)
    (tmp_path / "change.diff").write_text("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    code = cli.main(["--config", str(path), "--data-dir", str(tmp_path / "data"), "review",
                     str(tmp_path / "change.diff"), "--policy", str(ROOT / "policy")])  # fmt: skip
    assert code == 2 and "ANTHROPIC_API_KEY" in capsys.readouterr().err


# ---- one call -----------------------------------------------------------------------------------------------


def test_the_request_has_cache_breakpoints_structured_output_and_effort():
    c = call("PREFIX:the shared context|member lenses", schema=SCHEMA, effort="medium", cache_prefix=26)
    params = message_params(c)
    assert params["system"] == [{"type": "text", "text": "You review code.", "cache_control": {"type": "ephemeral"}}]
    first, rest = params["messages"][0]["content"]
    assert first == {"type": "text", "text": "PREFIX:the shared context|", "cache_control": {"type": "ephemeral"}}
    assert rest == {"type": "text", "text": "member lenses"}
    assert params["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert "thinking" not in params and "temperature" not in params  # adaptive thinking, the model's default
    plain = message_params(call("no prefix"))
    assert plain["messages"][0]["content"] == [{"type": "text", "text": "no prefix"}] and "output_config" not in plain


def test_unsupported_schema_keywords_are_dropped_but_property_names_kept():
    schema = {"type": "object", "properties": {
        "pattern": {"type": "string", "maxLength": 9}, "n": {"type": "integer", "minimum": 1},
        "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "many": {"type": "array", "items": {"type": "string"}, "minItems": 2}}}  # fmt: skip
    out = api_schema(schema)
    assert out["properties"]["pattern"] == {"type": "string"} and out["properties"]["n"] == {"type": "integer"}
    assert out["properties"]["ids"]["minItems"] == 1 and "minItems" not in out["properties"]["many"]


def test_a_call_streams_with_fallbacks_and_is_priced_from_usage():
    client = FakeClient([message()])
    beats: list = []
    llm = backend(client, beats=beats)
    result = llm.complete(call(schema=SCHEMA, stage="judge", pr="o/r#1"))
    assert result.data == {"ok": True} and result.text == '{"ok": true}' and result.model == "claude-opus-5-5"
    (sent,) = client.sent
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    # 100 in x $4 + 50 out x $20 + 1000 cache reads x $0.20 + 200 cache writes x $5, per million tokens
    assert result.usage.cost_usd == pytest.approx(0.0026)
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.cache_write_tokens) == (
        100,
        1000,
        200,
    )
    assert llm.guard.spent_usd == pytest.approx(0.0026) and llm.guard.reserved_usd == 0
    assert beats[-1]["spent_usd"] == pytest.approx(0.0026) and beats[-1]["budget_usd"] == 10.0
    assert beats[-1]["backend"] == "anthropic" and beats[-1]["calls"] == 1


def test_without_fallbacks_the_plain_stream_is_used():
    client = FakeClient()
    backend(client, fallbacks=False).complete(call())
    assert "fallbacks" not in client.sent[0] and "betas" not in client.sent[0]


def test_a_fallback_answer_is_priced_at_each_models_rates():
    none = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    iterations = [
        SimpleNamespace(type="message", model="claude-fable-5-1", input_tokens=1000, output_tokens=0, **none),
        SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=1000, output_tokens=100, **none),
    ]
    answer = message(model="claude-opus-4-8", use=usage(1000, 100, 0, 0, iterations))
    result = backend(FakeClient([answer])).complete(call(model="claude-fable-5-1"))
    assert result.usage.cost_usd == pytest.approx((1000 * 10 + 1000 * 5 + 100 * 25) / 1e6)
    assert result.model == "claude-opus-4-8"


def test_a_refusal_every_fallback_refused_fails_the_call_and_is_still_charged():
    llm = backend(FakeClient([message("", stop="refusal", category="cyber")]))
    with pytest.raises(LLMError, match="refused \\(cyber\\)"):
        llm.complete(call())
    assert llm.guard.spent_usd > 0 and llm.guard.reserved_usd == 0


def test_an_answer_that_is_not_the_requested_json_fails_the_call():
    with pytest.raises(LLMError, match="not JSON \\(stop reason max_tokens\\)"):
        backend(FakeClient([message('{"ok": tr', stop="max_tokens")])).complete(call(schema=SCHEMA))


@pytest.mark.parametrize(
    ("error", "raised", "stops"),
    [
        (status_error(anthropic.RateLimitError, 429), UsageLimitReached, True),
        (status_error(anthropic.AuthenticationError, 401), BackendUnavailable, True),
        (status_error(anthropic.NotFoundError, 404), BackendUnavailable, True),
        (status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), BackendUnavailable, True),
        (status_error(anthropic.BadRequestError, 400, "messages: bad"), LLMError, False),
        (status_error(anthropic.InternalServerError, 500), LLMError, False),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), BackendUnavailable, True),
    ],
)
def test_sdk_errors_left_after_its_retries_map_to_the_ports_errors(error, raised, stops):
    llm = backend(FakeClient([error]))
    with pytest.raises(raised):
        llm.complete(call())
    assert (llm.guard.stopped is not None) is stops and llm.guard.reserved_usd == 0
    if stops:  # the run is over: the next call raises before anything is sent
        with pytest.raises(type(llm.guard._stop)):
            llm.complete(call("another"))


def test_the_budget_refuses_a_call_whose_worst_case_does_not_fit():
    client = FakeClient()
    llm = backend(client, budget=0.05)
    worst = Prices(PRICES).worst_case(call(max_tokens=8192))
    assert worst > 0.05  # 8192 output tokens at $20 per million
    with pytest.raises(BudgetExhausted, match="run_budget_usd"):
        llm.complete(call(max_tokens=8192))
    assert client.sent == []
    small = backend(FakeClient(), budget=0.05)
    small.complete(call(max_tokens=1000))
    assert small.guard.spent_usd == pytest.approx(0.0026)


def test_the_call_cap_stops_the_run():
    llm = backend(FakeClient(), cap=1)
    llm.complete(call("one"))
    with pytest.raises(CallCapReached):
        llm.complete(call("two"))


def test_an_unknown_model_is_charged_the_tables_highest_prices():
    prices = Prices(PRICES)
    assert prices("claude-mystery") == ModelPrice(10.0, 50.0, 12.5, 0.50)


# ---- batch mode -----------------------------------------------------------------------------------------------


class Rig:
    def __init__(self, tmp_path, client: FakeClient, **kw: Any) -> None:
        self.client = client
        self.llm = backend(client, **kw)
        self.calls = SqliteCallStore(tmp_path / "calls.sqlite")
        self.cached = CachedLLM(self.llm, self.calls, run_id="run")
        self.answers: dict[str, Any] = {}

    def job(self, name: str, *users: str) -> Job:
        """A job that asks its calls in turn (each answer needed before the next question)."""

        def run() -> None:
            self.answers[name] = [self.cached.complete(call(u, schema=SCHEMA, stage=name)).data for u in users]

        return Job(name, "judge", run, done=lambda: name in self.answers)


def test_batch_mode_sends_one_batch_per_step_and_caches_the_answers(tmp_path):
    rig = Rig(tmp_path, FakeClient())
    jobs = [rig.job("a", "q1"), rig.job("b", "q2", "q3"), rig.job("c", "q1")]  # c asks a's question
    report = run_jobs(jobs, concurrency=4, stop_on=STOP_ON, batch=rig.llm)
    assert (report.completed, report.failed, report.stopped, report.batches) == (3, {}, None, 2)
    first, second = rig.client.batch_requests
    assert len(first) == 2 and len(second) == 1  # q1 once for a and c; b's second step in its own batch
    assert {r["custom_id"] for r in first} == {call("q1", schema=SCHEMA).key.digest,
                                                call("q2", schema=SCHEMA).key.digest}  # fmt: skip
    assert all("fallbacks" not in r["params"] and "betas" not in r["params"] for r in first)
    assert rig.client.sent == []  # nothing went out one by one
    assert rig.answers == {"a": [{"ok": True}], "b": [{"ok": True}, {"ok": True}], "c": [{"ok": True}]}
    ledger = [e for e in rig.calls.ledger() if not e.cached]
    assert sum(e.cost_usd for e in ledger) == pytest.approx(3 * 0.0026 * 0.5)  # half price, q1 paid once
    assert rig.llm.guard.spent_usd == pytest.approx(3 * 0.0026 * 0.5) and rig.llm.guard.calls == 3
    again = run_jobs([rig.job("d", "q3")], concurrency=1, stop_on=STOP_ON, batch=rig.llm)
    assert again.batches == 0 and again.completed == 1  # served from the call cache
    rig.calls.close()


def test_batch_items_fail_one_by_one_and_refusals_are_asked_again_with_fallbacks(tmp_path):
    bad, refused = call("bad", schema=SCHEMA).key.digest, call("touchy", schema=SCHEMA).key.digest
    errored = SimpleNamespace(type="errored", error=SimpleNamespace(error=SimpleNamespace(
        type="invalid_request_error", message="schema too complex")))  # fmt: skip
    results = {bad: errored, refused: SimpleNamespace(type="succeeded", message=message("", stop="refusal"))}
    rig = Rig(tmp_path, FakeClient(batch_results=results))
    jobs = [rig.job("ok", "fine"), rig.job("bad", "bad"), rig.job("touchy", "touchy")]
    report = run_jobs(jobs, concurrency=3, stop_on=STOP_ON, batch=rig.llm)
    assert report.completed == 2 and list(report.failed) == ["bad"] and "invalid_request_error" in report.failed["bad"]
    (rescue,) = rig.client.sent  # the refused item, asked again on its own with server-side fallbacks
    assert rescue["fallbacks"] == "default"
    rig.calls.close()


def test_a_nested_run_inside_a_batched_job_defers_the_job_around_it(tmp_path):
    rig = Rig(tmp_path, FakeClient())

    def panel() -> None:  # like the finder panel: an inner run with no batch of its own
        inner = run_jobs([rig.job("m1", "x"), rig.job("m2", "y")], concurrency=2, stop_on=STOP_ON)
        if inner.stop_error is not None:
            raise inner.stop_error
        rig.answers["outer"] = True

    report = run_jobs([Job("outer", "review", panel)], concurrency=1, stop_on=STOP_ON, batch=rig.llm)
    assert (report.completed, report.batches, report.failed) == (1, 1, {})
    assert len(rig.client.batch_requests[0]) == 2  # both members queued in the same pass
    rig.calls.close()


def test_the_call_cap_cuts_a_batch_and_then_stops_the_run(tmp_path):
    rig = Rig(tmp_path, FakeClient(), cap=2)
    report = run_jobs([rig.job(n, n) for n in ("p", "q", "r")], concurrency=3, stop_on=STOP_ON, batch=rig.llm)
    assert report.completed == 2 and report.not_run == 1 and "CallCapReached" in report.stopped
    assert [len(b) for b in rig.client.batch_requests] == [2]
    rig.calls.close()


def test_a_batch_whose_worst_case_does_not_fit_is_cut_to_what_does(tmp_path):
    one = Prices(PRICES).worst_case(call("p", schema=SCHEMA), 0.5)
    rig = Rig(tmp_path, FakeClient(), budget=one * 1.5)
    report = run_jobs([rig.job(n, n) for n in ("p", "q")], concurrency=2, stop_on=STOP_ON, batch=rig.llm)
    assert [len(b) for b in rig.client.batch_requests] == [1, 1]  # the second fits once the first is settled
    assert report.completed == 2 and rig.llm.guard.spent_usd <= one * 1.5
    rig.calls.close()


def test_the_heartbeat_shows_the_batch_while_it_runs(tmp_path):
    beats: list = []
    rig = Rig(tmp_path, FakeClient(polls=3), beats=beats)
    run_jobs([rig.job("a", "q")], concurrency=1, stop_on=STOP_ON, batch=rig.llm)
    shown = [b["batch"] for b in beats if b["batch"]]
    assert shown and shown[0]["id"] == "batch_1" and shown[0]["status"] == "in_progress"
    assert beats[-1]["batch"] is None and json.dumps(beats[-1])
    rig.calls.close()


def test_an_interrupt_while_a_batch_runs_cancels_it(tmp_path):
    client = FakeClient(polls=5)
    cancelled: list[str] = []
    client.messages.batches.cancel = cancelled.append  # type: ignore[attr-defined]
    rig = Rig(tmp_path, client)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    rig.llm._sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        run_jobs([rig.job("a", "q")], concurrency=1, stop_on=STOP_ON, batch=rig.llm)
    assert cancelled == ["batch_1"] and rig.llm.guard.reserved_usd == 0
    rig.calls.close()
