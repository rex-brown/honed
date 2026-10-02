"""The labeling judge on the LLM port: fixed prompts from `yardstick/prompts/`, JSON-schema answers, strict
parsing. PR text reaches the model only inside `<untrusted_pr_data>`, and every prompt says to ignore instructions
there (CLAUDE.md, untrusted input)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from honed.core.types import Addressed, Severity, Stance
from honed.ports.judge import (
    AddressedVerdict,
    FindingEvidence,
    GoldEvidence,
    GoldGroup,
    JudgeError,
    MatchVerdict,
    ThreadClass,
    ThreadEvidence,
    Validity,
)
from honed.ports.llm import LLM, LLMCall

PROMPTS = ("addressed", "classify", "gold", "validity", "match", "validity_set")
JUDGED_SEVERITY = {"important": Severity.IMPORTANT, "nit": Severity.NIT}  # the evaluation's validity question
NONE = "none"
NO_REPLY = "no_reply"
_TAG = re.compile(r"<(/?)untrusted_pr_data", re.I)


@dataclass(frozen=True)
class JudgeOptions:
    model: str
    effort: str | None
    max_tokens: int
    gold_max_tokens: int
    categories: tuple[str, ...]


def _neutralize(text: str) -> str:
    """PR text can't close or reopen the untrusted block."""
    return _TAG.sub(r"<\1untrusted-pr-data", text)


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def _location(ev: ThreadEvidence) -> str:
    if ev.lines is None:
        return f"{ev.path} (a comment on the whole file)"
    side = "" if ev.side == "RIGHT" else ", on the old side of the diff (deleted lines)"
    span = f"line {ev.lines[0]}" if ev.lines[0] == ev.lines[1] else f"lines {ev.lines[0]}-{ev.lines[1]}"
    return f"{ev.path}, {span}{side}"


def _thread_text(ev: ThreadEvidence, *, first_only: bool = False, head: bool = False) -> str:
    comments = ev.comments[:1] if first_only else ev.comments
    parts = [f"File: {_location(ev)}", "", "Review comment:" if first_only else "Review thread, in order:"]
    for n, c in enumerate(comments, 1):
        parts.append(f"[{n}] {c.author} ({c.role}):\n{c.body.strip()}")
    parts += ["", "Code the comment was made on (flagged lines marked >):", ev.anchor_code]
    if head:
        parts += ["", "The same region at the pull request's final head:", ev.head_code or "(not available)"]
    return "\n".join(parts)


def _wrap(pr: str, title: str, body: str) -> str:
    inner = _neutralize(f"Pull request: {pr}\nTitle: {title}\n\n{body}")
    return f"<untrusted_pr_data>\n{inner}\n</untrusted_pr_data>"


