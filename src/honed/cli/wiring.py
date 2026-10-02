"""Composition root, part 1: builds adapters from settings and wires them into services. Only the `cli` package
wires adapters into services."""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from honed import config
from honed.adapters import policy_dir
from honed.adapters.anthropic_llm import AnthropicLLM, AnthropicOptions, ModelPrice
from honed.adapters.blobs import BlobStore
from honed.adapters.budget_guard import BudgetGuard
from honed.adapters.cached_llm import CachedLLM
from honed.adapters.call_store import SqliteCallStore
from honed.adapters.claude_code_llm import ClaudeCodeLLM, ClaudeCodeOptions
from honed.adapters.gh_client import GhClient, GhOptions
from honed.adapters.git_reader import GitReader
from honed.adapters.github import GitHubCodeHost
from honed.adapters.limited_llm import ConcurrencyLimit
from honed.adapters.local_llm import LocalLLM, LocalOptions, MLXGenerator, model_dir
from honed.adapters.pack_reader import PackReader
from honed.adapters.plan_guard import PlanGuard, StatusFile
from honed.adapters.replay_llm import ReplayLLM
from honed.adapters.routing_llm import ReplayFirstLLM, RoutedLLM
from honed.adapters.sqlite_store import SqliteStore
from honed.core.filters import is_excluded
from honed.core.policy import ModelChoice, Policy, PolicyRules
from honed.core.types import ContextPack, Corpus, PRKey, PRSource
from honed.learn.evidence import SerialReader
from honed.learn.jobs import ResetWait, run_jobs
from honed.learn.judge import PROMPTS, JudgeOptions, LLMJudge
from honed.learn.label import STOP_ON
from honed.ports.code_reader import CodeReader
from honed.ports.llm import LLM, BackendUnavailable, CallBatch
from honed.ports.store import Store
from honed.review.pipeline import ReviewOptions, ReviewPipeline


class BackendRefused(RuntimeError):
    """The configured LLM backend refused to start (the anthropic backend without `ANTHROPIC_API_KEY`)."""


def open_store(settings: config.Settings) -> SqliteStore:
    return SqliteStore(settings.paths.sqlite, BlobStore(settings.paths.blobs))


def github_host(settings: config.Settings) -> GitHubCodeHost:
    gh = settings.github
    client = GhClient(
        GhOptions(
            max_retries=gh.max_retries,
            backoff_s=gh.backoff_s,
            min_remaining=gh.graphql_min_remaining,
            point_budget=gh.run_point_budget,
            min_page=gh.min_page,
            timeout_s=gh.request_timeout_s,
        )
    )
    return GitHubCodeHost(
        client, threads_page=gh.threads_page, comments_page=gh.comments_page, reactions_page=gh.reactions_page
    )


def git_reader(settings: config.Settings, repo: str) -> GitReader:
    return GitReader(f"https://github.com/{repo}.git", settings.paths.clones / (repo.replace("/", "__") + ".git"))


def local_llm(settings: config.Settings, *, max_calls: int | None = None) -> LocalLLM:
    """The local model (`[llm.local]`), loaded on its first call."""
    local = settings.llm.local
    options = LocalOptions(
        repo=local.model, max_context=local.max_context, max_tokens=local.max_tokens, temperature=local.temperature,
        sample_temperature=local.sample_temperature, top_p=local.top_p, parse_retries=local.parse_retries,
        call_cap=max_calls,
    )  # fmt: skip
    return LocalLLM(MLXGenerator(model_dir(settings.paths.models, local.model)), options)


