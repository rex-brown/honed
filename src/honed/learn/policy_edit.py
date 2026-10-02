"""Turns a proposed `PolicyEdit` into the candidate policy's files, and checks the result loads (ARCHITECTURE.md
section 7). Files are edited as text through `ports.policy.PolicyFiles`, so comments and attribution headers survive.

- Lessons: an added lesson starts as `candidate` (its lifecycle is driven by production outcomes, METRICS.md section 4,
  never by the proposer) with no stats; a changed lesson keeps its confidence and stats.
- Prompts: an exact passage found once in an existing prompt file is replaced; the file's attribution header stays.
  No new prompt files, and never the self-review lens (`prompts/policy_change.md`): a candidate must not loosen the
  review that judges candidates, so like the judge's prompts it changes only by hand.
- Config: keys of `config.toml` are set one by one.
Loading the result enforces every policy rule, the safety invariant included (`core.policy`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from honed.core.improve import EditKind, PolicyEdit
from honed.core.policy import CONFIG_FILE, LESSONS_FILE, PROMPT_DIR, SELF_REVIEW_PROMPTS, Policy, PolicyError
from honed.ports.policy import PolicyFiles

_LESSON_FIELDS = ("id", "kind", "scope", "categories", "text", "applies_when", "skip_when", "do_not_skip_when",
                  "example_signal", "evidence", "check")  # fmt: skip


SELF_REVIEW_FILES = frozenset(f"{PROMPT_DIR}{name}.md" for name in SELF_REVIEW_PROMPTS)


class EditError(ValueError):
    """The edit can't be applied, or the policy it makes doesn't load."""


def _lesson(raw: Mapping[str, Any] | None, *, keep: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not raw.get("id"):
        raise EditError("a lesson edit needs the whole lesson, with its id")
    out = {k: raw[k] for k in _LESSON_FIELDS if raw.get(k) not in (None, "", [], {})}
    out["confidence"] = (keep or {}).get("confidence", "candidate")
    if keep and keep.get("stats"):
        out["stats"] = keep["stats"]
    return out


def _existing(policy: Policy, lesson_id: str) -> dict[str, Any]:
    lesson = next((item for item in policy.lessons if item.id == lesson_id), None)
    if lesson is None:
        raise EditError(f"no lesson {lesson_id!r} in the incumbent")
    out: dict[str, Any] = {"confidence": lesson.confidence.value}
    if lesson.stats.fires or lesson.stats.precision is not None:
        out["stats"] = {"fires": lesson.stats.fires, "precision": lesson.stats.precision}
    return out


def apply(policy: Policy, edit: PolicyEdit, codec: PolicyFiles) -> tuple[dict[str, str], Policy]:
    """The candidate's files and the policy they load as. Raises `EditError`."""
    files = dict(policy.files)
    try:
        if edit.kind is EditKind.LESSON_ADD:
            lesson = _lesson(edit.lesson)
            if any(item.id == lesson["id"] for item in policy.lessons):
                raise EditError(f"lesson {lesson['id']!r} exists: propose a lesson_change to strengthen it")
            files[LESSONS_FILE] = codec.add_lesson(files[LESSONS_FILE], lesson)
        elif edit.kind is EditKind.LESSON_CHANGE:
            lesson = _lesson(edit.lesson, keep=_existing(policy, edit.lesson_id))
            if lesson["id"] != edit.lesson_id:
                raise EditError("a lesson_change keeps the lesson's id")
            files[LESSONS_FILE] = codec.replace_lesson(files[LESSONS_FILE], edit.lesson_id, lesson)
        elif edit.kind is EditKind.LESSON_REMOVE:
            _existing(policy, edit.lesson_id)
            files[LESSONS_FILE] = codec.remove_lesson(files[LESSONS_FILE], edit.lesson_id)
        elif edit.kind is EditKind.PROMPT_REPLACE:
            path = edit.file.removeprefix("policy/")
            files[path] = _replace(files, path, edit)
        elif edit.kind is EditKind.CONFIG_SET:
            if not edit.settings:
                raise EditError("a config_set edit needs at least one key")
            for key, value in edit.settings.items():
                files[CONFIG_FILE] = codec.set_config(files[CONFIG_FILE], key, value)
        else:
            raise EditError(f"unknown edit kind {edit.kind!r}")
    except KeyError as error:
        raise EditError(str(error).strip("'\"")) from None
    if files == dict(policy.files):
        raise EditError("the edit changes nothing")
    try:
        return files, codec.parse(files)
    except PolicyError as error:
        raise EditError(f"the edited policy doesn't load: {error}") from None


def _replace(files: Mapping[str, str], path: str, edit: PolicyEdit) -> str:
    if not (path.startswith(PROMPT_DIR) and path.endswith(".md")) or path not in files:
        raise EditError(f"prompt_replace edits an existing prompt file under {PROMPT_DIR}, not {edit.file!r}")
    if path in SELF_REVIEW_FILES:
        raise EditError(f"{path} is the self-review lens: the improve loop may not edit it")
    text = files[path]
    if not edit.old.strip() or text.count(edit.old) != 1:
        raise EditError(f"the passage to replace must occur exactly once in {path} (found {text.count(edit.old)})")
    out = text.replace(edit.old, edit.new, 1)
    header = text.splitlines()[0] if text.startswith("<!--") else ""
    if header and not out.startswith(header):
        raise EditError(f"{path}: the attribution header stays as it is")
    return out
