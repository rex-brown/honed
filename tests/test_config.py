from pathlib import Path

import pytest

from honed import config
from honed.core.types import Severity

ROOT = Path(__file__).resolve().parents[1]


def test_the_project_config_loads():
    s = config.load(ROOT / "honed.toml")
    assert s.metrics.beta == 0.5
    assert s.metrics.severity_weights[Severity.IMPORTANT] == 3.0
    assert dict(s.metrics.language_weights) == {"typescript": 0.45, "cpp": 0.25, "python": 0.20, "other": 0.10}
    assert s.harvest.target_prs == 1000
    assert s.gate.cost_cap_usd == 0.80 and s.metrics.max_neutral_vu_per_round == 3
    assert s.corpus.language_of("scikit-learn/scikit-learn") == "python"
    assert s.paths.sqlite.is_relative_to(ROOT)


def test_held_out_repos_are_excluded():
    excluded = config.load(ROOT / "honed.toml").corpus.excluded
    for repo in ("getsentry/sentry", "keycloak/keycloak", "redis/redis", "valkey-io/valkey", "nodejs/node"):
        assert repo in excluded
    assert len(config.load(ROOT / "honed.toml").corpus.exclude["aacr_bench"]) == 50


def test_a_corpus_repo_on_the_exclude_list_is_a_config_error(tmp_path):
    text = (
        (ROOT / "honed.toml")
        .read_text()
        .replace('near_duplicate = ["redis/redis"]', 'near_duplicate = ["django/django"]')
    )
    path = tmp_path / "honed.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match="django/django"):
        config.load(path)


def test_unknown_keys_are_rejected(tmp_path):
    text = (ROOT / "honed.toml").read_text().replace("[judge]\n", "[judge]\nmin_acuracy = 0.9\n")
    path = tmp_path / "honed.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match="min_acuracy"):
        config.load(path)


def test_llm_and_label_settings_load():
    s = config.load(ROOT / "honed.toml")
    assert s.llm.concurrency == 2 and s.llm.claude_code.isolation in ("safe_mode", "config_dir")
    assert s.llm.claude_code.stop_at_utilization == 0.80
    assert s.llm.claude_code.stop_at_weekly_utilization == 0.85
    assert s.models.judge.online == "claude-fable-5-1" and s.models.judge.effort == "medium"
    assert set(s.safety.high_risk_categories) <= set(s.label.categories)
    assert s.paths.yardstick_prompts == ROOT / "yardstick" / "prompts"


def test_the_improve_loop_may_not_touch_its_judge():
    promote = config.load(ROOT / "honed.toml").promote
    assert promote.permits("policy/lessons.yaml") and promote.permits("policy/prompts/finder.md")
    for path in ("yardstick/prompts/addressed.md", "src/honed/core/scoring.py", "honed.toml", "METRICS.md"):
        assert not promote.permits(path)


def test_the_yardstick_must_stay_forbidden(tmp_path):
    text = (ROOT / "honed.toml").read_text().replace('forbidden_paths = ["yardstick/", ', "forbidden_paths = [")
    path = tmp_path / "honed.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match="yardstick"):
        config.load(path)


def test_label_categories_must_cover_high_risk_ones(tmp_path):
    text = (ROOT / "honed.toml").read_text().replace('"idempotency", "concurrency",\n  "other",', '"other",')
    path = tmp_path / "honed.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match="idempotency"):
        config.load(path)
