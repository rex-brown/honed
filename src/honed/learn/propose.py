"""The proposer (ARCHITECTURE.md section 7, step 1): one candidate = one edit of `policy/`, with a hypothesis that names
a mechanism, grounded in the incumbent's latest evaluation on the feed split and in the decision log.

Generators (each one model call, `[models.proposer]`, its prompt in `learn/prompts/`, which are code, not policy):
- lesson miner: clusters missed gold issues, false positives and valid nits beyond the cap into one lesson add,
  change or remove (acceptance rules in `learn/lessons.py`);
- reflective mutation: one prompt passage or settings edit, from failure traces and the judge's reasons;
- subtractive: removes a lesson or trims a prompt passage;
- combine (on a plateau): unites near-misses.
The proposer sees the incumbent's files, its size, the diagnostics, failure cases (PR text, as untrusted data), the
decision log and the rules a policy must keep (high-risk categories, allowed models, efforts). `context` builds that
text once per round; `Proposer.propose` asks one generator for one proposal.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

from honed.core import scoring
from honed.core.evals import Decision, EvalRun
from honed.core.improve import EditKind, GeneratorKind, PolicyEdit, Proposal
from honed.core.policy import COMPOSITIONS, EFFORTS, LENSES, Policy, strip_attribution
from honed.core.scoring import Credit, ScoringParams
from honed.core.types import Bucket, CheckAction, CheckEngine, FindingClass, HarvestedPR, LessonKind, PRKey, Severity
from honed.learn import policy_edit
from honed.ports.llm import LLM, LLMCall, LLMError

UNTRUSTED = "untrusted_pr_data"
_TAG = re.compile(rf"<(/?){UNTRUSTED}", re.I)
ALLOWED_KINDS: Mapping[GeneratorKind, frozenset[EditKind]] = {
    GeneratorKind.LESSON_MINER: frozenset({EditKind.LESSON_ADD, EditKind.LESSON_CHANGE, EditKind.LESSON_REMOVE}),
    GeneratorKind.REFLECTIVE: frozenset({EditKind.PROMPT_REPLACE, EditKind.CONFIG_SET}),
    GeneratorKind.SUBTRACTIVE: frozenset({EditKind.LESSON_REMOVE, EditKind.PROMPT_REPLACE}),
    GeneratorKind.COMBINE: frozenset(EditKind),
}


class ProposalError(LLMError):
    """The proposer's answer is not a usable proposal."""


def prompt(name: str) -> str:
    """A proposer prompt, without its attribution header (that is for readers of the file)."""
    return strip_attribution(resources.files("honed.learn").joinpath("prompts", f"{name}.md").read_text())


def _block(text: str) -> str:
    """PR-derived text, inside a block it can't close or reopen."""
    inner = _TAG.sub(lambda m: f"<{m.group(1)}untrusted-pr-data", text.strip())
    return f"<{UNTRUSTED}>\n{inner}\n</{UNTRUSTED}>"


# ---- what the proposer reads -------------------------------------------------------------------------------


def _author(items: Mapping[PRKey, HarvestedPR], key: PRKey) -> str:
    item = items.get(key)
    return item.pr.author.login if item is not None and item.pr.author else "(unknown)"


