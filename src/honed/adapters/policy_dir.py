"""Reads a policy directory from disk and parses its `config.toml` and `lessons.yaml`; `core.policy` validates and
hashes the result. Also writes a policy out (a derived policy for inspection, a promoted one into `policy/`), and edits
the two structured files as text for the improve loop (`ports.policy.PolicyFiles`), so their comments and attribution
headers survive an edit.

`CHANGELOG.md` is the promote step's append-only log of the directory: it is not part of the policy, so it is never
read into one and never changes a policy's hash.
"""

from __future__ import annotations

import re
import textwrap
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from honed.core import policy as core_policy
from honed.core.policy import Policy, PolicyError, PolicyRules

CHANGELOG = "CHANGELOG.md"
_SKIPPED_NAMES = {".DS_Store", CHANGELOG}


def read_files(directory: Path) -> dict[str, str]:
    """Every text file under `directory`, keyed by its relative POSIX path (hidden files and the changelog skipped)."""
    if not directory.is_dir():
        raise PolicyError(f"no policy directory at {directory}")
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if not path.is_file() or path.name in _SKIPPED_NAMES or any(p.startswith(".") for p in relative.parts):
            continue
        files[relative.as_posix()] = path.read_text()
    return files


def parse(files: Mapping[str, str], rules: PolicyRules) -> Policy:
    try:
        config = tomllib.loads(files.get(core_policy.CONFIG_FILE, ""))
    except tomllib.TOMLDecodeError as error:
        raise PolicyError(f"{core_policy.CONFIG_FILE}: {error}") from None
    try:
        lessons = yaml.safe_load(files.get(core_policy.LESSONS_FILE, "")) if core_policy.LESSONS_FILE in files else None
    except yaml.YAMLError as error:
        raise PolicyError(f"{core_policy.LESSONS_FILE}: {error}") from None
    return core_policy.build(files, config, lessons, rules)


def load(directory: Path, rules: PolicyRules) -> Policy:
    return parse(read_files(directory), rules)


def write(directory: Path, policy: Policy) -> None:
    """Write every file of `policy` under `directory` (a derived policy, for inspection and re-runs)."""
    for relative, text in policy.files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def replace_files(directory: Path, old: Mapping[str, str], new: Mapping[str, str]) -> None:
    """Make `directory` hold `new` where it held `old`: write changed and added files, delete removed ones. Files the
    policy doesn't track (the changelog) are left alone."""
    for relative in sorted(set(old) - set(new)):
        (directory / relative).unlink(missing_ok=True)
    for relative, text in new.items():
        if old.get(relative) != text:
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)


def append_changelog(directory: Path, entry: str) -> None:
    """Append an entry to `policy/CHANGELOG.md` (append-only: earlier entries are never rewritten)."""
    path = directory / CHANGELOG
    if not path.exists():
        path.write_text("# Policy changelog\n\nWritten by `honed improve` when it promotes a candidate. "
                        "Append-only; not part of the policy or its hash.\n")  # fmt: skip
    with path.open("a") as handle:
        handle.write("\n" + entry.rstrip() + "\n")


# ---- text edits for the improve loop (ports.policy.PolicyFiles) ----------------------------------------------

_ITEM = re.compile(r"^(?P<indent>\s*)- id:\s*['\"]?(?P<id>[^'\"\s#]+)['\"]?\s*(#.*)?$")
_HEADER = re.compile(r"^\s*\[")
_KEY = re.compile(r"^(?P<lead>\s*)(?P<name>[A-Za-z_][\w-]*)\s*=\s*(?P<rest>.*)$")
_MEMBER_KEY = re.compile(r"^panel\.members\[(\d+)\]\.([A-Za-z_][\w-]*)$")


def _comment_at(rest: str) -> int | None:
    """Where the trailing comment starts in the text after `key =`: the first `#` before which the value parses."""
    for at in (i for i, ch in enumerate(rest) if ch == "#"):
        try:
            tomllib.loads("x = " + rest[:at])
        except tomllib.TOMLDecodeError:
            continue
        return at
    return None


def _dump_item(lesson: Mapping[str, Any], indent: str) -> list[str]:
    body = yaml.safe_dump([dict(lesson)], sort_keys=False, allow_unicode=True, width=1000, default_flow_style=False)
    return [line + "\n" for line in textwrap.indent(body, indent).splitlines()]


