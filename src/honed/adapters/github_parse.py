"""GitHub JSON (GraphQL nodes and REST payloads) to core types. Pure functions, shared with test fakes."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from honed.core.types import (
    Actor,
    Comment,
    CommitInfo,
    Compare,
    FilePatch,
    PRPage,
    PRSummary,
    PullRequest,
    Reaction,
    RepoInfo,
    Review,
    Thread,
)

Json = dict[str, Any]


def actor(node: Json | None) -> Actor | None:
    if not node or not node.get("login"):
        return None
    return Actor(login=node["login"], typename=node.get("__typename", "User"))


def _oid(node: Json | None) -> str | None:
    return node.get("oid") if node else None


def repo_info(repo: str, payload: Json) -> RepoInfo:
    license_ = payload.get("license") or {}
    return RepoInfo(name=repo, default_branch=payload.get("default_branch"), language=payload.get("language"),
                    license=license_.get("spdx_id") if isinstance(license_, dict) else None)  # fmt: skip


def search_page(repo: str, data: Json) -> PRPage:
    search = data["search"]
    items = []
    for node in search["nodes"]:
        if not node or "number" not in node:
            continue
        closers = [(n.get("closer") or {}).get("__typename") for n in (node.get("closed") or {}).get("nodes", [])]
        items.append(
            PRSummary(
                repo=repo,
                number=node["number"],
                title=node["title"],
                author=actor(node.get("author")),
                created_at=node["createdAt"],
                base_ref=node.get("baseRefName") or "",
                thread_count=(node.get("reviewThreads") or {}).get("totalCount", 0),
                review_authors=tuple(actor(r.get("author")) for r in (node.get("reviews") or {}).get("nodes", [])),
                closed_by_commit="Commit" in closers,
                changes_requested_by=tuple(
                    actor(r.get("author")) for r in (node.get("changesRequested") or {}).get("nodes", [])
                ),
            )
        )
    info = search["pageInfo"]
    return PRPage(items=tuple(items), end_cursor=info.get("endCursor"), has_next=bool(info.get("hasNextPage")))


def comment(node: Json) -> Comment:
    reactions = node.get("reactions") or {}
    return Comment(
        id=node["id"],
        author=actor(node.get("author")),
        body=node.get("body") or "",
        created_at=node["createdAt"],
        diff_hunk=node.get("diffHunk") or "",
        commit=_oid(node.get("commit")),
        original_commit=_oid(node.get("originalCommit")),
        line=node.get("line"),
        original_line=node.get("originalLine"),
        start_line=node.get("startLine"),
        original_start_line=node.get("originalStartLine"),
        reactions=tuple(
            Reaction(content=r["content"], user=(r.get("user") or {}).get("login")) for r in reactions.get("nodes", [])
        ),
        reaction_count=reactions.get("totalCount", 0),
    )


def thread(node: Json, comments: list[Json]) -> Thread:
    """A thread from its node plus all of its comment nodes (the first page and any fetched after it)."""
    return Thread(
        id=node["id"],
        path=node["path"],
        comments=tuple(comment(c) for c in comments),
        is_resolved=bool(node.get("isResolved")),
        is_outdated=bool(node.get("isOutdated")),
        diff_side=node.get("diffSide") or "RIGHT",
        subject_type=node.get("subjectType") or "LINE",
        line=node.get("line"),
        original_line=node.get("originalLine"),
        start_line=node.get("startLine"),
        original_start_line=node.get("originalStartLine"),
        resolved_by=(node.get("resolvedBy") or {}).get("login"),
        comment_count=(node.get("comments") or {}).get("totalCount", len(comments)),
    )


def pull_request(repo: str, node: Json, threads: list[Thread]) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=node["number"],
        title=node["title"],
        author=actor(node.get("author")),
        created_at=node["createdAt"],
        landed_at=node.get("mergedAt") or node.get("closedAt"),
        base_ref=node.get("baseRefName") or "",
        base_oid=node.get("baseRefOid") or "",
        head_oid=node.get("headRefOid") or "",
        url=node.get("url") or "",
        body=node.get("body") or "",
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        changed_files=node.get("changedFiles") or 0,
        force_pushes=(node.get("forcePushes") or {}).get("filteredCount", 0),
        threads=tuple(threads),
        reviews=tuple(
            Review(
                id=r["id"],
                author=actor(r.get("author")),
                state=r.get("state") or "",
                submitted_at=r.get("submittedAt"),
                commit=_oid(r.get("commit")),
            )
            for r in node["reviews"]["nodes"]
        ),
        commits=tuple(
            CommitInfo(
                oid=c["commit"]["oid"],
                committed_date=c["commit"].get("committedDate") or "",
                authored_date=c["commit"].get("authoredDate") or "",
                message_headline=c["commit"].get("messageHeadline") or "",
            )
            for c in node["commits"]["nodes"]
        ),
        commit_count=node["commits"].get("totalCount", 0),
    )


# GitHub lists at most this many files in a compare.
COMPARE_FILE_CAP = 300


def compare(base: str, head: str, payload: Json) -> Compare:
    files = payload.get("files") or []
    return Compare(
        base=base,
        head=head,
        status=payload.get("status") or "",
        merge_base=(payload.get("merge_base_commit") or {}).get("sha"),
        files=tuple(
            FilePatch(
                path=f["filename"],
                status=f.get("status") or "",
                patch=f.get("patch"),
                previous_path=f.get("previous_filename"),
            )
            for f in files
        ),
        complete=len(files) < COMPARE_FILE_CAP,
    )


# ---- permalinks ------------------------------------------------------------------------------------------------

_UINT = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}  # msgpack unsigned integer markers -> byte widths


def _msgpack_uints(data: bytes) -> list[int]:
    """The unsigned integers of a msgpack array of them (what GitHub's node ids encode)."""
    if not data or data[0] & 0xF0 != 0x90:
        return []
    out, i = [], 1
    for _ in range(data[0] & 0x0F):
        if i >= len(data):
            return []
        marker = data[i]
        if marker < 0x80:
            out.append(marker)
            i += 1
        elif marker in _UINT:
            width = _UINT[marker]
            out.append(int.from_bytes(data[i + 1 : i + 1 + width], "big"))
            i += 1 + width
        else:
            return []
    return out


def comment_database_id(node_id: str) -> int | None:
    """A review comment's database id from its GraphQL node id: `PRRC_<base64url msgpack [0, repo id, id]>`, or the
    legacy base64 of `...PullRequestReviewComment<id>`."""
    prefix, _, rest = node_id.partition("_")
    try:
        if prefix == "PRRC" and rest:
            numbers = _msgpack_uints(base64.urlsafe_b64decode(rest + "=" * (-len(rest) % 4)))
            return numbers[-1] if len(numbers) == 3 else None
        legacy = re.search(r"PullRequestReviewComment(\d+)$", base64.b64decode(node_id + "=" * (-len(node_id) % 4))
                           .decode("latin-1"))  # fmt: skip
        return int(legacy.group(1)) if legacy else None
    except (binascii.Error, ValueError):
        return None


def discussion_url(pr_url: str, comment_id: str) -> str:
    """The permalink of a review thread (`<pr url>#discussion_r<id>`, as GitHub's own `url` field gives it), or the
    PR's changed-files page when the comment id can't be read."""
    database_id = comment_database_id(comment_id)
    return f"{pr_url}#discussion_r{database_id}" if database_id is not None else f"{pr_url}/files"