def failure_cases(run: EvalRun, items: Mapping[PRKey, HarvestedPR], params: ScoringParams,
                  limit: int) -> dict[str, list[dict[str, Any]]]:  # fmt: skip
    """Missed gold issues, false positives, valid nits beyond the cap and false dismissals of a run, the heaviest
    first, at most `limit` of each."""
    missed, wrong, over, dismissed = [], [], [], []
    for record in run.records:
        result, key = record.result, record.result.pr
        ref, author = f"{key}", _author(items, key)
        scored = scoring.score_findings(result, params)
        hit = {s.gold.id for s in scored if s.credit is Credit.TP and s.gold is not None}
        rescued = {m.gold_id for m in record.other_matches if m.klass is FindingClass.TP}
        threads = {t.id: t for t in items[key].pr.threads} if key in items else {}
        for g in result.gold:
            if g.id in hit:
                continue
            first = threads.get(g.source_threads[0]) if g.source_threads else None
            comment = first.first.body if first is not None and first.first else ""
            missed.append({"pr": ref, "author": author, "language": result.language, "path": g.path,
                           "lines": [g.start_line, g.end_line], "severity": g.severity.value, "category": g.category,
                           "weight": params.severity_weights[g.severity] * g.conf, "issue": g.description[:400],
                           "human_comment": comment[:600],
                           "raised_but_not_posted": g.id in rescued})  # fmt: skip
        reasons = {m.finding_id: m.rationale for m in result.matches}
        for s in scored:
            if s.credit is not Credit.FP:
                continue
            f = s.finding
            case = {"pr": ref, "author": author, "language": result.language, "path": f.path, "line": f.start_line,
                    "severity": f.severity.value, "category": f.category, "weight": s.weight, "title": f.title,
                    "body": f.body[:400], "judge_reason": reasons.get(f.id, "")[:300], "raised_by": list(f.raised_by),
                    "lessons": list(f.lessons_cited), "lead_reviewer_reason": f.bucket_reason[:200],
                    "evidence_level": f.evidence_level, "class": s.klass.value}  # fmt: skip
            if s.inflated:
                case["class"] = "severity_inflation: valid, but the judge rates it a nit"
            (over if s.over_cap else wrong).append(case)
        buckets = {f.id: f for f in record.review.findings}
        for m in record.other_matches:
            f = buckets.get(m.finding_id)
            if m.klass is FindingClass.TP and f is not None and f.bucket is Bucket.DISMISSED:
                dismissed.append({"pr": ref, "author": author, "path": f.path, "line": f.start_line,
                                  "title": f.title, "lead_reviewer_reason": f.bucket_reason[:300],
                                  "lessons": list(f.lessons_cited), "weight": 1.0})  # fmt: skip

    def top(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(cases, key=lambda c: (-c["weight"], c["pr"], c.get("path", ""), c.get("line", 0)))[:limit]

    return {"missed_gold_issues": top(missed), "false_positives": top(wrong), "valid_nits_beyond_cap": top(over),
            "false_dismissals": top(dismissed)}  # fmt: skip


@dataclass(frozen=True)
class Rules:
    """What a policy must keep, shown to the proposer (the loader enforces it either way)."""

    categories: tuple[str, ...]
    high_risk: tuple[str, ...]
    models: Mapping[str, tuple[str, ...]]  # stage -> models a policy may choose
    max_lessons: int
    max_prompt_tokens: int
    min_prs: int
    min_authors: int


def config_keys(policy: Policy) -> list[str]:
    c = policy.config
    keys = ["panel.composition", "panel.language_lens"]
    keys += [f"panel.members[{n}].{k}" for n in range(1, len(c.members) + 1) for k in ("model", "effort", "lenses")]
    keys += [f"intent.{k}" for k in ("model", "effort", "max_tokens")]
    keys += ["finders.max_tokens", "finders.max_findings"]
    keys += [f"verifier.{k}" for k in ("enabled", "model", "effort", "max_tokens")]
    keys += [f"context.{k}" for k in c.context.__dataclass_fields__]
    keys += [f"rank.{k}" for k in c.rank.__dataclass_fields__]
    return keys


def _decision(d: Decision) -> str:
    return (f"- #{d.id} [{d.verdict}] {d.change[:200]}\n  hypothesis: {d.hypothesis[:300]}\n  delta: {d.delta}"
            + (f"\n  note: {d.note[:200]}" if d.note else ""))  # fmt: skip


def context(policy: Policy, diagnostics: Mapping[str, Any], cases: Mapping[str, Sequence[Mapping[str, Any]]],
            decisions: Sequence[Decision], rules: Rules, *, near_misses: Sequence[Mapping[str, Any]] = (),
            max_decisions: int = 40) -> str:  # fmt: skip
    """The user content every generator of a round reads. The self-review lens is left out: candidates are not
    written against the review that judges them (and may not edit it)."""
    files = "\n\n".join(f"### {path}\n```\n{text.rstrip()}\n```" for path, text in sorted(policy.files.items())
                        if path not in policy_edit.SELF_REVIEW_FILES)  # fmt: skip
    log = "\n".join(_decision(d) for d in decisions[-max_decisions:]) or "(empty)"
    rule_lines = [
        f"- Categories: {', '.join(rules.categories)}.",
        "- High-risk categories (never suppressed, dismissed by a lesson, or capped at Nit): "
        f"{', '.join(rules.high_risk)}.",
        *(f"- Models allowed for the {stage} stage: {', '.join(models)}." for stage, models in rules.models.items()),
        f"- Efforts: {', '.join(EFFORTS)}. Panel compositions: {', '.join(COMPOSITIONS)}. Lenses: {', '.join(LENSES)}.",
        f"- Size: {len(policy.active_lessons)} active lessons (max {rules.max_lessons}); "
        f"{policy.prompt_tokens()} prompt tokens (max {rules.max_prompt_tokens}).",
        f"- Lessons need evidence from {rules.min_prs} PRs by {rules.min_authors} authors.",
        f"- `config_set` keys: {', '.join(config_keys(policy))}.",
    ]  # fmt: skip
    parts = [
        "## The current policy", files,
        "## Its latest evaluation (the feed split): metrics and diagnostics",
        "```json\n" + json.dumps(diagnostics, indent=1, default=str) + "\n```",
        "## Failure cases from that evaluation", _block(json.dumps(cases, indent=1)),
        "## Decision log, newest last (read it before proposing)", log,
    ]  # fmt: skip
    if near_misses:
        parts += ["## Near misses", _block(json.dumps(list(near_misses), indent=1))]
    parts += ["## Rules", "\n".join(rule_lines), "Propose one change now."]
    return "\n\n".join(parts)


# ---- the answer ----------------------------------------------------------------------------------------------


def _obj(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": list(properties),
            "additionalProperties": False}  # fmt: skip


def _strings() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}}


