"""The yardstick's human-labeling files (`[paths] human_labels`, committed: `yardstick/human_labels/`), part of the
fixed measure (METRICS.md section 5):

- `sample.json`: the blind sample (`honed export-audit-sample`): `{generated_at, note, items: [{id, repo, pr, path,
  language, comment, author, url, commit, lines}]}`. No code: the labeling page fetches it from GitHub at view time.
- `labels-<username>.json`: one person's verdicts, as the labeling page (`honed human-labels serve`) saves them:
  `{labeler, labeled_at, honed_version, items: [{id, verdict, note}]}`, verdict `valid`, `not_valid` or `unsure`.
  One file per person, added by pull request; `honed human-labels check` validates them and `honed audit-judge`
  reads every one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from honed.core.consensus import HumanLabel, HumanVerdict

SAMPLE_NAME = "sample.json"
LABELS_GLOB = "labels-*.json"
# GitHub's rule for a login: letters, digits and single hyphens, not at either end, at most 39 characters.
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
VERDICTS = frozenset(v.value for v in HumanVerdict)


class LabelsError(ValueError):
    def __init__(self, path: Path, problems: Sequence[str]) -> None:
        super().__init__(f"{path.name}: " + "; ".join(problems))
        self.path = path
        self.problems = tuple(problems)


@dataclass(frozen=True)
class SampleRef:
    """Where a sample item's review comment lives in the store."""

    id: str
    repo: str
    pr: int


@dataclass(frozen=True)
class LabelsFile:
    path: Path
    labeler: str
    labeled_at: str
    honed_version: str
    labels: tuple[HumanLabel, ...]


def load_sample(path: Path) -> dict[str, SampleRef]:
    """The sample's items by id; none when the file doesn't exist."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    refs = (SampleRef(str(e["id"]), str(e["repo"]), int(e["pr"])) for e in data.get("items", []))
    return {ref.id: ref for ref in refs}


def label_paths(directory: Path) -> list[Path]:
    return sorted(directory.glob(LABELS_GLOB))


def problems(data: Any, *, file_name: str, sample_ids: Collection[str]) -> list[str]:
    """What is wrong with one labels file's content (nothing: it is valid)."""
    if not isinstance(data, Mapping):
        return ["expected a JSON object {labeler, labeled_at, honed_version, items}"]
    out = []
    labeler = data.get("labeler")
    if not isinstance(labeler, str) or not LOGIN.fullmatch(labeler):
        out.append(f"`labeler` must be a GitHub username, not {labeler!r}")
    elif file_name.lower() != f"labels-{labeler.lower()}.json":
        out.append(f"the file must be named labels-{labeler}.json")
    for key in ("labeled_at", "honed_version"):
        if not isinstance(data.get(key), str):
            out.append(f"`{key}` must be text")
    items = data.get("items")
    if not isinstance(items, list):
        return [*out, "`items` must be a list of {id, verdict, note}"]
    seen: set[str] = set()
    for n, entry in enumerate(items, 1):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            out.append(f"item {n}: needs an `id`")
            continue
        lid, verdict, note = entry["id"], entry.get("verdict"), entry.get("note", "")
        if lid not in sample_ids:
            out.append(f"item {n}: {lid!r} is not in the sample")
        elif lid in seen:
            out.append(f"item {n}: {lid!r} appears twice")
        elif verdict not in VERDICTS:
            out.append(f"item {n}: verdict {verdict!r} is not one of {sorted(VERDICTS)}")
        elif not isinstance(note, str | None):
            out.append(f"item {n}: `note` must be text")
        seen.add(lid)
    return out


def read(path: Path, sample_ids: Collection[str]) -> LabelsFile:
    """One labels file, validated against the sample's ids (LabelsError lists every problem)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LabelsError(path, [str(error)]) from error
    found = problems(data, file_name=path.name, sample_ids=sample_ids)
    if found:
        raise LabelsError(path, found)
    labeler = data["labeler"]
    labels = tuple(
        HumanLabel(e["id"], labeler, HumanVerdict(e["verdict"]), str(e.get("note") or "")) for e in data["items"]
    )
    return LabelsFile(path, labeler, data["labeled_at"], data["honed_version"], labels)


def load_all(directory: Path, sample_ids: Collection[str]) -> tuple[list[LabelsFile], list[LabelsError]]:
    """Every labels file in `directory`: the valid ones, and what is wrong with the others. A second file by the same
    labeler is an error (one file per person)."""
    files, errors, by_labeler = [], [], {}
    for path in label_paths(directory):
        try:
            labels = read(path, sample_ids)
        except LabelsError as error:
            errors.append(error)
            continue
        key = labels.labeler.lower()
        if key in by_labeler:
            errors.append(LabelsError(path, [f"{by_labeler[key].name} is already {labels.labeler}'s file"]))
            continue
        by_labeler[key] = path
        files.append(labels)
    return files, errors
