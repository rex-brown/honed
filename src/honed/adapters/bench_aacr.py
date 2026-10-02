"""AACR-Bench (Apache-2.0; https://github.com/alibaba/aacr-bench) as a `BenchmarkSource`.

Reads `dataset/positive_samples.json` (the comments the benchmark's annotators confirmed) and, when present,
`dataset/negative_samples.json` (the ones they rejected), from a download directory (or flat in it). Each entry:
{githubPrUrl, source_commit (the PR's base), target_commit (the reviewed head), project_main_language,
change_line_count, category, comments: [{note, path, side, from_line, to_line, category, context, is_ai_comment,
source_model}]}. A PR present only among the negatives has no golden comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from honed.adapters.bench_martian import parse_url
from honed.core.benchmarks import AACR, BenchmarkComment, BenchmarkPR
from honed.ports.benchmark import BenchmarkError

LICENSE = "Apache-2.0"
HOMEPAGE = "https://github.com/alibaba/aacr-bench"
REPO = "alibaba/aacr-bench"
FILES = ("dataset/positive_samples.json", "dataset/negative_samples.json")


def _comment(raw: dict[str, Any], *, valid: bool) -> BenchmarkComment:
    return BenchmarkComment(
        text=str(raw.get("note") or "").strip(), category=str(raw.get("category") or ""),
        path=str(raw.get("path") or ""), start_line=int(raw.get("from_line") or 0),
        end_line=int(raw.get("to_line") or 0), side=str(raw.get("side") or "right"), valid=valid,
    )  # fmt: skip


class AACRBenchmark:
    name, license, homepage = AACR, LICENSE, HOMEPAGE

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _file(self, name: str) -> Path | None:
        return next((p for p in (self._dir / "dataset" / name, self._dir / name) if p.exists()), None)

    def prs(self) -> list[BenchmarkPR]:
        positive = self._file("positive_samples.json")
        if positive is None:
            raise BenchmarkError(f"no AACR-Bench positive_samples.json under {self._dir}")
        negative = self._file("negative_samples.json")
        prs: dict[str, BenchmarkPR] = {}
        for path, valid in ((positive, True), *(((negative, False),) if negative else ())):
            for entry in json.loads(path.read_text()):
                url = entry["githubPrUrl"]
                repo, number = parse_url(url)
                comments = tuple(_comment(c, valid=valid) for c in entry.get("comments") or ())
                known = prs.get(url)
                if known is not None:
                    if (known.base_commit, known.head_commit) != (entry["source_commit"], entry["target_commit"]):
                        raise BenchmarkError(f"{url}: the positive and negative samples review other commits")
                    prs[url] = BenchmarkPR(**{**known.__dict__, "comments": known.comments + comments})
                    continue
                prs[url] = BenchmarkPR(
                    benchmark=AACR, url=url, repo=repo, number=number,
                    language=str(entry.get("project_main_language") or ""), base_commit=entry["source_commit"],
                    head_commit=entry["target_commit"], comments=comments,
                )  # fmt: skip
        return sorted(prs.values(), key=lambda pr: (pr.repo, pr.number))
