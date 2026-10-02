"""PolicyFiles: the policy directory's file formats (`config.toml`, `lessons.yaml`), for the improve loop, which
edits a policy as text so comments and attributions survive (ARCHITECTURE.md sections 3 and 7)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from honed.core.policy import Policy


class PolicyFiles(Protocol):
    def parse(self, files: Mapping[str, str]) -> Policy:
        """A validated policy from its files (path -> text); raises `PolicyError` (including the safety invariant)."""
        ...

    def add_lesson(self, text: str, lesson: Mapping[str, Any]) -> str:
        """`lessons.yaml` with the lesson appended to the list."""
        ...

    def replace_lesson(self, text: str, lesson_id: str, lesson: Mapping[str, Any]) -> str:
        """`lessons.yaml` with the lesson's entry replaced; raises KeyError when there is no such lesson."""
        ...

    def remove_lesson(self, text: str, lesson_id: str) -> str:
        """`lessons.yaml` without the lesson's entry; raises KeyError when there is no such lesson."""
        ...

    def set_config(self, text: str, key: str, value: Any) -> str:
        """`config.toml` with `key` (`section.name`, or `panel.members[N].name`, N from 1) set to `value`; raises
        KeyError for an unknown section or member."""
        ...


class PolicyDirectory(Protocol):
    """The incumbent policy's directory (`[paths] policy`), which only the promote step writes."""

    def files(self) -> dict[str, str]:
        """The policy's files, path relative to the directory -> text (the changelog is not one of them)."""
        ...

    def replace(self, old: Mapping[str, str], new: Mapping[str, str]) -> None:
        """Make the directory hold `new` where it held `old`."""
        ...

    def append_changelog(self, entry: str) -> None:
        """Append to the directory's changelog; earlier entries are never rewritten."""
        ...
