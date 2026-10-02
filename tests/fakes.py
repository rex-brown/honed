"""A CodeHost fake serving canned data, built by hand or from a recorded GitHub fixture."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from honed.adapters import github_parse as parse
from honed.core.types import Compare, DateWindow, PRPage, PRSummary, PullRequest, RepoInfo
from honed.ports.code_host import NotFound


class FakeCodeHost:
    def __init__(
        self,
        *,
        repos: dict[str, RepoInfo],
        pages: dict[str, list[tuple[PRSummary, ...]]],
        prs: dict[tuple[str, int], PullRequest],
        compares: dict[tuple[str, str], Compare] | None = None,
        files: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._repos, self._pages, self._prs = repos, pages, prs
        self.compares, self.files = compares or {}, files or {}
        self.calls: Counter[str] = Counter()

    @classmethod
    def from_fixture(cls, path: Path) -> FakeCodeHost:
        """Recorded payloads: repo_info[repo], search[repo] (a list of GraphQL `data` pages), prs["repo#n"] (a
        pullRequest node holding all its threads), compares["base...head"] (REST), files["path@commit"]."""
        fx = json.loads(path.read_text())
        prs = {}
        for key, node in fx["prs"].items():
            repo, number = key.split("#")
            threads = [parse.thread(t, t["comments"]["nodes"]) for t in node["reviewThreads"]["nodes"]]
            prs[(repo, int(number))] = parse.pull_request(repo, node, threads)
        compares = {}
        for key, payload in fx["compares"].items():
            base, head = key.split("...")
            compares[(base, head)] = parse.compare(base, head, payload)
        return cls(
            repos={repo: parse.repo_info(repo, payload) for repo, payload in fx["repo_info"].items()},
            pages={repo: [parse.search_page(repo, d).items for d in data] for repo, data in fx["search"].items()},
            prs=prs,
            compares=compares,
            files={tuple(key.rsplit("@", 1)): text for key, text in fx["files"].items()},  # type: ignore[misc]
        )

    def repo_info(self, repo: str) -> RepoInfo:
        self.calls["repo_info"] += 1
        return self._repos[repo]

    def list_landed_prs(self, repo: str, window: DateWindow, *, after: str | None, page_size: int) -> PRPage:
        self.calls["list"] += 1
        pages = self._pages.get(repo, [])
        index = int(after or 0)
        if index >= len(pages):
            return PRPage((), None, False)
        items = tuple(s for s in pages[index] if window.contains(s.created_at))
        return PRPage(items, str(index + 1), index + 1 < len(pages))

    def fetch_pr(self, repo: str, number: int, *, thread_hint: int | None = None) -> PullRequest:
        self.calls["fetch_pr"] += 1
        try:
            return self._prs[(repo, number)]
        except KeyError:
            raise NotFound(f"{repo}#{number}") from None

    def compare(self, repo: str, base: str, head: str) -> Compare:
        self.calls["compare"] += 1
        try:
            return self.compares[(base, head)]
        except KeyError:
            raise NotFound(f"{base}...{head}") from None

    def read_file(self, repo: str, path: str, commit: str) -> str | None:
        self.calls["read_file"] += 1
        return self.files.get((path, commit))
