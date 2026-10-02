"""Loads `honed.toml` into typed, frozen settings. The only module that reads configuration."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from honed.core.filters import canonical_repo
from honed.core.scoring import ScoringParams
from honed.core.types import DateWindow, GoldProvenance, PackBudget, Severity

CONFIG_NAME = "honed.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PathSettings:
    root: Path  # the directory holding honed.toml
    data: Path
    sqlite: Path
    blobs: Path
    clones: Path
    cache: Path
    llm_status: Path  # heartbeat written after every live model call
    human_labels: Path  # the blind sample and people's labels files: part of the yardstick, committed
    human_labels_page: Path  # the labeling page (`human-labels serve`)
    reports: Path  # evaluation reports (JSON), one per run
    models: Path  # downloaded local models, one directory each
    benchmarks: Path  # downloaded public benchmarks (`import-benchmark --fetch`), one directory each
    yardstick_prompts: Path  # judge prompts (outside policy/)
    policy: Path  # the incumbent reviewer policy directory (the default `--policy`)
    removals_file: Path  # PRs and review comments every dataset export leaves out: part of the yardstick, committed
    exports: Path  # `honed export-gold` writes here


@dataclass(frozen=True)
class LanguageGroup:
    key: str
    label: str
    repos: tuple[str, ...]
    github_languages: tuple[str, ...]  # GitHub primary languages that place an ungrouped repo here


@dataclass(frozen=True)
class CorpusSettings:
    groups: tuple[LanguageGroup, ...]
    ai_feedback: tuple[str, ...]
    exclude: Mapping[str, tuple[str, ...]]  # reason -> repos

    @property
    def excluded(self) -> frozenset[str]:
        return frozenset(repo for repos in self.exclude.values() for repo in repos)

    @property
    def human_repos(self) -> tuple[str, ...]:
        return tuple(repo for group in self.groups for repo in group.repos)

    def language_of(self, repo: str) -> str | None:
        wanted = canonical_repo(repo)
        return next((g.key for g in self.groups for r in g.repos if canonical_repo(r) == wanted), None)

    @property
    def github_languages(self) -> Mapping[str, str]:
        """GitHub primary language -> group key."""
        return _frozen({lang: g.key for g in self.groups for lang in g.github_languages})

    @property
    def fallback_language(self) -> str:
        return next(g.key for g in self.groups if not g.github_languages)


@dataclass(frozen=True)
class MetricsSettings:
    beta: float
    valid_unlabeled_credit: float  # c_vu, for valid unlabeled findings claimed and judged Important, traced
    max_neutral_vu_per_round: int  # valid unlabeled Nits per PR-round that are neutral; each one beyond is an FP
    vu_min_evidence: int  # the verifier's evidence level a valid unlabeled Important needs to earn c_vu
    severity_weights: Mapping[Severity, float]
    language_weights: Mapping[str, float]
    gold_conf: Mapping[GoldProvenance, float]

    def scoring_params(self) -> ScoringParams:
        return ScoringParams(
            beta=self.beta,
            severity_weights=self.severity_weights,
            valid_unlabeled_credit=self.valid_unlabeled_credit,
            language_weights=self.language_weights,
            max_neutral_vu_per_round=self.max_neutral_vu_per_round,
            vu_min_evidence=self.vu_min_evidence,
        )


@dataclass(frozen=True)
class GateSettings:
    min_gain_floor: float
    min_gain_noise_multiplier: float
    language_tolerance: float
    important_recall_tolerance: float
    min_prs_per_language: int
    clean_pr_alarm_rise_pp: float
    cost_cap_usd: float  # mean list-price cost per review
    cost_growth_max: float
    latency_p90_max_s: float
    policy_max_lessons: int
    policy_max_prompt_tokens: int
    bootstrap_resamples: int
    ci_level: float
    well_formed_min: float  # findings that parse, and Important and Nit findings that anchor to a changed line
    removal_min_exposure: int  # PR-rounds on which removed content must have fired for the pure-removal exception
    screen_fraction: float  # of the gate split, replayed first; only a positive screen delta-S goes on


@dataclass(frozen=True)
class ProdSettings:
    rollback_window_findings: int
    rollback_window_days: int
    rollback_acted_drop_pp: float
    rollback_thumbsdown_rise_pp: float


@dataclass(frozen=True)
class JudgeSettings:
    min_accuracy: float
    min_kappa: float
    min_consistency: float
    matcher_min_f1: float
    cross_family_alarm_kappa: float
    local_min_human_accuracy: float  # the cross-family alarm counts only once the local judge is this accurate
    audit_items: int
    consistency_items: int
    consistency_repeats: int
    human_label_items: int  # review comments in the blind sample for human labeling


@dataclass(frozen=True)
class LessonSettings:
    min_prs: int
    min_authors: int
    recurring_min_fires: int
    strong_min_fires: int
    strong_min_repos: int


@dataclass(frozen=True)
class ReviewSettings:
    act_on_flag: int


@dataclass(frozen=True)
class SafetySettings:
    high_risk_categories: tuple[str, ...]


@dataclass(frozen=True)
class StageModel:
    online: str
    offline: str
    tunable: bool
    effort: str | None = None  # None: the backend's default
    allowed: tuple[str, ...] = ()  # models a policy may choose for a tunable stage (the online default always is)

    def permits(self, model: str) -> bool:
        return model == self.online or (self.tunable and model in self.allowed)


@dataclass(frozen=True)
class ModelSettings:
    intent: StageModel
    finders: StageModel
    verifier: StageModel
    proposer: StageModel
    judge: StageModel


@dataclass(frozen=True)
class HarvestSettings:
    target_prs: int
    ai_feedback_target_prs: int
    window: DateWindow
    window_slices: int
    search_page_size: int
    fixed_line_slack: int
    keep_clones: bool
    approval_only_share: float  # approval-only PRs kept, as a share of a repo's quota, on top of it
    bug_targeted_share: float  # share of a human-corpus repo's quota sampled from PRs where a human requested changes
    pack: PackBudget


@dataclass(frozen=True)
class ClaudeCodeSettings:
    binary: str
    isolation: str  # safe_mode | config_dir
    oauth_token_env: str
    keychain_service: str
    timeout_s: float
    max_retries: int
    backoff_s: float
    cli_max_retries: int
    stop_at_utilization: float  # the 5-hour window, and any window but the weekly ones
    stop_at_weekly_utilization: float  # every window whose name starts with `seven_day`
    require_usage_signal: bool
    reset_wait_margin_s: float  # --wait-for-reset: sleep until the plan window resets plus this
    wait_heartbeat_s: float  # ... rewriting the heartbeat this often
    max_wait_s: float = 21600.0  # ... but never longer than this: a reset further away ends the run instead


@dataclass(frozen=True)
class LocalModelSettings:
    """The local model (MLX-LM): the offline backend and the cross-family judge."""

    model: str  # Hugging Face repo id
    max_context: int
    max_tokens: int
    temperature: float
    sample_temperature: float
    top_p: float
    parse_retries: int


@dataclass(frozen=True)
class AnthropicPrice:
    """List prices, USD per million tokens; `cache_write` is the 5-minute-TTL write price."""

    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class AnthropicSettings:
    """The `anthropic` backend: the Messages API with an API key from `ANTHROPIC_API_KEY` (ARCHITECTURE.md 8)."""

    run_budget_usd: float  # the run's dollar ceiling: a call or batch whose worst case doesn't fit is refused
    use_batches: bool  # label, audit-judge and eval send their calls as Message Batches (half price)
    fallbacks: bool  # server-side refusal fallbacks on live calls
    batch_discount: float  # batches cost this fraction of list price
    timeout_s: float
    max_retries: int  # the SDK's retries of rate limits, overloads, server and connection errors
    batch_poll_s: float
    batch_max_wait_s: float  # a batch still running after this is cancelled
    prices: Mapping[str, AnthropicPrice]  # model id -> list prices


@dataclass(frozen=True)
class LLMSettings:
    backend: str  # claude_code | anthropic | replay
    concurrency: int
    run_call_cap: int
    claude_code: ClaudeCodeSettings
    local: LocalModelSettings
    anthropic: AnthropicSettings


@dataclass(frozen=True)
class LabelSettings:
    region_context_lines: int
    max_tokens: int
    gold_max_tokens: int
    categories: tuple[str, ...]


@dataclass(frozen=True)
class EvalSettings:
    """Replay evaluation (ARCHITECTURE.md section 6)."""

    max_rounds: int  # review rounds replayed per PR: the first, then the ones with the most human threads
    split_fractions: Mapping[str, float]  # train, validation, test: time-ordered by PR creation, per language
    dev_split: str  # a split holding every eligible PR, while the dataset is too small to split
    test_private_share: float  # of each language's test PRs held out as test-private by `splits --assign`
    include_escaped_defects: bool  # add mined escaped-defect gold issues to the replayed rounds
    code_excerpt_lines: int  # lines of code around each finding and gold issue shown to the judge
    bootstrap_seed: int
    max_findings_judged: int  # findings per PR-round sent to the match and validity questions


@dataclass(frozen=True)
class DefectSettings:
    """Escaped-defect mining (ARCHITECTURE.md section 5): later bug-fix PRs blamed back to corpus PRs."""

    after_days: int  # fix PRs created up to this many days after the corpus window
    fix_title: str  # regex over a fix PR's title
    fix_labels: tuple[str, ...]  # labels that mark a bug fix (case-insensitive)
    max_fix_lines: int  # a fix hunk removing or rewriting more old lines than this is a refactor, not a fix
    max_files: int  # a fix PR touching more files than this is skipped


@dataclass(frozen=True)
class ImproveSettings:
    """The improve loop (ARCHITECTURE.md section 7)."""

    candidates_per_round: int
    plateau_rejects: int
    target_s: float | None  # never relaxed by the loop
    generators: tuple[str, ...]  # the usual rotation of proposal generators
    feed_split: str  # the proposer mines this split; the gate decides on `improve --split`
    feed_sample_rounds: int  # PR-rounds of the feed split the incumbent is evaluated on for the proposer
    max_failure_cases: int
    proposer_max_tokens: int


_GENERATORS = ("lesson_miner", "reflective", "subtractive", "combine")


def _improve(raw: Mapping[str, Any]) -> ImproveSettings:
    values = _keys(raw, ImproveSettings)
    improve = ImproveSettings(**{**values, "generators": tuple(values["generators"])})
    unknown = sorted(set(improve.generators) - set(_GENERATORS))
    if unknown or not improve.generators:
        raise ValueError(f"improve.generators: unknown {unknown} (known: {_GENERATORS})")
    if improve.candidates_per_round < 1 or improve.plateau_rejects < 1 or improve.feed_sample_rounds < 1:
        raise ValueError("improve.candidates_per_round, plateau_rejects and feed_sample_rounds must be at least 1")
    return improve


@dataclass(frozen=True)
class PromoteSettings:
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]  # wins over allowed_paths

    def permits(self, path: str) -> bool:
        """Whether a candidate policy diff may touch `path` (relative to the project root)."""

        def under(prefixes: tuple[str, ...]) -> bool:
            return any(path == p.rstrip("/") or path.startswith(p if p.endswith("/") else p + "/") for p in prefixes)

        return under(self.allowed_paths) and not under(self.forbidden_paths)


@dataclass(frozen=True)
class DatasetSettings:
    """The published dataset (DATASET.md): what a bundle export is stamped with."""

    version: str  # the release a bundle export carries; a removal is followed by a point release
    annotations_license: str  # SPDX id of our annotations' license (DATA_LICENSE)


@dataclass(frozen=True)
class GitHubSettings:
    max_retries: int
    backoff_s: float
    graphql_min_remaining: int
    run_point_budget: int
    threads_page: int
    comments_page: int
    reactions_page: int
    min_page: int
    request_timeout_s: float


@dataclass(frozen=True)
class Settings:
    source: Path
    offline: bool
    paths: PathSettings
    corpus: CorpusSettings
    metrics: MetricsSettings
    gate: GateSettings
    prod: ProdSettings
    judge: JudgeSettings
    lessons: LessonSettings
    review: ReviewSettings
    safety: SafetySettings
    models: ModelSettings
    harvest: HarvestSettings
    github: GitHubSettings
    llm: LLMSettings
    label: LabelSettings
    eval: EvalSettings
    defects: DefectSettings
    improve: ImproveSettings
    promote: PromoteSettings
    dataset: DatasetSettings


def relocate_data(settings: Settings, data_dir: Path) -> Settings:
    """`--data-dir`: move every `[paths]` entry that lies under `paths.data` to the same place under `data_dir`
    (a runtime override, not configuration). Paths outside the data directory (the judge prompts, the policy) stay."""
    paths, root = settings.paths, settings.paths.data
    data_dir = data_dir.resolve()
    moved = {}
    for f in fields(PathSettings):
        value = getattr(paths, f.name)
        if f.name != "root" and value.is_relative_to(root):
            moved[f.name] = data_dir / value.relative_to(root)
    return replace(settings, paths=replace(paths, **moved))


def find_config(start: Path | None = None) -> Path:
    """`honed.toml` in `start` (default: the working directory) or its nearest ancestor that has one."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} in {here} or its parents")