def schema(categories: Sequence[str]) -> dict[str, Any]:
    check = _obj({
        "engine": {"type": "string", "enum": ["", *(e.value for e in CheckEngine)]}, "pattern": {"type": "string"},
        "select": {"type": "string"}, "exclude": {"type": "string"}, "threshold": {"type": "integer"},
        "action": {"type": "string", "enum": ["", *(a.value for a in CheckAction)]},
        "severity": {"type": "string", "enum": ["", *(s.value for s in Severity)]},
        "category": {"type": "string", "enum": ["", *categories]},
    })  # fmt: skip
    lesson = _obj({
        "id": {"type": "string"}, "kind": {"type": "string", "enum": ["", *(k.value for k in LessonKind)]},
        "languages": _strings(), "paths": _strings(), "repos": _strings(),
        "categories": {"type": "array", "items": {"type": "string", "enum": list(categories)}},
        "text": {"type": "string"}, "applies_when": {"type": "string"}, "skip_when": {"type": "string"},
        "do_not_skip_when": {"type": "string"}, "example_signal": {"type": "string"}, "evidence": _strings(),
        "check": check,
    })  # fmt: skip
    edit = _obj({
        "kind": {"type": "string", "enum": [k.value for k in EditKind]}, "lesson_id": {"type": "string"},
        "lesson": lesson, "file": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
        "settings": {"type": "array", "items": _obj({"key": {"type": "string"}, "value_json": {"type": "string"}})},
    })  # fmt: skip
    return _obj({"hypothesis": {"type": "string"}, "change": {"type": "string"}, "evidence": _strings(),
                 "why_not_check": {"type": "string"}, "edit": edit})  # fmt: skip


