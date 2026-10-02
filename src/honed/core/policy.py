"""The reviewer policy (ARCHITECTURE.md section 3): prompts, panel and stage settings, and lessons.

Pure. The caller reads `policy/` and parses `config.toml` and `lessons.yaml` (`adapters/policy_dir.py`); `build`
validates the parsed data into a `Policy` identified by the content hash of the directory's files, and `derive`
makes a changed policy with regenerated file texts, so a derived policy (the weakened one of the sensitivity check)
has a hash of its own.

Safety (CLAUDE.md): no lesson may suppress or downgrade findings in `[safety] high_risk_categories`. A suppressing
lesson (a skip rule, or a suppress check) must name the finding categories it applies to, none of them high-risk,
and state its risk boundary (`do_not_skip_when`). A severity rule capping a category at Nit may not name a high-risk
category either. `review/rank.py` enforces the same invariant again at run time.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from honed.core.types import (
    CheckAction,
    CheckEngine,
    CheckRule,
    Lesson,
    LessonConfidence,
    LessonKind,
    LessonScope,
    LessonStats,
    PolicyVersion,
    Severity,
)

CONFIG_FILE = "config.toml"
LESSONS_FILE = "lessons.yaml"
PROMPT_DIR = "prompts/"

LENSES = ("correctness", "root_cause", "security", "verification", "design", "comments")
LANGUAGE_LENSES = {"typescript": "lang_typescript", "cpp": "lang_cpp", "python": "lang_python"}
FRAME_PROMPTS = ("intent", "finder", "shared", "specialist", "verifier", "writing")
POLICY_CHANGE_LENS = "policy_change"  # self-review only: replaces the code lenses when a policy reviews a policy diff
SELF_REVIEW_PROMPTS = (POLICY_CHANGE_LENS,)
REQUIRED_PROMPTS = (*FRAME_PROMPTS, *(f"lens_{lens}" for lens in LENSES), *LANGUAGE_LENSES.values(),
                    *SELF_REVIEW_PROMPTS)  # fmt: skip
COMPOSITIONS = ("specialist", "shared_rubric", "mixed")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ATTRIBUTION = re.compile(r"\A\s*<!--.*?-->\s*", re.S)


class PolicyError(ValueError):
    """The policy directory is malformed or breaks a rule (the safety invariant, an unknown model, ...)."""


# ---- settings ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelChoice:
    """Which models a policy may choose for one stage (`honed.toml` `[models.<stage>]`)."""

    default: str
    allowed: tuple[str, ...] = ()
    tunable: bool = True

    def permits(self, model: str) -> bool:
        return model == self.default or (self.tunable and model in self.allowed)


@dataclass(frozen=True)
class PolicyRules:
    """What a policy is checked against; all of it sits outside `policy/` so the improve loop can't change it."""

    high_risk: frozenset[str]
    categories: tuple[str, ...]
    languages: tuple[str, ...]
    models: Mapping[str, ModelChoice]  # stage (intent, finders, verifier) -> choice


@dataclass(frozen=True)
class Member:
    """One finder panel member. No lenses: the shared checklist (every lens); otherwise a specialist."""

    id: str
    model: str
    effort: str | None
    lenses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stage:
    model: str
    effort: str | None
    max_tokens: int
    enabled: bool = True


@dataclass(frozen=True)
class ContextBudget:
    max_files: int  # changed files shown with their surrounding code
    max_lines: int  # lines of diff and code shown in total
    hunk_context_lines: int  # lines of unchanged code shown around each hunk
    max_callers: int  # lines referencing a changed symbol, from files the PR didn't change
    max_prior_threads: int  # review threads on the same files from before the PR
    max_earlier_threads: int  # review threads from earlier rounds of the same PR
    max_guidance_chars: int  # of the repo's REVIEW.md / CLAUDE.md


@dataclass(frozen=True)
class RankRules:
    confidence_threshold: float  # a verified finding below this is noted, not posted
    nit_cap: int  # posted Nits per review; the rest are noted
    act_on_flag: int  # more "act on" findings than this flags the review (the verifier is filtering too little)
    important_min_evidence: int  # Important needs this evidence level or higher, else it is posted as a Nit
    nit_only_categories: tuple[str, ...]  # findings in these categories are never Important
    dedup_line_slack: int  # lines apart that still count as the same location when merging duplicates