def load(path: Path | None = None) -> Settings:
    source = (path or find_config()).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        return _settings(source, raw)
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError(f"{source}: {error}") from error


# ----------------------------------------------------------------------------------------------------------------


def _frozen(mapping: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(dict(mapping))


def _keys(section: Mapping[str, Any], fields: type) -> dict[str, Any]:
    """The section's values for the dataclass `fields`; a missing or unknown key is an error."""
    names = set(fields.__dataclass_fields__)
    unknown = set(section) - names
    if unknown:
        raise KeyError(f"unknown keys {sorted(unknown)} for {fields.__name__}")
    return {name: section[name] for name in names if name in section}


def _paths(root: Path, raw: Mapping[str, str]) -> PathSettings:
    return PathSettings(root=root, **{key: root / value for key, value in raw.items()})


def _corpus(raw: Mapping[str, Any]) -> CorpusSettings:
    groups = tuple(
        LanguageGroup(g["key"], g["label"], tuple(g["repos"]), tuple(g["github_languages"])) for g in raw["groups"]
    )
    if sum(not g.github_languages for g in groups) != 1:
        raise ValueError("exactly one corpus group must have an empty github_languages list (the fallback)")
    exclude = _frozen({reason: tuple(repos) for reason, repos in raw["exclude"].items()})
    corpus = CorpusSettings(groups=groups, ai_feedback=tuple(raw["ai_feedback"]["repos"]), exclude=exclude)
    excluded = {canonical_repo(r) for r in corpus.excluded}
    clashes = sorted(r for r in (*corpus.human_repos, *corpus.ai_feedback) if canonical_repo(r) in excluded)
    if clashes:
        raise ValueError(f"corpus repos on the exclude list: {clashes}")
    return corpus


def _metrics(raw: Mapping[str, Any], groups: tuple[LanguageGroup, ...]) -> MetricsSettings:
    severity = {Severity(k): float(v) for k, v in raw["severity_weights"].items()}
    if set(severity) != set(Severity):
        raise ValueError(f"metrics.severity_weights needs exactly {[s.value for s in Severity]}")
    conf = {GoldProvenance(k): float(v) for k, v in raw["gold_conf"].items()}
    if set(conf) != set(GoldProvenance):
        raise ValueError(f"metrics.gold_conf needs exactly {[p.value for p in GoldProvenance]}")
    languages = {k: float(v) for k, v in raw["language_weights"].items()}
    if set(languages) != {g.key for g in groups}:
        raise ValueError("metrics.language_weights keys must equal the corpus.groups keys")
    if abs(sum(languages.values()) - 1.0) > 1e-9:
        raise ValueError("metrics.language_weights must sum to 1")
    return MetricsSettings(
        beta=float(raw["beta"]),
        valid_unlabeled_credit=float(raw["valid_unlabeled_credit"]),
        max_neutral_vu_per_round=_count(raw["max_neutral_vu_per_round"], "metrics.max_neutral_vu_per_round"),
        vu_min_evidence=_count(raw["vu_min_evidence"], "metrics.vu_min_evidence"),
        severity_weights=_frozen(severity),
        language_weights=_frozen(languages),
        gold_conf=_frozen(conf),
    )


def _count(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _harvest(raw: Mapping[str, Any]) -> HarvestSettings:
    start, _, end = raw["window"].partition("..")
    values = _keys({k: v for k, v in raw.items() if k not in ("window", "pack")}, HarvestSettings)
    for share in ("approval_only_share", "bug_targeted_share"):
        if not 0 <= values[share] <= 1:
            raise ValueError(f"harvest.{share} must be in [0, 1]")
    return HarvestSettings(**values, window=DateWindow(start, end), pack=PackBudget(**_keys(raw["pack"], PackBudget)))


_BACKENDS = ("claude_code", "anthropic", "replay")
_ISOLATION = ("safe_mode", "config_dir")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _llm(raw: Mapping[str, Any]) -> LLMSettings:
    values = _keys({k: v for k, v in raw.items() if k not in ("claude_code", "local", "anthropic")}, LLMSettings)
    claude = ClaudeCodeSettings(**_keys(raw["claude_code"], ClaudeCodeSettings))
    local = LocalModelSettings(**_keys(raw["local"], LocalModelSettings))
    if local.max_tokens >= local.max_context or local.parse_retries < 0:
        raise ValueError("llm.local: max_tokens must be under max_context, and parse_retries >= 0")
    if values["backend"] not in _BACKENDS:
        raise ValueError(f"llm.backend must be one of {_BACKENDS}")
    if claude.isolation not in _ISOLATION:
        raise ValueError(f"llm.claude_code.isolation must be one of {_ISOLATION}")
    for threshold in ("stop_at_utilization", "stop_at_weekly_utilization"):
        if not 0 < getattr(claude, threshold) <= 1:
            raise ValueError(f"llm.claude_code.{threshold} must be in (0, 1]")
    if claude.reset_wait_margin_s < 0 or claude.wait_heartbeat_s <= 0 or claude.max_wait_s < 0:
        raise ValueError("llm.claude_code.reset_wait_margin_s and max_wait_s must be >= 0 and wait_heartbeat_s > 0")
    return LLMSettings(**values, claude_code=claude, local=local, anthropic=_anthropic(raw["anthropic"]))


def _anthropic(raw: Mapping[str, Any]) -> AnthropicSettings:
    values = _keys({k: v for k, v in raw.items() if k != "prices"}, AnthropicSettings)
    prices = _frozen({str(model): AnthropicPrice(**_keys(price, AnthropicPrice))
                      for model, price in raw["prices"].items()})  # fmt: skip
    settings = AnthropicSettings(**values, prices=prices)
    if settings.run_budget_usd <= 0 or not 0 < settings.batch_discount <= 1 or not prices:
        raise ValueError("llm.anthropic: run_budget_usd must be > 0, batch_discount in (0, 1], and prices not empty")
    return settings


def _label(raw: Mapping[str, Any], safety: SafetySettings) -> LabelSettings:
    label = LabelSettings(**{**_keys(raw, LabelSettings), "categories": tuple(raw["categories"])})
    missing = sorted(set(safety.high_risk_categories) - set(label.categories))
    if missing:
        raise ValueError(f"label.categories must include every safety.high_risk_categories entry; missing {missing}")
    return label


def _stage_model(raw: Mapping[str, Any]) -> StageModel:
    values = _keys(raw, StageModel)
    return StageModel(**{**values, "allowed": tuple(values.get("allowed", ()))})


def _models(raw: Mapping[str, Any]) -> ModelSettings:
    models = ModelSettings(**{stage: _stage_model(raw[stage]) for stage in ModelSettings.__dataclass_fields__})
    for stage in ModelSettings.__dataclass_fields__:
        effort = getattr(models, stage).effort
        if effort is not None and effort not in _EFFORTS:
            raise ValueError(f"models.{stage}.effort must be one of {_EFFORTS}")
    return models


def _eval(raw: Mapping[str, Any]) -> EvalSettings:
    values = _keys(raw, EvalSettings)
    fractions = {str(k): float(v) for k, v in values["split_fractions"].items()}
    if set(fractions) != {"train", "validation", "test"} or abs(sum(fractions.values()) - 1.0) > 1e-9:
        raise ValueError("eval.split_fractions needs train, validation and test, summing to 1")
    if values["max_rounds"] < 1:
        raise ValueError("eval.max_rounds must be at least 1")
    if not 0.0 <= values["test_private_share"] < 1.0:
        raise ValueError("eval.test_private_share must be at least 0 and below 1")
    return EvalSettings(**{**values, "split_fractions": _frozen(fractions)})


def _defects(raw: Mapping[str, Any]) -> DefectSettings:
    values = _keys(raw, DefectSettings)
    return DefectSettings(**{**values, "fix_labels": tuple(values["fix_labels"])})


def _promote(raw: Mapping[str, Any]) -> PromoteSettings:
    promote = PromoteSettings(tuple(raw["allowed_paths"]), tuple(raw["forbidden_paths"]))
    if "yardstick/" not in promote.forbidden_paths:
        raise ValueError("promote.forbidden_paths must include yardstick/: the improve loop may not edit its judge")
    return promote


def _settings(source: Path, raw: Mapping[str, Any]) -> Settings:
    corpus = _corpus(raw["corpus"])
    safety = SafetySettings(high_risk_categories=tuple(raw["safety"]["high_risk_categories"]))
    return Settings(
        source=source,
        offline=bool(raw["offline"]),
        paths=_paths(source.parent, raw["paths"]),
        corpus=corpus,
        metrics=_metrics(raw["metrics"], corpus.groups),
        gate=GateSettings(**_keys(raw["gate"], GateSettings)),
        prod=ProdSettings(**_keys(raw["prod"], ProdSettings)),
        judge=JudgeSettings(**_keys(raw["judge"], JudgeSettings)),
        lessons=LessonSettings(**_keys(raw["lessons"], LessonSettings)),
        review=ReviewSettings(**_keys(raw["review"], ReviewSettings)),
        safety=safety,
        models=_models(raw["models"]),
        harvest=_harvest(raw["harvest"]),
        github=GitHubSettings(**_keys(raw["github"], GitHubSettings)),
        llm=_llm(raw["llm"]),
        label=_label(raw["label"], safety),
        eval=_eval(raw["eval"]),
        defects=_defects(raw["defects"]),
        improve=_improve(raw["improve"]),
        promote=_promote(raw["promote"]),
        dataset=DatasetSettings(**_keys(raw["dataset"], DatasetSettings)),
    )
