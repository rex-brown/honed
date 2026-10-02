"""CodeHost on GitHub, through the `gh` CLI: GraphQL for PRs and threads, REST for compares and file contents
(REST does not spend GraphQL points)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from honed.adapters import github_parse as parse
from honed.adapters import github_queries as queries
from honed.adapters.gh_client import GhClient
from honed.core.filters import LANDED_BY_COMMIT, landed_qualifier
from honed.core.types import Compare, DateWindow, PRPage, PullRequest, RepoInfo, Thread
from honed.ports.code_host import MergedPR, NotFound

_SHRINK = ("threads", "comments")
_MORE_COMMENTS_PAGE = 50
_NODES_PER_QUERY = 100  # GitHub's limit for `nodes(ids:)`
_PRS_PER_QUERY = 50


class GitHubCodeHost:
    def __init__(self, client: GhClient, *, threads_page: int, comments_page: int, reactions_page: int) -> None:
        self._client = client
        self._threads_page = threads_page
        self._comments_page = comments_page
        self._reactions_page = reactions_page

    @property
    def points_used(self) -> int:
        return self._client.points_used

    @property
    def rest_calls(self) -> int:
        return self._client.rest_calls

    def repo_info(self, repo: str) -> RepoInfo:
        return parse.repo_info(repo, json.loads(self._client.rest(f"repos/{repo}")))

    def list_landed_prs(self, repo: str, window: DateWindow, *, after: str | None, page_size: int) -> PRPage:
        query = (
            f"repo:{repo} is:pr {landed_qualifier(repo)} created:{window} "
            "-author:app/dependabot -author:app/renovate sort:created-desc"
        )
        variables = {"q": query, "n": page_size, "after": after, "byCommit": repo in LANDED_BY_COMMIT}
        return parse.search_page(repo, self._client.graphql(queries.SEARCH_PRS, variables, shrink=("n",)))

    def fetch_pr(self, repo: str, number: int, *, thread_hint: int | None = None) -> PullRequest:
        owner, name = repo.split("/", 1)
        first = self._threads_page if thread_hint is None else max(1, min(self._threads_page, thread_hint))
        base = {"owner": owner, "name": name, "number": number, "reactions": self._reactions_page}
        data = self._client.graphql(
            queries.PULL_REQUEST, {**base, "threads": first, "comments": self._comments_page}, shrink=_SHRINK
        )
        node = (data.get("repository") or {}).get("pullRequest")
        if node is None:
            raise NotFound(f"{repo}#{number}")
        thread_nodes = list(node["reviewThreads"]["nodes"])
        page = node["reviewThreads"]["pageInfo"]
        while page.get("hasNextPage"):
            more = self._client.graphql(
                queries.MORE_THREADS,
                {**base, "threads": self._threads_page, "comments": self._comments_page, "after": page["endCursor"]},
                shrink=_SHRINK,
            )
            connection = more["repository"]["pullRequest"]["reviewThreads"]
            thread_nodes += connection["nodes"]
            page = connection["pageInfo"]
        threads = [self._thread(t) for t in thread_nodes if t]
        return parse.pull_request(repo, node, threads)

    def _thread(self, node: dict[str, Any]) -> Thread:
        comments = list(node["comments"]["nodes"])
        page = node["comments"]["pageInfo"]
        while page.get("hasNextPage"):
            more = self._client.graphql(
                queries.MORE_COMMENTS,
                {
                    "id": node["id"],
                    "comments": _MORE_COMMENTS_PAGE,
                    "after": page["endCursor"],
                    "reactions": self._reactions_page,
                },
                shrink=("comments",),
            )
            connection = more["node"]["comments"]
            comments += connection["nodes"]
            page = connection["pageInfo"]
        return parse.thread(node, [c for c in comments if c])

    def compare(self, repo: str, base: str, head: str) -> Compare:
        payload = json.loads(self._client.rest(f"repos/{repo}/compare/{base}...{head}?per_page=1"))
        return parse.compare(base, head, payload)

    def read_file(self, repo: str, path: str, commit: str) -> str | None:
        try:
            raw = self._client.rest(
                f"repos/{repo}/contents/{quote(path)}?ref={commit}", accept="application/vnd.github.raw+json"
            )
        except NotFound:
            return None
        if b"\0" in raw[:8192]:
            return None
        return raw.decode("utf-8", errors="replace")

    def list_merged_prs(self, repo: str, window: DateWindow, *, limit: int) -> list[MergedPR]:
        """REST issue search (no GraphQL points): merged PRs in `window`, newest first."""
        out: list[MergedPR] = []
        page = 1
        while len(out) < limit:
            query = urlencode({"q": f"repo:{repo} is:pr is:merged merged:{window}", "sort": "created",
                               "order": "desc", "per_page": min(100, limit), "page": page})  # fmt: skip
            items = json.loads(self._client.rest(f"search/issues?{query}")).get("items") or []
            for item in items:
                merged = (item.get("pull_request") or {}).get("merged_at") or item.get("closed_at") or ""
                labels = tuple(str(label.get("name", "")) for label in item.get("labels") or [])
                out.append(MergedPR(int(item["number"]), str(item.get("title", "")), labels, merged))
            if len(items) < min(100, limit):
                break
            page += 1
        return out[:limit]

    def merge_commit(self, repo: str, number: int) -> str | None:
        payload = json.loads(self._client.rest(f"repos/{repo}/pulls/{number}"))
        return payload.get("merge_commit_sha") if payload.get("merged") else None

    # ---- TextSource: a stripped bundle's text ----------------------------------------------------------------

    def comment_bodies(self, ids: Sequence[str]) -> Mapping[str, str | None]:
        out: dict[str, str | None] = dict.fromkeys(ids)
        unique = list(dict.fromkeys(ids))
        for start in range(0, len(unique), _NODES_PER_QUERY):
            batch = unique[start : start + _NODES_PER_QUERY]
            data = self._client.graphql(queries.COMMENT_BODIES, {"ids": batch}, partial=True)
            for node in data.get("nodes") or []:
                if node and node.get("id") in out:
                    out[node["id"]] = node.get("body") or ""
        return out

    def pr_bodies(self, repo: str, numbers: Sequence[int]) -> Mapping[int, str | None]:
        owner, name = repo.split("/", 1)
        out: dict[int, str | None] = dict.fromkeys(numbers)
        unique = sorted(set(numbers))
        for start in range(0, len(unique), _PRS_PER_QUERY):
            batch = unique[start : start + _PRS_PER_QUERY]
            data = self._client.graphql(queries.pr_bodies(batch), {"owner": owner, "name": name}, partial=True)
            found = data.get("repository") or {}
            for n in batch:
                node = found.get(f"pr{n}")
                out[n] = (node.get("body") or "") if node else None
        return out