@dataclass(frozen=True)
class PolicyConfig:
    composition: str  # specialist | shared_rubric | mixed
    language_lens: bool  # every member also applies the lens for the PR's language
    members: tuple[Member, ...]
    intent: Stage
    finders: Stage  # max_tokens and enabled apply to every member; model and effort are per member
    max_findings: int  # per member
    verifier: Stage
    context: ContextBudget
    rank: RankRules


@dataclass(frozen=True)
class Policy:
    content_hash: str
    files: Mapping[str, str]  # path relative to the policy directory -> text
    prompts: Mapping[str, str]  # prompt name -> text, attribution header removed
    config: PolicyConfig
    lessons: tuple[Lesson, ...]

    @property
    def short_hash(self) -> str:
        return self.content_hash[:12]

    @property
    def version(self) -> PolicyVersion:
        return PolicyVersion(self.content_hash, tuple(sorted(self.files)))

    @property
    def active_lessons(self) -> tuple[Lesson, ...]:
        return tuple(lesson for lesson in self.lessons if lesson.confidence is not LessonConfidence.RETIRED)

    def lessons_for(self, *, language: str, repo: str, paths: Sequence[str]) -> tuple[Lesson, ...]:
        """Active lessons whose scope matches the review: its language, repo, and at least one changed path."""
        return tuple(lesson for lesson in self.active_lessons if in_scope(lesson.scope, language, repo, paths))

    def prompt_tokens(self) -> int:
        """Approximate tokens (4 characters each) of the policy text one review can send, for the size gate
        (METRICS.md section 3): every prompt, but only the largest language lens (a review uses one) and not the
        self-review lens (a code review never sends it), plus every active lesson."""
        languages = set(LANGUAGE_LENSES.values())
        skipped = languages | set(SELF_REVIEW_PROMPTS)
        shared = sum(len(text) for name, text in self.prompts.items() if name not in skipped)
        language = max((len(self.prompts[name]) for name in languages if name in self.prompts), default=0)
        lessons = sum(len(lesson_text(lesson)) for lesson in self.active_lessons)
        return (shared + language + lessons) // 4


def lesson_text(lesson: Lesson) -> str:
    return "\n".join(p for p in (lesson.text, lesson.applies_when, lesson.skip_when, lesson.do_not_skip_when,
                                 lesson.example_signal) if p)  # fmt: skip


# ---- hashing and scope -------------------------------------------------------------------------------------


def content_hash(files: Mapping[str, str]) -> str:
    """sha256 over every file's relative path and text, in path order."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode() + b"\0" + files[path].encode() + b"\0")
    return digest.hexdigest()


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A path glob: `**/` any directories (or none), `**` anything, `*` and `?` within one path segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_in_scope(path: str, globs: Sequence[str]) -> bool:
    """No globs: every path. Otherwise the path matches an include glob and no `!`-prefixed exclude glob."""
    if not globs:
        return True
    includes = [g for g in globs if not g.startswith("!")]
    excludes = [g[1:] for g in globs if g.startswith("!")]
    if any(glob_regex(g).match(path) for g in excludes):
        return False
    return not includes or any(glob_regex(g).match(path) for g in includes)


def in_scope(scope: LessonScope, language: str, repo: str, paths: Sequence[str]) -> bool:
    if scope.languages and language not in scope.languages:
        return False
    if scope.repos and repo.lower() not in {r.lower() for r in scope.repos}:
        return False
    return not scope.paths or any(path_in_scope(p, scope.paths) for p in paths)


def strip_attribution(text: str) -> str:
    """A prompt file's leading `<!-- ... -->` attribution is for readers of the file, not for the model."""
    return _ATTRIBUTION.sub("", text, count=1)


# ---- building ----------------------------------------------------------------------------------------------


