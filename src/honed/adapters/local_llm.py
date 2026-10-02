"""The `local` LLM backend: an open model run in-process with MLX-LM on Apple Silicon (ARCHITECTURE.md sections 8
and 9). Offline, it answers every review stage; it is also the cross-family judge (METRICS.md section 5).

- The model loads once, on the first call, and calls run one at a time (MLX generation is not thread-safe).
- A call is the system prompt and the user content as two chat messages. The model's reasoning mode is off.
- Structured output: no constrained decoding, so the system prompt gets the JSON schema and an instruction to answer
  with one matching object; the answer is parsed and validated (`json_answer`), and an invalid one is sent back with
  the errors, up to `parse_retries` times, before the call fails. `stats` counts first-try and final parse failures.
- Usage is counted with the model's tokenizer: input tokens are the prompt's, output tokens the answer's, over every
  attempt. Shadow cost is 0 (nothing is billed); latency is the wall time of the call.
- Sample 0 decodes greedily (temperature `temperature`); a sample k > 0 uses `sample_temperature` with seed k, an
  independent but reproducible sample. The call's effort is ignored.

`Generator` is the part that touches MLX, so tests use a fake.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from honed.adapters import json_answer
from honed.ports.llm import CallCapReached, LLMCall, LLMError, LLMResult, Usage

log = logging.getLogger(__name__)

LOCAL_PREFIX = "local:"


def model_id(repo: str) -> str:
    """The model name local answers are recorded and cached under: `local:<repo id>`, never a Claude model's name."""
    return LOCAL_PREFIX + repo


def model_dir(models: Path, repo: str) -> Path:
    """Where a downloaded model lives: `<models>/<owner>__<name>`."""
    return models / repo.replace("/", "__")


@dataclass(frozen=True)
class Generated:
    text: str
    prompt_tokens: int
    output_tokens: int
    seconds: float
    prompt_tps: float = 0.0  # prefill speed, tokens per second
    generation_tps: float = 0.0  # decode speed


class Generator(Protocol):
    def generate(self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float, top_p: float,
                 seed: int | None) -> Generated: ...  # fmt: skip

    def count(self, messages: Sequence[Mapping[str, str]]) -> int:
        """Prompt tokens of `messages` with the chat template applied."""
        ...


@dataclass(frozen=True)
class LocalOptions:
    repo: str  # the Hugging Face repo id of the MLX model
    max_context: int  # prompt plus answer tokens; a longer prompt fails the call
    max_tokens: int  # answer tokens per attempt, at most (a call's own max_tokens hint is capped by this)
    temperature: float  # sample 0
    sample_temperature: float  # samples > 0
    top_p: float
    parse_retries: int  # corrective re-asks after an answer that is not the requested JSON
    call_cap: int | None = None  # calls this run may make (`--max-calls`)


@dataclass
class LocalStats:
    calls: int = 0
    structured: int = 0  # calls with a JSON schema
    first_try_failures: int = 0  # structured calls whose first answer did not parse or validate
    retries: int = 0
    failures: int = 0  # structured calls still invalid after every retry
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    prompt_tps: list[float] = field(default_factory=list)
    generation_tps: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def parse_failure_rate(self) -> float | None:
        return self.failures / self.structured if self.structured else None

    @property
    def first_try_failure_rate(self) -> float | None:
        return self.first_try_failures / self.structured if self.structured else None

    @property
    def output_tokens_per_s(self) -> float | None:
        return self.output_tokens / self.seconds if self.seconds else None