def _item_span(lines: list[str], lesson_id: str) -> tuple[int, int, str]:
    """(first line, line after the last non-blank line, indent) of a lesson's list item."""
    for start, line in enumerate(lines):
        match = _ITEM.match(line.rstrip("\n"))
        if match and match.group("id") == lesson_id:
            indent = match.group("indent")
            end = last = start + 1
            while end < len(lines):
                text = lines[end]
                if text.strip():
                    if len(text) - len(text.lstrip(" ")) <= len(indent):
                        break
                    last = end + 1
                end += 1
            return start, last, indent
    raise KeyError(f"no lesson {lesson_id!r} in {core_policy.LESSONS_FILE}")


class PolicyFiles:
    """`ports.policy.PolicyFiles` for the directory format this module reads."""

    def __init__(self, rules: PolicyRules) -> None:
        self._rules = rules

    def parse(self, files: Mapping[str, str]) -> Policy:
        return parse(files, self._rules)

    def add_lesson(self, text: str, lesson: Mapping[str, Any]) -> str:
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        indent = next((m.group("indent") for line in lines if (m := _ITEM.match(line.rstrip("\n")))), "  ")
        out = "".join(lines)
        if re.search(r"^lessons:\s*\[\s*\]\s*$", out, re.M):
            out = re.sub(r"^lessons:\s*\[\s*\]\s*$", "lessons:", out, count=1, flags=re.M) + "\n"
        elif not re.search(r"^lessons:", out, re.M):
            out += "lessons:\n"
        return out + ("\n" if out.strip() and not out.endswith(":\n") else "") + "".join(_dump_item(lesson, indent))

    def replace_lesson(self, text: str, lesson_id: str, lesson: Mapping[str, Any]) -> str:
        lines = text.splitlines(keepends=True)
        start, end, indent = _item_span(lines, lesson_id)
        return "".join(lines[:start] + _dump_item(lesson, indent) + lines[end:])

    def remove_lesson(self, text: str, lesson_id: str) -> str:
        lines = text.splitlines(keepends=True)
        start, end, _ = _item_span(lines, lesson_id)
        if end < len(lines) and not lines[end].strip() and (start == 0 or not lines[start - 1].strip()):
            end += 1  # one blank separator goes with the entry
        return "".join(lines[:start] + lines[end:])

    def set_config(self, text: str, key: str, value: Any) -> str:
        lines = text.splitlines(keepends=True)
        member = _MEMBER_KEY.match(key)
        if member:
            header, nth, name = "[[panel.members]]", int(member.group(1)), member.group(2)
        else:
            section, _, name = key.rpartition(".")
            if not section or not name:
                raise KeyError(f"config key {key!r}: expected section.name or panel.members[N].name")
            header, nth = f"[{section}]", 1
        seen = 0
        start = None
        for n, line in enumerate(lines):
            if line.split("#", 1)[0].strip() == header:
                seen += 1
                if seen == nth:
                    start = n
                    break
        if start is None:
            raise KeyError(f"config key {key!r}: no {header} section" + (f" number {nth}" if member else ""))
        end = next((n for n in range(start + 1, len(lines)) if _HEADER.match(lines[n])), len(lines))
        rendered = core_policy.toml_value(value)
        last_key = start
        for n in range(start + 1, end):
            match = _KEY.match(lines[n].rstrip("\n"))
            if not match:
                continue
            last_key = n
            if match.group("name") == name:
                rest = match.group("rest")
                at = _comment_at(rest)
                if at is None:
                    lines[n] = f"{match.group('lead')}{name} = {rendered}\n"
                else:
                    gap = max(1, at - len(rendered))  # keep the comment's column
                    lines[n] = f"{match.group('lead')}{name} = {rendered}{' ' * gap}{rest[at:]}\n"
                return "".join(lines)
        lines.insert(last_key + 1, f"{name} = {rendered}\n")
        return "".join(lines)


class PolicyDirectory:
    """`ports.policy.PolicyDirectory` on a directory on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def files(self) -> dict[str, str]:
        return read_files(self.path)

    def replace(self, old: Mapping[str, str], new: Mapping[str, str]) -> None:
        replace_files(self.path, old, new)

    def append_changelog(self, entry: str) -> None:
        append_changelog(self.path, entry)