def _get(data: Mapping[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if key not in data:
        raise PolicyError(f"{where}: missing `{key}`")
    value = data[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise PolicyError(f"{where}.{key}: expected {getattr(kind, '__name__', kind)}, got {value!r}")
    return value


def _only(data: Mapping[str, Any], keys: Iterable[str], where: str) -> None:
    unknown = sorted(set(data) - set(keys))
    if unknown:
        raise PolicyError(f"{where}: unknown keys {unknown}")


def _effort(value: Any, where: str) -> str | None:
    if value in (None, ""):
        return None
    if value not in EFFORTS:
        raise PolicyError(f"{where}: effort must be one of {EFFORTS}, got {value!r}")
    return str(value)


def _model(stage: str, model: Any, rules: PolicyRules, where: str) -> str:
    choice = rules.models.get(stage)
    if not isinstance(model, str) or choice is None or not choice.permits(model):
        allowed = (choice.default, *choice.allowed) if choice else ()
        raise PolicyError(f"{where}: model {model!r} is not allowed for the {stage} stage (allowed: {allowed})")
    return model


def _stage(data: Mapping[str, Any], name: str, rules: PolicyRules, *, model_stage: str) -> Stage:
    where = f"{CONFIG_FILE} [{name}]"
    _only(data, ("model", "effort", "max_tokens", "enabled"), where)
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PolicyError(f"{where}.enabled: expected a boolean")
    model = _model(model_stage, data.get("model", rules.models[model_stage].default), rules, where)
    tokens = _get(data, "max_tokens", int, where)
    if tokens < 256:
        raise PolicyError(f"{where}.max_tokens: at least 256")
    return Stage(model, _effort(data.get("effort"), where), tokens, enabled)


def _members(raw: Any, composition: str, rules: PolicyRules) -> tuple[Member, ...]:
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"{CONFIG_FILE}: [[panel.members]] needs at least one member")
    members, seen = [], set()
    for n, data in enumerate(raw, 1):
        where = f"{CONFIG_FILE} panel.members[{n}]"
        _only(data, ("id", "model", "effort", "lenses"), where)
        member_id = _get(data, "id", str, where)
        if not _SLUG.match(member_id) or member_id in seen or member_id.startswith("check"):
            raise PolicyError(f"{where}.id: a unique lower-case slug not starting with 'check', got {member_id!r}")
        seen.add(member_id)
        lenses = tuple(data.get("lenses") or ())
        unknown = sorted(set(lenses) - set(LENSES))
        if unknown:
            raise PolicyError(f"{where}.lenses: unknown lenses {unknown} (known: {LENSES})")
        model = _model("finders", data.get("model"), rules, where)
        members.append(Member(member_id, model, _effort(data.get("effort"), where), lenses))
    specialists = sum(bool(m.lenses) for m in members)
    wanted = {"specialist": specialists == len(members), "shared_rubric": specialists == 0,
              "mixed": 0 < specialists < len(members)}  # fmt: skip
    if not wanted[composition]:
        needs = {"specialist": "every member to list lenses", "shared_rubric": "no member to list lenses",
                 "mixed": "some members with lenses and some without"}  # fmt: skip
        raise PolicyError(f"{CONFIG_FILE}: composition {composition!r} needs {needs[composition]}")
    return tuple(members)


def parse_config(data: Mapping[str, Any], rules: PolicyRules) -> PolicyConfig:
    _only(data, ("panel", "intent", "finders", "verifier", "context", "rank"), CONFIG_FILE)
    panel = _get(data, "panel", dict, CONFIG_FILE)
    _only(panel, ("composition", "language_lens", "members"), f"{CONFIG_FILE} [panel]")
    composition = _get(panel, "composition", str, f"{CONFIG_FILE} [panel]")
    if composition not in COMPOSITIONS:
        raise PolicyError(f"{CONFIG_FILE} [panel].composition must be one of {COMPOSITIONS}")
    finders = dict(_get(data, "finders", dict, CONFIG_FILE))
    max_findings = _get(finders, "max_findings", int, f"{CONFIG_FILE} [finders]")
    finders.pop("max_findings")
    context = _get(data, "context", dict, CONFIG_FILE)
    rank = _get(data, "rank", dict, CONFIG_FILE)
    budget = ContextBudget(**{k: _get(context, k, int, f"{CONFIG_FILE} [context]")
                              for k in ContextBudget.__dataclass_fields__})  # fmt: skip
    _only(context, ContextBudget.__dataclass_fields__, f"{CONFIG_FILE} [context]")
    _only(rank, RankRules.__dataclass_fields__, f"{CONFIG_FILE} [rank]")
    where = f"{CONFIG_FILE} [rank]"
    nit_only = tuple(_get(rank, "nit_only_categories", list, where))
    rules_ = RankRules(
        confidence_threshold=_get(rank, "confidence_threshold", float, where),
        nit_cap=_get(rank, "nit_cap", int, where),
        act_on_flag=_get(rank, "act_on_flag", int, where),
        important_min_evidence=_get(rank, "important_min_evidence", int, where),
        nit_only_categories=nit_only,
        dedup_line_slack=_get(rank, "dedup_line_slack", int, where),
    )
    if not 0 <= rules_.confidence_threshold <= 1 or not 1 <= rules_.important_min_evidence <= 5:
        raise PolicyError(f"{where}: confidence_threshold in [0, 1] and important_min_evidence in 1..5")
    if min(rules_.nit_cap, rules_.act_on_flag, rules_.dedup_line_slack, max_findings) < 0 or max_findings < 1:
        raise PolicyError(f"{where}: caps must be non-negative, and [finders] max_findings at least 1")
    unknown = sorted(set(nit_only) - set(rules.categories))
    if unknown:
        raise PolicyError(f"{where}.nit_only_categories: unknown categories {unknown}")
    risky = sorted(set(nit_only) & rules.high_risk)
    if risky:
        raise PolicyError(f"{where}.nit_only_categories may not downgrade high-risk categories {risky}")
    language_lens = panel.get("language_lens", True)
    if not isinstance(language_lens, bool):
        raise PolicyError(f"{CONFIG_FILE} [panel].language_lens: expected a boolean")
    return PolicyConfig(
        composition=composition,
        language_lens=language_lens,
        members=_members(panel.get("members"), composition, rules),
        intent=_stage(_get(data, "intent", dict, CONFIG_FILE), "intent", rules, model_stage="intent"),
        finders=_stage({"max_tokens": finders.get("max_tokens"), **finders}, "finders", rules, model_stage="finders"),
        max_findings=max_findings,
        verifier=_stage(_get(data, "verifier", dict, CONFIG_FILE), "verifier", rules, model_stage="verifier"),
        context=budget,
        rank=rules_,
    )


_LESSON_KEYS = ("id", "kind", "scope", "text", "evidence", "applies_when", "skip_when", "do_not_skip_when",
                "example_signal", "confidence", "stats", "check", "categories")  # fmt: skip
_CHECK_KEYS = ("engine", "pattern", "threshold", "select", "exclude", "action", "severity", "category")


def _regex(value: str, where: str) -> str:
    try:
        re.compile(value)
    except re.error as error:
        raise PolicyError(f"{where}: bad regex {value!r}: {error}") from None
    return value


def _check(data: Any, where: str, rules: PolicyRules) -> CheckRule:
    if not isinstance(data, dict):
        raise PolicyError(f"{where}: a check lesson needs a `check` mapping")
    _only(data, _CHECK_KEYS, where)
    try:
        engine = CheckEngine(data.get("engine"))
        action = CheckAction(data.get("action", "flag"))
        severity = Severity(data.get("severity", "nit"))
    except ValueError as error:
        raise PolicyError(f"{where}: {error}") from None
    rule = CheckRule(
        engine=engine, pattern=_regex(str(data.get("pattern", "")), f"{where}.pattern"),
        threshold=data.get("threshold"), select=_regex(str(data.get("select", "")), f"{where}.select"),
        exclude=_regex(str(data.get("exclude", "")), f"{where}.exclude"), action=action, severity=severity,
        category=str(data.get("category", "")),
    )  # fmt: skip
    needs = {
        CheckEngine.ADDED_LINES_REGEX: bool(rule.pattern),
        CheckEngine.STRUCTURAL_PATTERN: bool(rule.pattern and rule.select),
        CheckEngine.THRESHOLD: isinstance(rule.threshold, int) and rule.threshold > 0,
        CheckEngine.FINDING_TEXT: bool(rule.pattern),
    }
    if not needs[engine]:
        raise PolicyError(f"{where}: engine {engine.value} is missing its pattern, select or threshold")
    if (engine is CheckEngine.FINDING_TEXT) != (action is CheckAction.SUPPRESS):
        raise PolicyError(f"{where}: the finding_text engine is for suppress checks, and suppress checks use only it")
    if action is CheckAction.FLAG and rule.category not in rules.categories:
        raise PolicyError(f"{where}.category: a flag check needs a category from [label] categories")
    return rule


def _scope(data: Any, where: str, rules: PolicyRules) -> LessonScope:
    data = data or {}
    if not isinstance(data, dict):
        raise PolicyError(f"{where}: expected a mapping")
    _only(data, ("languages", "paths", "repos"), where)
    scope = LessonScope(tuple(data.get("languages") or ()), tuple(data.get("paths") or ()),
                        tuple(data.get("repos") or ()))  # fmt: skip
    unknown = sorted(set(scope.languages) - set(rules.languages))
    if unknown:
        raise PolicyError(f"{where}.languages: unknown languages {unknown}")
    return scope


def parse_lesson(data: Any, rules: PolicyRules, n: int = 0) -> Lesson:
    where = f"{LESSONS_FILE} lessons[{n}]"
    if not isinstance(data, dict):
        raise PolicyError(f"{where}: expected a mapping")
    _only(data, _LESSON_KEYS, where)
    lesson_id = _get(data, "id", str, where)
    where = f"{LESSONS_FILE} {lesson_id}"
    if not _SLUG.match(lesson_id):
        raise PolicyError(f"{where}: the id must be a lower-case slug")
    try:
        kind = LessonKind(data.get("kind"))
        confidence = LessonConfidence(data.get("confidence", "candidate"))
    except ValueError as error:
        raise PolicyError(f"{where}: {error}") from None
    evidence = tuple(str(e) for e in data.get("evidence") or ())
    if not evidence:
        raise PolicyError(f"{where}: every lesson cites the evidence that justifies it")
    categories = tuple(data.get("categories") or ())
    unknown = sorted(set(categories) - set(rules.categories))
    if unknown:
        raise PolicyError(f"{where}.categories: unknown categories {unknown}")
    stats = data.get("stats") or {}
    check = _check(data.get("check"), f"{where}.check", rules) if kind is LessonKind.CHECK else None
    if kind is LessonKind.PROMPT and data.get("check") is not None:
        raise PolicyError(f"{where}: a prompt lesson has no `check`")
    lesson = Lesson(
        id=lesson_id, kind=kind, scope=_scope(data.get("scope"), f"{where}.scope", rules),
        text=_get(data, "text", str, where).strip(), evidence=evidence,
        applies_when=str(data.get("applies_when") or "").strip(), skip_when=str(data.get("skip_when") or "").strip(),
        do_not_skip_when=str(data.get("do_not_skip_when") or "").strip(),
        example_signal=str(data.get("example_signal") or "").strip(), confidence=confidence,
        stats=LessonStats(int(stats.get("fires", 0)), stats.get("precision")), check=check, categories=categories,
    )  # fmt: skip
    if check is not None and check.action is CheckAction.FLAG and lesson.skip_when:
        raise PolicyError(f"{where}: a flag check has no skip_when (it raises findings, it doesn't dismiss them)")
    _check_safety(lesson, rules)
    return lesson


def _check_safety(lesson: Lesson, rules: PolicyRules) -> None:
    """The safety invariant, at load time."""
    if not lesson.suppresses:
        return
    where = f"{LESSONS_FILE} {lesson.id}"
    if not lesson.categories:
        raise PolicyError(f"{where}: a suppressing lesson must name the finding categories it applies to")
    risky = sorted(set(lesson.categories) & rules.high_risk)
    if risky:
        raise PolicyError(f"{where}: a lesson may never suppress or downgrade high-risk categories {risky}")
    if not lesson.do_not_skip_when:
        raise PolicyError(f"{where}: a suppressing lesson must state its risk boundary (do_not_skip_when)")


def parse_lessons(data: Any, rules: PolicyRules) -> tuple[Lesson, ...]:
    if data is None:
        return ()
    if not isinstance(data, dict) or set(data) != {"lessons"} or not isinstance(data["lessons"], list | None):
        raise PolicyError(f"{LESSONS_FILE}: expected a mapping with one key, `lessons`, holding a list")
    lessons = tuple(parse_lesson(item, rules, n) for n, item in enumerate(data["lessons"] or [], 1))
    ids = [lesson.id for lesson in lessons]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PolicyError(f"{LESSONS_FILE}: duplicate lesson ids {duplicates}")
    return lessons


def build(files: Mapping[str, str], config_data: Mapping[str, Any], lessons_data: Any, rules: PolicyRules) -> Policy:
    """A validated policy from its directory's files (path -> text) and the parsed config and lessons."""
    prompts = {path[len(PROMPT_DIR) : -3]: strip_attribution(text) for path, text in files.items()
               if path.startswith(PROMPT_DIR) and path.endswith(".md")}  # fmt: skip
    missing = [name for name in REQUIRED_PROMPTS if name not in prompts]
    if missing:
        raise PolicyError(f"policy prompts missing: {missing}")
    for required in (CONFIG_FILE, LESSONS_FILE):
        if required not in files:
            raise PolicyError(f"policy file missing: {required}")
    return Policy(
        content_hash=content_hash(files),
        files=dict(files),
        prompts=prompts,
        config=parse_config(config_data, rules),
        lessons=parse_lessons(lessons_data, rules),
    )


# ---- derived policies --------------------------------------------------------------------------------------


def toml_value(value: Any) -> str:
    """A value in TOML syntax (the scalars and lists a policy config holds)."""
    return _toml_value(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML form for {value!r}")


def _stage_dict(stage: Stage) -> dict[str, Any]:
    out: dict[str, Any] = {"enabled": stage.enabled, "model": stage.model, "max_tokens": stage.max_tokens}
    if stage.effort:
        out["effort"] = stage.effort
    return out


def config_toml(config: PolicyConfig) -> str:
    """`config.toml` text for a config (derived policies are written and hashed in this canonical form)."""
    lines = [
        "# Generated from a derived policy.",
        "",
        "[panel]",
        f"composition = {_toml_value(config.composition)}",
        f"language_lens = {_toml_value(config.language_lens)}",
    ]
    for member in config.members:
        lines += ["", "[[panel.members]]", f"id = {_toml_value(member.id)}", f"model = {_toml_value(member.model)}"]
        if member.effort:
            lines.append(f"effort = {_toml_value(member.effort)}")
        lines.append(f"lenses = {_toml_value(list(member.lenses))}")
    sections: list[tuple[str, dict[str, Any]]] = [
        ("intent", _stage_dict(config.intent)),
        ("finders", {"enabled": config.finders.enabled, "max_tokens": config.finders.max_tokens,
                     "max_findings": config.max_findings}),
        ("verifier", _stage_dict(config.verifier)),
        ("context", {k: getattr(config.context, k) for k in ContextBudget.__dataclass_fields__}),
        ("rank", {k: getattr(config.rank, k) for k in RankRules.__dataclass_fields__}),
    ]  # fmt: skip
    for name, values in sections:
        lines += ["", f"[{name}]", *(f"{k} = {_toml_value(v)}" for k, v in values.items())]
    return "\n".join(lines) + "\n"


def lesson_dict(lesson: Lesson) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": lesson.id, "kind": lesson.kind.value,
        "scope": {"languages": list(lesson.scope.languages), "paths": list(lesson.scope.paths),
                  "repos": list(lesson.scope.repos)},
        "categories": list(lesson.categories), "text": lesson.text, "evidence": list(lesson.evidence),
        "confidence": lesson.confidence.value,
    }  # fmt: skip
    for key in ("applies_when", "skip_when", "do_not_skip_when", "example_signal"):
        if getattr(lesson, key):
            out[key] = getattr(lesson, key)
    if lesson.check is not None:
        c = lesson.check
        out["check"] = {"engine": c.engine.value, "action": c.action.value, "severity": c.severity.value,
                        "category": c.category, **{k: v for k in ("pattern", "select", "exclude", "threshold")
                                                   if (v := getattr(c, k)) not in ("", None)}}  # fmt: skip
    return out


def lessons_yaml(lessons: Sequence[Lesson]) -> str:
    """`lessons.yaml` text for lessons: JSON, which is valid YAML."""
    return json.dumps({"lessons": [lesson_dict(lesson) for lesson in lessons]}, indent=2) + "\n"


def derive(policy: Policy, *, config: PolicyConfig | None = None, lessons: Sequence[Lesson] | None = None) -> Policy:
    """The policy with a new config and/or lessons; the changed files are regenerated, so the hash changes."""
    files = dict(policy.files)
    if config is not None:
        files[CONFIG_FILE] = config_toml(config)
    if lessons is not None:
        files[LESSONS_FILE] = lessons_yaml(lessons)
    return replace(
        policy,
        content_hash=content_hash(files),
        files=files,
        config=config or policy.config,
        lessons=tuple(lessons) if lessons is not None else policy.lessons,
    )
