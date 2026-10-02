"""Martian Code Review Bench (MIT; https://github.com/withmartian/code-review-benchmark) as a `BenchmarkSource`.

Reads the offline set from a download directory: `golden_comments/*.json` (one file per source repo: a list of
{pr_title, url, original_url?, comments: [{comment, severity, category}]}) and, when present,
`benchmark_data.json` (keyed by PR URL: {pr_title, original_url, source_repo, golden_comments, reviews}), which is
checked against the golden files. The tools' reviews in `benchmark_data.json` are not used. Golden comments name no
file or line. The files may also sit flat in the directory (`<dir>/sentry.json`, ...), as `honed` downloads them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from honed.core.benchmarks import MARTIAN, BenchmarkComment, BenchmarkPR
from honed.ports.benchmark import BenchmarkError

LICENSE = "MIT"
HOMEPAGE = "https://github.com/withmartian/code-review-benchmark"
REPO = "withmartian/code-review-benchmark"
FILES = ("offline/golden_comments/{name}.json", "offline/results/benchmark_data.json")
GOLDEN = ("cal_dot_com", "discourse", "grafana", "keycloak", "sentry")
_PR_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")


def parse_url(url: str) -> tuple[str, int]:
    match = _PR_URL.match(url.strip())
    if not match:
        raise BenchmarkError(f"not a GitHub pull request URL: {url!r}")
    return match.group(1), int(match.group(2))


def _comments(raw: list[dict[str, Any]]) -> tuple[BenchmarkComment, ...]:
    return tuple(BenchmarkComment(text=str(c["comment"]).strip(), severity=str(c.get("severity") or ""),
                                  category=str(c.get("category") or "")) for c in raw)  # fmt: skip


class MartianBenchmark:
    name, license, homepage = MARTIAN, LICENSE, HOMEPAGE

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _golden_files(self) -> list[Path]:
        for folder in (self._dir / "golden_comments", self._dir / "offline" / "golden_comments", self._dir):
            found = sorted(p for p in folder.glob("*.json") if p.stem in GOLDEN)
            if found:
                return found
        raise BenchmarkError(f"no Martian golden_comments/*.json under {self._dir}")

    def prs(self) -> list[BenchmarkPR]:
        out: dict[str, BenchmarkPR] = {}
        for path in self._golden_files():
            for entry in json.loads(path.read_text()):
                repo, number = parse_url(entry["url"])
                out[entry["url"]] = BenchmarkPR(
                    benchmark=MARTIAN, url=entry["url"], repo=repo, number=number, title=entry.get("pr_title") or "",
                    original_url=entry.get("original_url") or "", comments=_comments(entry["comments"]),
                )  # fmt: skip
        places = (self._dir / "benchmark_data.json", self._dir / "offline" / "results" / "benchmark_data.json")
        combined = next((p for p in places if p.exists()), None)
        if combined is not None:
            for url, entry in json.loads(combined.read_text()).items():
                if url not in out:
                    raise BenchmarkError(f"{combined.name}: {url} is not in the golden comments")
                if len(entry.get("golden_comments") or ()) != len(out[url].comments):
                    raise BenchmarkError(f"{combined.name}: {url} has other golden comments than its golden file")
        return sorted(out.values(), key=lambda pr: (pr.repo, pr.number))