class LLMWiring:
    """The LLM stack for one run: backend, plan guard (claude_code only) and the call cache and ledger.

    `llm` answers the review stages and the proposer, `judge_llm` the judge's evaluation verdicts (match and
    validity), `gold_llm` the judge's gold labels (the per-round gold issues). Offline (`offline = true`), every stage
    goes to the local model, the judge's verdicts too: the local model judges both sides of every comparison, and
    cached Fable verdicts are never mixed in (a strict judge on one side and a lenient one on the other would bias
    it); its own earlier answers are replayed from the cache. Gold labels are the dataset, shared by both sides, so
    they come from Fable's cache first, then the local model. Runs are stored under backend `local`, and promotions
    from them are provisional (ARCHITECTURE.md section 8)."""

    def __init__(self, settings: config.Settings, *, max_calls: int | None = None) -> None:
        self.run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self.calls = SqliteCallStore(settings.paths.sqlite)
        self.guard: PlanGuard | None = None  # claude_code
        self.budget: BudgetGuard | None = None  # anthropic
        self.batch: CallBatch | None = None  # anthropic with use_batches
        self.local: LocalLLM | None = None
        self.replay_first: ReplayFirstLLM | None = None
        self.offline = settings.offline
        if settings.offline:
            self.local = local_llm(settings, max_calls=max_calls)
            routed = RoutedLLM(CachedLLM(self.local, self.calls, run_id=self.run_id), self.local.model)
            replay = CachedLLM(ReplayLLM(self.calls), self.calls, run_id=self.run_id)
            # The judge's verdicts: the local model's own cached answers (under its model name), then the local model.
            self.replay_first = ReplayFirstLLM(RoutedLLM(replay, self.local.model), routed)
            self.backend = "local"
            self.judge_model = self.local.model
            self.llm: LLM = routed
            self.judge_llm: LLM = self.replay_first
            self.gold_llm: LLM = ReplayFirstLLM(replay, routed)  # Fable's cached gold labels, then the local model
            return
        cap = min(settings.llm.run_call_cap, max_calls) if max_calls is not None else settings.llm.run_call_cap
        if settings.llm.backend == "replay":
            backend: LLM = ReplayLLM(self.calls)
        elif settings.llm.backend == "anthropic":
            backend = self._anthropic(settings, cap)
        else:
            cc = settings.llm.claude_code
            self.guard = PlanGuard(
                cap=cap, stop_at_utilization=cc.stop_at_utilization,
                stop_at_weekly_utilization=cc.stop_at_weekly_utilization, require_signal=cc.require_usage_signal,
                write_status=StatusFile(settings.paths.llm_status).write,
            )  # fmt: skip
            self.guard.start()
            backend = ClaudeCodeLLM(
                ClaudeCodeOptions(
                    binary=cc.binary, isolation=cc.isolation, oauth_token_env=cc.oauth_token_env,
                    keychain_service=cc.keychain_service, timeout_s=cc.timeout_s, max_retries=cc.max_retries,
                    backoff_s=cc.backoff_s, cli_max_retries=cc.cli_max_retries,
                ),
                self.guard,
            )  # fmt: skip
        live = self.guard is not None or self.budget is not None
        if live:
            backend = ConcurrencyLimit(backend, settings.llm.concurrency)
        self.backend = settings.llm.backend if live else "replay"
        self.judge_model = settings.models.judge.online
        self.llm = CachedLLM(backend, self.calls, run_id=self.run_id)
        self.judge_llm = self.llm
        self.gold_llm = self.llm

    def _anthropic(self, settings: config.Settings, cap: int) -> LLM:
        """The API-key backend; refuses to start (`BackendUnavailable`) without `ANTHROPIC_API_KEY`."""
        a = settings.llm.anthropic
        options = AnthropicOptions(
            prices={model: ModelPrice(p.input, p.output, p.cache_write, p.cache_read) for model, p in a.prices.items()},
            fallbacks=a.fallbacks, batch_discount=a.batch_discount, timeout_s=a.timeout_s, max_retries=a.max_retries,
            batch_poll_s=a.batch_poll_s, batch_max_wait_s=a.batch_max_wait_s,
        )  # fmt: skip
        budget = BudgetGuard(
            budget_usd=a.run_budget_usd, cap=cap, write_status=StatusFile(settings.paths.llm_status).write
        )
        try:
            backend = AnthropicLLM.from_environment(options, budget)
        except BackendUnavailable as error:
            raise BackendRefused(str(error)) from None
        self.budget = budget
        self.budget.start()
        if a.use_batches:
            self.batch = backend
        return backend

    @property
    def live_calls(self) -> int:
        """Live model calls made so far in this run (any backend)."""
        if self.guard is not None:
            return self.guard.calls
        if self.budget is not None:
            return self.budget.calls
        return self.local.stats.calls if self.local is not None else 0

    @property
    def provisional(self) -> bool:
        """Results from this stack are provisional until an online run re-scores them with Fable."""
        return self.offline

    def local_judge_llm(self, settings: config.Settings, *, max_calls: int | None = None) -> LLM:
        """The local model as a judge (the cross-family audit), cached under its own model name."""
        if self.local is None:
            self.local = local_llm(settings, max_calls=max_calls)
        return RoutedLLM(CachedLLM(self.local, self.calls, run_id=self.run_id), self.local.model)

    def close(self) -> None:
        self.calls.close()

    def local_line(self) -> str:
        if self.local is None:
            return ""
        s = self.local.stats
        rate = s.parse_failure_rate
        speeds = (f"; prefill {sum(s.prompt_tps) / len(s.prompt_tps):.0f} tok/s, decode "
                  f"{sum(s.generation_tps) / len(s.generation_tps):.1f} tok/s" if s.prompt_tps else "")  # fmt: skip
        return (f"local {self.local.model}: {s.calls} calls, {s.structured} structured, {s.first_try_failures} "
                f"invalid first answers, {s.failures} still invalid after retries"
                + (f" (parse failure rate {rate:.3f})" if rate is not None else "") + speeds)  # fmt: skip

    def judge_id(self, judge: LLMJudge) -> str:
        """The evaluation judge's identity for stored runs: the model that answers and the judge's fingerprint."""
        return f"{self.judge_model}:{judge.fingerprint}"

    def status_line(self) -> str:
        if self.offline:
            replayed = self.replay_first.replayed if self.replay_first else 0
            return (f"run {self.run_id}: offline; local judge answers replayed from cache: {replayed}; "
                    + self.local_line())  # fmt: skip
        if self.budget is not None:
            b = self.budget
            return (f"run {self.run_id}: anthropic, {b.calls}/{b.cap} live calls, ${b.spent_usd:.2f} of "
                    f"${b.budget_usd:.2f} spent" + (f"; STOPPED: {b.stopped}" if b.stopped else ""))  # fmt: skip
        if self.guard is None:
            return f"run {self.run_id}: replay (no live calls)" + (f"; {self.local_line()}" if self.local else "")
        g, last = self.guard, self.guard.last
        plan = (f"plan status {last.status}, utilization {last.utilization}, overage {last.overage}"
                if last else "no plan reading")  # fmt: skip
        return f"run {self.run_id}: {g.calls}/{g.cap} live calls; {plan}" + (
            f"; STOPPED: {g.stopped}" + (f" (resets {g.resets_at})" if g.resets_at else "") if g.stopped else ""
        )