class LLMJudge:
    def __init__(self, llm: LLM, prompts: Mapping[str, str], options: JudgeOptions) -> None:
        missing = [name for name in PROMPTS if name not in prompts]
        if missing:
            raise ValueError(f"judge prompts missing: {missing}")
        listing = "\n".join(f"- `{c}`" for c in options.categories)
        self._prompts = {name: prompts[name].replace("{{categories}}", listing) for name in PROMPTS}
        self._prompts["validity_set"] = self._prompts["validity"].rstrip() + "\n\n" + self._prompts["validity_set"]
        self._llm = llm
        self._o = options
        self._categories = options.categories

    @property
    def model(self) -> str:
        return self._o.model

    @property
    def fingerprint(self) -> str:
        """What the evaluation's verdicts depend on: the model, effort and the match and validity prompts. A stored
        evaluation is reused, and compared with another, only under the same fingerprint."""
        parts = (self._o.model, self._o.effort or "", self._prompts["match"], self._prompts["validity_set"])
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]

    def _ask(self, prompt: str, user: str, schema: Mapping[str, Any], *, stage: str, pr: str, sample: int = 0,
             max_tokens: int | None = None) -> Mapping[str, Any]:  # fmt: skip
        result = self._llm.complete(
            LLMCall(model=self._o.model, system=self._prompts[prompt], user=user, schema=schema,
                    max_tokens=max_tokens or self._o.max_tokens, effort=self._o.effort, sample=sample, stage=stage,
                    pr=pr)
        )  # fmt: skip
        if not isinstance(result.data, Mapping):
            raise JudgeError(f"{stage}: expected a JSON object, got {type(result.data).__name__}")
        return result.data

    def _category(self, value: Any) -> str:
        if value not in self._categories:
            raise JudgeError(f"unknown category {value!r}")
        return str(value)

    # ---- questions ---------------------------------------------------------------------------------------

    def addressed(self, evidence: ThreadEvidence) -> AddressedVerdict:
        schema = _object({"reason": {"type": "string"},
                          "verdict": {"type": "string", "enum": [a.value for a in Addressed]}})  # fmt: skip
        user = _wrap(evidence.pr, evidence.title, _thread_text(evidence, head=True))
        data = self._ask("addressed", user, schema, stage="addressed", pr=evidence.pr)
        try:
            return AddressedVerdict(Addressed(data["verdict"]), str(data.get("reason", "")))
        except (KeyError, ValueError) as error:
            raise JudgeError(f"addressed: bad answer {dict(data)}") from error

    def classify(self, evidence: ThreadEvidence) -> ThreadClass:
        stances = [s.value for s in Stance] + [NO_REPLY]
        schema = _object({"reason": {"type": "string"}, "stance": {"type": "string", "enum": stances},
                          "category": {"type": "string", "enum": list(self._categories)}})  # fmt: skip
        user = _wrap(evidence.pr, evidence.title, _thread_text(evidence))
        data = self._ask("classify", user, schema, stage="classify", pr=evidence.pr)
        stance = data.get("stance")
        if stance not in stances:
            raise JudgeError(f"classify: unknown stance {stance!r}")
        return ThreadClass(
            None if stance == NO_REPLY else Stance(stance), self._category(data.get("category")),
            str(data.get("reason", "")),
        )  # fmt: skip

    def gold_groups(
        self, pr: str, title: str, threads: Sequence[ThreadEvidence], *, sample: int = 0
    ) -> list[GoldGroup]:
        if not threads:
            return []
        short = {f"T{n}": ev.thread_id for n, ev in enumerate(threads, 1)}
        blocks = [f"=== Thread {sid} ===\n{_thread_text(ev)}" for sid, ev in zip(short, threads, strict=True)]
        issue = _object({
            "thread_ids": {"type": "array", "items": {"type": "string", "enum": list(short)}, "minItems": 1},
            "severity": {"type": "string", "enum": [s.value for s in Severity]},
            "category": {"type": "string", "enum": list(self._categories)},
            "summary": {"type": "string"},
        })  # fmt: skip
        schema = _object({"issues": {"type": "array", "items": issue}})
        user = _wrap(pr, title, f"{len(threads)} threads: {', '.join(short)}\n\n" + "\n\n".join(blocks))
        data = self._ask("gold", user, schema, stage="gold", pr=pr, sample=sample, max_tokens=self._o.gold_max_tokens)
        groups, seen = [], set()
        for raw in data.get("issues") or []:
            ids = [short[i] for i in raw.get("thread_ids") or [] if i in short and short[i] not in seen]
            if not ids:
                continue
            seen.update(ids)
            try:
                severity = Severity(raw["severity"])
            except (KeyError, ValueError) as error:
                raise JudgeError(f"gold: bad severity in {raw}") from error
            groups.append(GoldGroup(tuple(ids), severity, self._category(raw.get("category")),
                                    str(raw.get("summary", ""))))  # fmt: skip
        missing = [sid for sid, tid in short.items() if tid not in seen]
        if missing:
            raise JudgeError(f"gold: threads left out of every issue: {missing}")
        return groups

    def validity(self, evidence: ThreadEvidence, *, sample: int = 0) -> Validity:
        schema = _object({"reason": {"type": "string"}, "valid": {"type": "boolean"}})
        user = _wrap(evidence.pr, evidence.title, _thread_text(evidence, first_only=True))
        data = self._ask("validity", user, schema, stage="audit_validity", pr=evidence.pr, sample=sample)
        if not isinstance(data.get("valid"), bool):
            raise JudgeError(f"validity: bad answer {dict(data)}")
        return Validity(data["valid"], str(data.get("reason", "")))

    # ---- evaluation --------------------------------------------------------------------------------------

    def match(self, pr: str, title: str, findings: Sequence[FindingEvidence], others: Sequence[FindingEvidence],
              gold: Sequence[GoldEvidence], *, sample: int = 0) -> list[MatchVerdict]:  # fmt: skip
        if not gold or not (findings or others):
            return [MatchVerdict(f.label, None, None, "no known issues") for f in (*findings, *others)]
        golds = [g.label for g in gold] + [NONE]
        f_labels = [f.label for f in findings]

        def enum(values: list[str]) -> dict[str, Any]:
            return {"type": "string", "enum": values} if values else {"type": "string"}

        schema = _object({
            "findings": {"type": "array", "items": _object({
                "id": enum(f_labels), "gold": enum(golds), "duplicate_of": enum([*f_labels, ""]),
                "reason": {"type": "string"}})},
            "other": {"type": "array", "items": _object({
                "id": enum([o.label for o in others]), "gold": enum(golds), "reason": {"type": "string"}})},
        })  # fmt: skip
        parts = [f"Known issues ({', '.join(g.label for g in gold)}):"]
        for g in gold:
            parts.append(f"=== {g.label} ===\nFile: {_span(g.path, g.lines)}\nIssue: {g.description.strip()}"
                         + (f"\nThe reviewer's comment:\n{g.comment.strip()}" if g.comment.strip() else "")
                         + f"\nCode (flagged lines marked >):\n{g.code}")  # fmt: skip
        parts.append(f"Review findings to match ({', '.join(f_labels) or 'none'}):")
        parts += [_finding_text(f) for f in findings]
        if others:
            parts.append(f"Other notes ({', '.join(o.label for o in others)}):")
            parts += [_finding_text(o) for o in others]
        user = _wrap(pr, title, "\n\n".join(parts))
        data = self._ask("match", user, schema, stage="match", pr=pr, sample=sample)
        known = {g.label for g in gold}
        out: dict[str, MatchVerdict] = {}
        for key, labels in (("findings", set(f_labels)), ("other", {o.label for o in others})):
            for raw in data.get(key) or []:
                label = raw.get("id")
                if label not in labels or label in out:
                    continue
                gold_label = raw.get("gold") if raw.get("gold") in known else None
                duplicate = raw.get("duplicate_of") if key == "findings" else None
                duplicate = duplicate if duplicate in labels and duplicate != label and gold_label is None else None
                out[label] = MatchVerdict(label, gold_label, duplicate, str(raw.get("reason", "")))
        missing = [f.label for f in (*findings, *others) if f.label not in out]
        if missing:
            raise JudgeError(f"match: no verdict for {missing}")
        return [out[f.label] for f in (*findings, *others)]

    def validity_many(self, pr: str, title: str, findings: Sequence[FindingEvidence], *,
                      sample: int = 0) -> dict[str, Validity]:  # fmt: skip
        if not findings:
            return {}
        labels = [f.label for f in findings]
        verdict = _object(
            {
                "id": {"type": "string", "enum": labels},
                "reason": {"type": "string"},
                "valid": {"type": "boolean"},
                "severity": {"type": "string", "enum": list(JUDGED_SEVERITY)},
            }
        )
        schema = _object({"verdicts": {"type": "array", "items": verdict}})  # fmt: skip
        blocks = [f"=== {f.label} ===\nFile: {_span(f.path, f.lines)}\n\nReview comment:\n{f.text.strip()}\n\n"
                  f"Code the comment was made on (flagged lines marked >):\n{f.code}" for f in findings]  # fmt: skip
        user = _wrap(pr, title, f"{len(findings)} review comments: {', '.join(labels)}\n\n" + "\n\n".join(blocks))
        data = self._ask("validity_set", user, schema, stage="validity", pr=pr, sample=sample)
        out: dict[str, Validity] = {}
        for raw in data.get("verdicts") or []:
            severity = JUDGED_SEVERITY.get(raw.get("severity"))
            if raw.get("id") in labels and raw["id"] not in out and isinstance(raw.get("valid"), bool) and severity:
                out[raw["id"]] = Validity(raw["valid"], str(raw.get("reason", "")), severity)
        missing = [label for label in labels if label not in out]
        if missing:
            raise JudgeError(f"validity: no verdict for {missing}")
        return out


def _span(path: str, lines: tuple[int, int]) -> str:
    if not path:
        return "(no location given: match on the description)"  # a benchmark's golden comment without one
    if lines == (0, 0):
        return f"{path} (the whole file)"
    return f"{path}, line {lines[0]}" if lines[0] == lines[1] else f"{path}, lines {lines[0]}-{lines[1]}"


def _finding_text(f: FindingEvidence) -> str:
    return f"=== {f.label} ===\nFile: {_span(f.path, f.lines)}\nFinding:\n{f.text.strip()}\n" \
        f"Code (flagged lines marked >):\n{f.code}"  # fmt: skip