def _lesson(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """The answer's lesson in `lessons.yaml` shape, empty fields dropped."""
    if not raw or not raw.get("id"):
        return None
    out: dict[str, Any] = {"id": raw["id"], "kind": raw.get("kind") or "prompt"}
    scope = {k: list(raw.get(k) or []) for k in ("languages", "paths", "repos") if raw.get(k)}
    if scope:
        out["scope"] = scope
    for key in ("categories", "text", "applies_when", "skip_when", "do_not_skip_when", "example_signal", "evidence"):
        if raw.get(key):
            out[key] = list(raw[key]) if isinstance(raw[key], list) else raw[key]
    check = raw.get("check") or {}
    if out["kind"] == LessonKind.CHECK.value and check.get("engine"):
        out["check"] = {k: v for k, v in check.items() if v not in ("", None, 0)}
    return out


def parse(data: Any, generator: GeneratorKind, model: str) -> Proposal:
    if not isinstance(data, Mapping) or not isinstance(data.get("edit"), Mapping):
        raise ProposalError("the proposal has no edit")
    raw = data["edit"]
    try:
        kind = EditKind(raw.get("kind"))
    except ValueError:
        raise ProposalError(f"unknown edit kind {raw.get('kind')!r}") from None
    if kind not in ALLOWED_KINDS[generator]:
        raise ProposalError(f"the {generator.value} generator may not propose a {kind.value} edit")
    settings: dict[str, Any] = {}
    for pair in raw.get("settings") or []:
        try:
            settings[str(pair["key"])] = json.loads(pair["value_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ProposalError(f"bad config setting {pair!r}: {error}") from None
    edit = PolicyEdit(kind=kind, lesson=_lesson(raw.get("lesson") or {}), lesson_id=str(raw.get("lesson_id") or ""),
                      file=str(raw.get("file") or ""), old=str(raw.get("old") or ""), new=str(raw.get("new") or ""),
                      settings=settings)  # fmt: skip
    if kind is EditKind.LESSON_REMOVE and not edit.lesson_id:
        raise ProposalError("a lesson_remove names its lesson_id")
    if generator is GeneratorKind.SUBTRACTIVE and kind is EditKind.PROMPT_REPLACE and len(edit.new) >= len(edit.old):
        raise ProposalError("a subtractive prompt edit must make the passage shorter")
    hypothesis = str(data.get("hypothesis") or "").strip()
    if not hypothesis:
        raise ProposalError("the proposal states no hypothesis")
    return Proposal(generator=generator, hypothesis=hypothesis, change=str(data.get("change") or "").strip()[:300],
                    edit=edit, evidence=tuple(str(e) for e in data.get("evidence") or ()),
                    why_not_check=str(data.get("why_not_check") or "").strip(), model=model)  # fmt: skip


@dataclass(frozen=True)
class ProposerOptions:
    model: str
    effort: str | None
    max_tokens: int
    categories: tuple[str, ...]
    min_prs: int
    min_authors: int
    removal_min_exposure: int = 5  # `[gate] removal_min_exposure`, quoted to the proposer


class Proposer:
    def __init__(self, llm: LLM, options: ProposerOptions) -> None:
        self._llm = llm
        self._o = options
        self._schema = schema(options.categories)
        self._base = prompt("proposer")

    def system(self, generator: GeneratorKind) -> str:
        text = self._base.rstrip() + "\n\n" + prompt(generator.value)
        values = {"min_prs": self._o.min_prs, "min_authors": self._o.min_authors,
                  "removal_min_exposure": self._o.removal_min_exposure}  # fmt: skip
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)

    def propose(self, user: str, generator: GeneratorKind, *, sample: int = 0) -> Proposal:
        result = self._llm.complete(LLMCall(model=self._o.model, system=self.system(generator), user=user,
                                            schema=self._schema, max_tokens=self._o.max_tokens, effort=self._o.effort,
                                            sample=sample, stage=f"propose:{generator.value}"))  # fmt: skip
        return parse(result.data, generator, result.model or self._o.model)