def reset_wait(settings: config.Settings, wiring: LLMWiring, args: argparse.Namespace) -> ResetWait | None:
    """`--wait-for-reset`: wait out plan-window stops, up to `[llm.claude_code] max_wait_s` (claude_code only: the
    anthropic backend has no plan window, and replay makes no live calls)."""
    if not args.wait_for_reset or wiring.guard is None:
        return None
    cc = settings.llm.claude_code
    return ResetWait(wiring.guard, margin_s=cc.reset_wait_margin_s, heartbeat_s=cc.wait_heartbeat_s,
                     max_wait_s=cc.max_wait_s)  # fmt: skip


def make_judge(settings: config.Settings, llm: LLM) -> LLMJudge:
    prompts = {name: (settings.paths.yardstick_prompts / f"{name}.md").read_text() for name in PROMPTS}
    judge, label = settings.models.judge, settings.label
    options = JudgeOptions(
        model=judge.online, effort=judge.effort, max_tokens=label.max_tokens, gold_max_tokens=label.gold_max_tokens,
        categories=label.categories,
    )  # fmt: skip
    return LLMJudge(llm, prompts, options)


def readers(settings: config.Settings) -> Callable[[str], CodeReader]:
    """One serialized git reader per repo (clones in data/clones; offline, only what they already hold)."""
    readers: dict[str, CodeReader] = {}

    def reader_for(repo: str) -> CodeReader:
        if repo not in readers:
            readers[repo] = SerialReader(git_reader(settings, repo))
        return readers[repo]

    return reader_for


def label_keys(store: SqliteStore, settings: config.Settings, repos: Sequence[str] | None,
                limit: int | None) -> list[PRKey]:  # fmt: skip
    wanted = {r.lower() for r in repos} if repos else None
    facts = {PRKey(f.repo, f.number): f for f in store.pr_facts()}
    by_repo: dict[str, list[PRKey]] = {}
    for key in store.pr_keys():
        fact = facts.get(key)
        if fact is None or fact.corpus is Corpus.APPROVAL_ONLY or fact.source is PRSource.BENCHMARK:
            continue  # a benchmark PR's gold is the benchmark's; it is never labeled
        if is_excluded(key.repo, settings.corpus.excluded):
            continue
        if wanted is None or key.repo.lower() in wanted:
            by_repo.setdefault(key.repo, []).append(key)
    return [key for keys in by_repo.values() for key in (keys[:limit] if limit else keys)]


def require_online(settings: config.Settings, command: str) -> None:
    if settings.offline:
        raise SystemExit(f"honed {command} needs the network; set offline = false in {settings.source}")


def policy_rules(settings: config.Settings) -> PolicyRules:
    models = settings.models
    return PolicyRules(
        high_risk=frozenset(settings.safety.high_risk_categories),
        categories=settings.label.categories,
        languages=tuple(settings.metrics.language_weights),
        models={stage: ModelChoice(m.online, m.allowed, m.tunable)
                for stage, m in (("intent", models.intent), ("finders", models.finders),
                                 ("verifier", models.verifier))},
    )  # fmt: skip


def load_policy(settings: config.Settings, directory: Path | None) -> Policy:
    return policy_dir.load(directory or settings.paths.policy, policy_rules(settings))


def pipeline(settings: config.Settings, llm: LLM, policy: Policy, store: Store | None, *,
             focus: str | None = None) -> ReviewPipeline:  # fmt: skip
    """The review pipeline; `focus` names a policy prompt that replaces the code lenses (the self-review's
    `policy_change`)."""
    runner = functools.partial(run_jobs, stop_on=STOP_ON)
    options = ReviewOptions(categories=settings.label.categories,
                            high_risk=frozenset(settings.safety.high_risk_categories), focus=focus)  # fmt: skip
    return ReviewPipeline(llm, policy, runner, options, store)


def pack_reader(store: SqliteStore) -> Callable[[ContextPack], CodeReader]:
    def make(pack: ContextPack) -> CodeReader:
        return PackReader(pack, store.get_blob)

    return make
