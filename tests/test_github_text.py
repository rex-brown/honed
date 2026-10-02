"""The GitHub side of `rehydrate --comments`: review-comment bodies by node id and PR descriptions by number, batched
GraphQL queries that tolerate objects GitHub no longer has. The `gh` CLI is faked."""

from __future__ import annotations

import json
import subprocess

import pytest

from honed.adapters.gh_client import GhClient, GhOptions
from honed.adapters.github import GitHubCodeHost
from honed.ports.code_host import HostError

RATE = {"cost": 1, "remaining": 4999, "resetAt": "2026-10-01T00:00:00Z"}


class FakeGh:
    """Answers `gh api graphql`: known comment ids and PR numbers have text; others are NOT_FOUND."""

    def __init__(self, comments: dict[str, str], prs: dict[int, str], error_type: str = "NOT_FOUND") -> None:
        self.comments, self.prs, self.error_type, self.queries = comments, prs, error_type, []

    def __call__(self, args, *, input, **kwargs):
        payload = json.loads(input)
        self.queries.append(payload)
        variables = payload["variables"]
        errors = []
        if "ids" in variables:
            nodes = [{"id": i, "body": self.comments[i]} if i in self.comments else None for i in variables["ids"]]
            errors = [{"type": self.error_type, "path": ["nodes", n]} for n, node in enumerate(nodes) if node is None]
            data = {"rateLimit": RATE, "nodes": nodes}
        else:
            lines = [part for part in payload["query"].split("\n") if "pullRequest(" in part]
            numbers = [int(part.split(":")[0].strip()[2:]) for part in lines]
            repo = {f"pr{n}": {"body": self.prs[n]} if n in self.prs else None for n in numbers}
            errors = [{"type": self.error_type, "path": ["repository", f"pr{n}"]} for n in numbers if n not in self.prs]
            data = {"rateLimit": RATE, "repository": repo}
        body = json.dumps({"data": data, **({"errors": errors} if errors else {})})
        return subprocess.CompletedProcess(args, 1 if errors else 0, body, "")


def host(fake: FakeGh) -> GitHubCodeHost:
    options = GhOptions(max_retries=0, backoff_s=0, min_remaining=0, point_budget=100, min_page=5, timeout_s=10)
    return GitHubCodeHost(GhClient(options, run=fake, sleep=lambda s: None), threads_page=5, comments_page=5,
                          reactions_page=5)  # fmt: skip


def test_comment_bodies_come_back_by_id_in_batches_of_100_with_deleted_ones_as_none():
    ids = [f"PRRC_{n}" for n in range(150)]
    fake = FakeGh({i: f"text {i}" for i in ids if i != "PRRC_7"}, {})
    found = host(fake).comment_bodies([*ids, "PRRC_3"])
    assert found["PRRC_3"] == "text PRRC_3" and found["PRRC_7"] is None and len(found) == 150
    assert [len(q["variables"]["ids"]) for q in fake.queries] == [100, 50]  # one id asked once


def test_pr_bodies_come_back_by_number_with_missing_ones_as_none():
    fake = FakeGh({}, {1: "first", 3: ""})
    assert host(fake).pr_bodies("o/r", [1, 2, 3]) == {1: "first", 2: None, 3: ""}
    assert fake.queries[0]["variables"] == {"owner": "o", "name": "r"}


def test_other_errors_still_fail():
    with pytest.raises(HostError):
        host(FakeGh({}, {}, error_type="FORBIDDEN")).comment_bodies(["PRRC_1"])
