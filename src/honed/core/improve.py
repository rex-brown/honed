"""The improve loop's records (ARCHITECTURE.md section 7): proposals, candidates, gate verdicts, the policy versions
the loop promotes, and human labels for the judge audit. Pure data, plus the few rules that need no I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from honed.core import patches


class GeneratorKind(StrEnum):
    """Where a candidate came from (ARCHITECTURE.md section 7, step 1)."""

    LESSON_MINER = "lesson_miner"  # clusters missed issues and false positives into lesson edits
    REFLECTIVE = "reflective"  # prompt and config edits from failure traces and judge reasons
    SUBTRACTIVE = "subtractive"  # removes a lesson or trims prompt text
    COMBINE = "combine"  # on a plateau: one change that unites the mechanisms of near-misses


class EditKind(StrEnum):
    LESSON_ADD = "lesson_add"
    LESSON_CHANGE = "lesson_change"
    LESSON_REMOVE = "lesson_remove"
    PROMPT_REPLACE = "prompt_replace"  # one exact passage of one prompt file, replaced (or deleted: new = "")
    CONFIG_SET = "config_set"  # keys of config.toml, such as `rank.nit_cap` or `panel.members[1].effort`


@dataclass(frozen=True)
class PolicyEdit:
    """One change to `policy/`, as the proposer states it; the loop turns it into file texts and a diff."""

    kind: EditKind
    lesson: Mapping[str, Any] | None = None  # lesson_add, lesson_change: the whole lesson as lessons.yaml holds it
    lesson_id: str = ""  # lesson_change, lesson_remove
    file: str = ""  # prompt_replace: a path under policy/, such as prompts/verifier.md
    old: str = ""  # prompt_replace: the exact passage, found once in the file
    new: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)  # config_set: dotted key -> value


@dataclass(frozen=True)
class Proposal:
    generator: GeneratorKind
    hypothesis: str  # names a mechanism, grounded in the diagnostics
    change: str  # one line: what the edit does
    edit: PolicyEdit
    evidence: tuple[str, ...] = ()  # PR refs and diagnostics it rests on
    why_not_check: str = ""  # a prompt lesson: why a `check` lesson can't express it
    model: str = ""  # the proposer model


class Outcome(StrEnum):
    PROMOTED = "promoted"
    REJECTED = "rejected"  # failed the gate
    SUPERSEDED = "superseded"  # passed the gate, but another candidate of the round won
    SELF_REVIEW = "rejected_by_self_review"
    INVALID = "invalid"  # the edit doesn't apply, the policy doesn't load, or a lesson fails acceptance
    REFUSED = "refused"  # touches a path the promote step may not write
    REPEAT = "repeat"  # the same policy as an earlier rejected candidate
    SCREENED_OUT = "screened_out"  # its delta-S on the screen subset wasn't above 0, so the full split never ran
    UNMEASURED = "unmeasured"  # a pure removal that held its score, but what it removes was barely exercised
    INCOMPLETE = "incomplete"  # the run stopped (a plan limit, the call cap) before the verdict


@dataclass(frozen=True)
class RuleResult:
    """One METRICS.md section 3 rule: whether it held, and the numbers it compared."""

    rule: str
    passed: bool
    detail: str
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    rules: tuple[RuleResult, ...]
    min_gain: float
    provisional: bool = False  # offline, or the sensitivity precondition overridden
    unmeasured: bool = False  # a pure removal whose score held but whose removed content was not exercised enough

    def failed(self) -> tuple[str, ...]:
        return tuple(r.rule for r in self.rules if not r.passed)


@dataclass(frozen=True)
class SelfReview:
    """The incumbent reviewing the candidate's `policy/` diff (ARCHITECTURE.md section 7, step 2)."""

    important: int  # posted Important findings: any one rejects the candidate
    findings: tuple[Mapping[str, Any], ...] = ()  # every finding, with its bucket and reason
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return self.important == 0


@dataclass(frozen=True)
class CandidateRecord:
    id: str
    round: int
    proposal: Proposal
    parent_hash: str  # the incumbent it was proposed against
    policy_hash: str  # "" when the edit didn't produce a valid policy
    diff: str  # unified diff, paths relative to the project root (policy/...)
    outcome: Outcome
    note: str = ""
    pure_removal: bool = False
    self_review: SelfReview | None = None
    eval_run: str = ""  # the candidate's evaluation on the gate split
    gate: GateVerdict | None = None
    screen: Mapping[str, Any] | None = None  # the screen's numbers (`learn/gate.py` `screen`), when it ran
    created_at: str = ""
    backend: str = ""  # the LLM backend it was judged on: a verdict carries over to no other backend


@dataclass(frozen=True)
class PolicyRecord:
    """A promoted policy version: what it is, where it came from, why, and what the gate measured."""

    hash: str
    parent: str
    created_at: str
    rationale: str  # the candidate's hypothesis and change
    diff: str
    deltas: Mapping[str, Any]  # the gate's numbers
    provisional: bool  # promoted offline, or with the sensitivity precondition overridden
    files: Mapping[str, str]  # the whole policy, so any version can be restored
    candidate: str = ""


DIFF_CONTEXT = 3  # lines of context in a candidate's diff (the self-review and the changelog read it)


def policy_diff(old: Mapping[str, str], new: Mapping[str, str], prefix: str) -> str:
    """A git-style diff of two policy versions' files (paths relative to the policy directory), with `prefix` (the
    directory, relative to the project root) on every path."""
    out = []
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        full = prefix + path
        header = [f"diff --git a/{full} b/{full}"]
        if before is None:
            header += ["new file mode 100644", "--- /dev/null", f"+++ b/{full}"]
        elif after is None:
            header += ["deleted file mode 100644", f"--- a/{full}", "+++ /dev/null"]
        else:
            header += [f"--- a/{full}", f"+++ b/{full}"]
        body = patches.unified_diff(before or "", after or "", full, context=DIFF_CONTEXT)
        out.append("\n".join([*header, body]))
    return "\n".join(out) + ("\n" if out else "")


def changed_paths(diff: str) -> tuple[str, ...]:
    """Every path a diff touches, old and new names (relative to the project root)."""
    paths: list[str] = []
    for f in patches.parse_diff_file(diff):
        paths += [p for p in (f.path, f.previous_path) if p and p not in paths]
    return tuple(paths)


def _deletes_only(old: str, new: str) -> bool:
    """`new` is `old` with characters deleted (a subsequence of it)."""
    rest = iter(old)
    return all(ch in rest for ch in new)


def is_pure_removal(old: Mapping[str, str], new: Mapping[str, str], config_file: str) -> bool:
    """The candidate only deletes policy content (METRICS.md section 3, the exception for pure removals): no file
    added, prompts and lessons only lose text (a passage, a sentence, a lesson), and the config only loses whole lines
    (a changed value is not a removal, even when it is shorter)."""
    if set(new) - set(old) or new == old:
        return False
    for path, before in old.items():
        after = new.get(path, "")
        if after == before:
            continue
        if path == config_file:
            kept = iter(before.splitlines())
            if not all(line in kept for line in after.splitlines()):
                return False
        elif not _deletes_only(before, after):
            return False
    return True