class LocalLLM:
    def __init__(self, generator: Generator, options: LocalOptions) -> None:
        self._generator = generator
        self._o = options
        self._lock = threading.Lock()
        self.stats = LocalStats()

    @property
    def model(self) -> str:
        return model_id(self._o.repo)

    def complete(self, call: LLMCall) -> LLMResult:
        with self._lock:
            if self._o.call_cap is not None and self.stats.calls >= self._o.call_cap:
                raise CallCapReached(f"call cap reached ({self._o.call_cap} local calls this run)")
            self.stats.calls += 1
            started = time.monotonic()
            result = self._answer(call)
            seconds = time.monotonic() - started
            self.stats.seconds += seconds
            return LLMResult(text=result[0], data=result[1], usage=Usage(result[2], result[3], duration_s=seconds),
                             model=self.model, stop_reason="end_turn")  # fmt: skip

    def _answer(self, call: LLMCall) -> tuple[str, Any, int, int]:
        """(text, data, prompt tokens, output tokens), retrying invalid structured answers."""
        system = call.system if call.schema is None else call.system + "\n\n" + json_answer.instructions(call.schema)
        messages: list[dict[str, str]] = [{"role": "system", "content": system}, {"role": "user", "content": call.user}]
        sampled = call.sample > 0
        temperature = self._o.sample_temperature if sampled else self._o.temperature
        prompt_total = output_total = 0
        attempts = 1 + (self._o.parse_retries if call.schema is not None else 0)
        problems: list[str] = []
        for attempt in range(attempts):
            budget = min(call.max_tokens, self._o.max_tokens)
            prompt = self._generator.count(messages)
            if prompt + budget > self._o.max_context:
                raise LLMError(f"{call.stage or 'call'}: prompt of {prompt} tokens plus {budget} answer tokens is "
                               f"over the local context of {self._o.max_context}")  # fmt: skip
            seed = call.sample * 1000 + attempt if sampled or attempt else None
            out = self._generator.generate(messages, max_tokens=budget, temperature=temperature, top_p=self._o.top_p,
                                           seed=seed)  # fmt: skip
            prompt_total += out.prompt_tokens
            output_total += out.output_tokens
            self.stats.prompt_tokens += out.prompt_tokens
            self.stats.output_tokens += out.output_tokens
            if out.prompt_tps:
                self.stats.prompt_tps.append(out.prompt_tps)
                self.stats.generation_tps.append(out.generation_tps)
            if call.schema is None:
                return out.text, None, prompt_total, output_total
            if attempt == 0:
                self.stats.structured += 1
            try:
                data = json_answer.extract(out.text)
                problems = json_answer.errors(data, call.schema)
            except ValueError as error:
                data, problems = None, [str(error)]
            if not problems:
                return out.text, data, prompt_total, output_total
            if attempt == 0:
                self.stats.first_try_failures += 1
            if attempt + 1 < attempts:
                self.stats.retries += 1
                log.info("local %s: invalid answer (%s): %r; asking again", call.stage, "; ".join(problems[:3]),
                         out.text[:300])  # fmt: skip
                messages += [
                    {"role": "assistant", "content": out.text},
                    {"role": "user", "content": "That answer is not valid: " + "; ".join(problems[:8])
                     + ". Answer again with only the JSON object that matches the schema."},
                ]  # fmt: skip
        self.stats.failures += 1
        message = f"{call.stage or 'call'}: no valid JSON after {attempts} attempts: {'; '.join(problems[:3])}"
        self.stats.errors.append(message)
        raise LLMError(message)


class MLXGenerator:
    """MLX-LM in-process. Imports `mlx_lm` (the `local` extra) only when the model first loads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._model: Any = None
        self._tokenizer: Any = None
        self.load_seconds: float | None = None
        self.peak_memory_gb: float | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        if not (self._path / "config.json").is_file():
            raise LLMError(f"no local model at {self._path}: run `honed fetch-local-model`")
        try:
            import mlx_lm
        except ImportError as error:
            raise LLMError("the local backend needs the `local` extra: `uv sync --extra local`") from error
        started = time.monotonic()
        self._model, self._tokenizer = mlx_lm.load(str(self._path))
        self.load_seconds = time.monotonic() - started
        log.info("loaded %s in %.1fs", self._path.name, self.load_seconds)

    def _prompt(self, messages: Sequence[Mapping[str, str]]) -> list[int]:
        self._load()
        try:
            return list(self._tokenizer.apply_chat_template(list(messages), add_generation_prompt=True,
                                                            enable_thinking=False))  # fmt: skip
        except TypeError:  # a template without the reasoning switch
            return list(self._tokenizer.apply_chat_template(list(messages), add_generation_prompt=True))

    def count(self, messages: Sequence[Mapping[str, str]]) -> int:
        return len(self._prompt(messages))

    def generate(self, messages: Sequence[Mapping[str, str]], *, max_tokens: int, temperature: float, top_p: float,
                 seed: int | None) -> Generated:  # fmt: skip
        import mlx.core as mx
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        prompt = self._prompt(messages)
        if seed is not None:
            mx.random.seed(seed)
        sampler = make_sampler(temp=temperature, top_p=top_p if temperature > 0 else 0.0)
        started = time.monotonic()
        pieces, generated, last = [], 0, None
        for response in stream_generate(self._model, self._tokenizer, prompt, max_tokens=max_tokens, sampler=sampler):
            pieces.append(response.text)
            generated = response.generation_tokens
            last = response
        if last is not None and getattr(last, "peak_memory", None):
            self.peak_memory_gb = max(self.peak_memory_gb or 0.0, float(last.peak_memory))
        prefill = float(getattr(last, "prompt_tps", 0.0) or 0.0)
        decode = float(getattr(last, "generation_tps", 0.0) or 0.0)
        return Generated("".join(pieces), len(prompt), generated, time.monotonic() - started, prefill, decode)


def download(repo: str, target: Path) -> Path:
    """Fetch a model's files from Hugging Face into `target` (the `local` extra's `huggingface_hub`)."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise LLMError("downloading a local model needs the `local` extra: `uv sync --extra local`") from error
    target.mkdir(parents=True, exist_ok=True)
    return Path(snapshot_download(repo, local_dir=str(target)))
