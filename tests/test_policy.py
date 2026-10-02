"""The policy directory: loading, validation, hashing, derived policies, and the safety invariant at load time."""

from __future__ import annotations

import shutil
import tomllib

import pytest
import yaml

from honed.adapters import policy_dir
from honed.core import policy as core_policy
from honed.core.policy import PolicyError, glob_regex, path_in_scope
from honed.core.types import CheckAction, LessonConfidence, LessonKind
from honed.learn.sensitivity import weaken
from reviewkit import ROOT, rules, seed_policy


def copy(tmp_path):
    target = tmp_path / "policy"
    shutil.copytree(ROOT / "policy", target, dirs_exist_ok=True)
    return target


def test_the_seed_policy_loads_and_meets_the_size_gate():
    policy = seed_policy()
    assert policy.config.composition == "shared_rubric" and len(policy.config.members) == 2
    assert all(lesson.confidence is LessonConfidence.CANDIDATE for lesson in policy.lessons)
    kinds = {lesson.kind for lesson in policy.lessons}
    assert kinds == {LessonKind.PROMPT, LessonKind.CHECK}
    assert policy.prompt_tokens() <= 6000 and len(policy.active_lessons) <= 60
    assert not any(p.lstrip().startswith("<!--") for p in policy.prompts.values())  # attribution is not sent


def test_every_adapted_prompt_carries_its_attribution():
    for path in sorted((ROOT / "policy" / "prompts").glob("*.md")):
        text = path.read_text()
        if "pstack" in text:
            assert text.startswith("<!-- Adapted") and "Lauren Tan" in text.splitlines()[0], path.name
    assert "Lauren Tan" in (ROOT / "policy" / "lessons.yaml").read_text().splitlines()[0]


def test_the_hash_follows_the_content(tmp_path):
    target = copy(tmp_path)
    first = policy_dir.load(target, rules())
    assert first.content_hash == seed_policy().content_hash
    (target / "prompts" / "writing.md").write_text((target / "prompts" / "writing.md").read_text() + "\nMore.\n")
    assert policy_dir.load(target, rules()).content_hash != first.content_hash


@pytest.mark.parametrize(("edit", "message"), [
    (("composition = \"shared_rubric\"", "composition = \"specialist\""), "every member to list lenses"),
    (("model = \"claude-sonnet-5-5\"\neffort = \"medium\"", "model = \"gpt-9\"\neffort = \"medium\""), "not allowed"),
    (("nit_only_categories = [\"style\"", "nit_only_categories = [\"security\", \"style\""), "high-risk"),
    (("effort = \"high\"", "effort = \"extreme\""), "effort must be one of"),
])  # fmt: skip
def test_bad_configs_are_rejected(tmp_path, edit, message):
    target = copy(tmp_path)
    config = target / "config.toml"
    assert edit[0] in config.read_text()
    config.write_text(config.read_text().replace(edit[0], edit[1], 1))
    with pytest.raises(PolicyError, match=message):
        policy_dir.load(target, rules())


def _with_lesson(tmp_path, lesson: dict) -> None:
    target = copy(tmp_path)
    data = yaml.safe_load((target / "lessons.yaml").read_text())
    data["lessons"].append(lesson)
    (target / "lessons.yaml").write_text(yaml.safe_dump(data))
    policy_dir.load(target, rules())


SKIP = {"id": "skip-auth-noise", "kind": "prompt", "text": "t", "evidence": ["o/r#1", "o/r#2"],
        "skip_when": "the owner says so", "do_not_skip_when": "never mind"}  # fmt: skip


def test_no_lesson_may_suppress_a_high_risk_category(tmp_path):
    with pytest.raises(PolicyError, match="never suppress or downgrade high-risk"):
        _with_lesson(tmp_path, {**SKIP, "categories": ["design", "auth"]})
    with pytest.raises(PolicyError, match="must name the finding categories"):
        _with_lesson(tmp_path, {**SKIP, "id": "skip-everything"})
    check = {
        "id": "hide-sql",
        "kind": "check",
        "text": "t",
        "evidence": ["x"],
        "categories": ["security"],
        "do_not_skip_when": "d",
        "check": {"engine": "finding_text", "action": "suppress", "pattern": "SQL"},
    }
    with pytest.raises(PolicyError, match="high-risk"):
        _with_lesson(tmp_path, check)


def test_suppressing_lessons_need_a_risk_boundary_and_evidence(tmp_path):
    with pytest.raises(PolicyError, match="risk boundary"):
        _with_lesson(tmp_path, {**SKIP, "categories": ["design"], "do_not_skip_when": ""})
    with pytest.raises(PolicyError, match="cites the evidence"):
        _with_lesson(tmp_path, {**SKIP, "categories": ["design"], "evidence": []})


def test_a_derived_policy_regenerates_its_files_and_hash():
    seed = seed_policy()
    weak = weaken(seed)
    assert weak.content_hash != seed.content_hash and weak.lessons == ()
    assert not weak.config.verifier.enabled
    assert {m.effort for m in weak.config.members} == {weak.config.intent.effort} == {"low"}
    reparsed = core_policy.parse_config(tomllib.loads(weak.files["config.toml"]), rules())
    assert reparsed == weak.config
    assert core_policy.parse_lessons(yaml.safe_load(core_policy.lessons_yaml(seed.lessons)), rules()) == seed.lessons
    assert weak.files["prompts/verifier.md"] == seed.files["prompts/verifier.md"]


def test_scope_globs():
    assert glob_regex("**/tests/**").match("a/tests/b/c.py") and glob_regex("**/tests/**").match("tests/c.py")
    assert glob_regex("*.ts").match("x.ts") and not glob_regex("*.ts").match("a/x.ts")
    globs = ("**/*.ts", "!**/*.test.*")
    assert path_in_scope("src/a.ts", globs) and not path_in_scope("src/a.test.ts", globs)
    assert path_in_scope("anything", ())


def test_seed_suppress_checks_and_skip_lessons_are_all_safe():
    high_risk = rules().high_risk
    for lesson in seed_policy().lessons:
        if lesson.suppresses:
            assert lesson.categories and not set(lesson.categories) & high_risk, lesson.id
        if lesson.check is not None:
            assert lesson.check.action is CheckAction.FLAG


def test_the_changelog_is_not_part_of_the_policy(tmp_path):
    import shutil

    from honed.adapters.policy_dir import CHANGELOG, append_changelog, load

    shutil.copytree(ROOT / "policy", tmp_path / "policy")
    before = load(tmp_path / "policy", rules())
    append_changelog(tmp_path / "policy", "## one")
    append_changelog(tmp_path / "policy", "## two")
    text = (tmp_path / "policy" / CHANGELOG).read_text()
    assert text.index("## one") < text.index("## two") and text.startswith("# Policy changelog")
    assert load(tmp_path / "policy", rules()).content_hash == before.content_hash
