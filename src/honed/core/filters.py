"""Which PRs and accounts count: bot, automation, AI-reviewer, AI-agent, backport and release-branch rules.

Ported from `research/repo-selection/measure_repos.py` (same regexes and sets, adapted to `Actor` and `PRSummary`),
which found them by inspecting 138 repos. Keep the two in sync by hand if the research rules change.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from honed.core.types import Actor, AuthorKind, PRSummary

EXCLUDE_TITLE = re.compile(
    r"^(rollup of|automated cherry pick|\[automated\]|bump |chore\(deps|update dependency|\[bot\])"
    r"|^(\s*\[[^\]]*\])*\s*\[[^\]]*(\d+\.(\d+|x)|release|backport)[^\]]*\]"  # "[3.15] ...", "[SPARK-1][SQL][4.1] ..."
    r"|^\s*\[(cp-[^\]]*|stable|beta|lts|v\d+-\d+(-\w+)?)\]"  # "[CP-stable] ...", "[stable] ...", "[v3-2-test] ..."
    r"|^(branch|release)-\d+(\.\d+)*\s*:"  # "branch-4.0: ...", "release-26.2: ..."
    r"|\(uplift to [^)]*\)|\bstable batch\b"  # "... (uplift to 1.93.x)", "v260 stable batch up to ..."
    r"|\b(backport|cherry[- ]?pick)\b|\U0001F352"  # Swift marks cherry-picks with a cherry emoji
    # release ceremony and branch syncs: "chore(release): 2.260.0", "Version Packages", "Release v8.2.0",
    # "v0.46.0", "nginx-1.30.2-RELEASE", "REL: ...", "chore: prepare Tokio v1.52.3", "Merge 'tokio-1.51.3' into
    # 'tokio-1.52.x'", "Merge main to features/x"
    r"|^(chore\((release|merge-back)\): *v?\d|\[ci\] (release|format)$|version packages\b|patch release$|(rel|rls)[:,]"
    r"|(chore: )?prepare (\w+ )?v?\d+\.\d+"
    r"|release[/ ]+v?\d+(\.\d+)+\S*$|v?\d+\.\d+\.\d+\S*$|\S+-\d+(\.\d+)+-release$"
    r"|merge (back\b|(main|master)$|v?\d+(\.\d+)+$|(release |branch )?[`'\"]?(main|master|[\w.-]*(\d|/|release|stable|feature)[\w./-]*)"  # noqa: E501
    r"[`'\"]?( branch)? (in)?to [`'\"]?[\w./-]+[`'\"]?( branch)?$)"
    # automation that runs under a human-typed account: dependency rolls, l10n syncs, generated SDKs
    r"|roll \S+ from [0-9a-f]{7,} to [0-9a-f]{7,}|localization updates\b|translations update from|\[autopr\b)",
    re.I,
)
BOT_LOGIN = re.compile(
    r"(\[bot\]$|bot$|-robot$|^robot-|^bors$|^homu$|^rustbot$|^codecov|^netlify|^vercel$|^miss-islington$|^trop$"
    r"|machine$|autoroll|-automation$|-builds?$|-publish$|^publish-|-devprod$|-service$|-integration$|wpt-sync"
    r"|^azure-sdk$|^hc-github-team-|^consvc$|^weblate$|-teamcity$)",
    re.I,
)
# Release-branch PRs that the title does not mark. A PR is treated as one when it targets a branch other than the
# development branch and that branch is a maintenance line ("v1.82.x", "43-x-y", "maintenance/2.5.x"), or the
# title is prefixed with the branch ("tentacle: ..."), names its version ("... (1.93.x)", "[4.1]", "on 25.8"), or
# ends with the original PR's number ("Fix X (#31609)") on a release-like branch. Fix-first-on-stable workflows
# (php-src PHP-8.x, duckdb v1.5-*, nestjs/apollo next-version branches) are deliberately NOT caught.
DEV_BRANCHES = frozenset({"main", "master", "trunk", "develop", "dev", "canary", "next", "unstable"})
RELEASE_BASE = re.compile(r"release|stable|maint|hotfix|lts|^rc$|^branch|\d+[._-](\d+|x)", re.I)
MAINT_LINE = re.compile(r"[.-]x(-y|-dev)?$|^maintenance/", re.I)
HUMAN_LOGIN = frozenset({"paleolimbot"})  # people whose login happens to match BOT_LOGIN
# Coding-agent accounts. Their PRs stay in the sample (human review of agent code is still review); their own review
# comments count as AI, not human. Includes personal agent accounts (Deno's divybot etc., Bun's robobun "farm",
# ClickHouse's groeneai/clickgapai, Astro's Houston triage bot, Brave's netzenbot).
AI_AGENT_LOGIN = re.compile(
    r"^(copilot-swe-agent|copilot|claude|cursor|devin-ai-integration|chatgpt-codex-connector|codex|google-labs-jules"
    r"|openhands(-agent)?|sweep-ai|agent-sandbox-\S+|robobun|groeneai|clickgapai|astrobot-houston|netzenbot|divybot"
    r"|crowlbot|nathanwhitbot|zaniebot)$",
    re.I,
)
# Repos that land by closing the PR (merge scripts, Skara, merge bots, Meta's Phabricator export) rather than
# GitHub's merge: the search qualifier that selects landed PRs.
LANDED = {
    "pytorch/pytorch": "is:closed label:Merged",
    "openjdk/jdk": "is:closed label:integrated",
    "apache/spark": "is:closed review:approved",
    "openssl/openssl": "is:closed review:approved",
    "facebook/rocksdb": "is:closed label:Merged",
    "react/react-native": "is:closed label:Merged",
    "curl/curl": "is:closed",
    "vim/vim": "is:closed",
}
LANDED_BY_COMMIT = frozenset({"curl/curl", "vim/vim"})  # no label: keep only PRs closed by the landing commit
RENAMED = {
    "facebook/react": "react/react",
    "facebook/react-native": "react/react-native",
    "prisma/prisma": "prisma/orm",
}
AI_LOGIN = re.compile(
    r"copilot|coderabbit|gemini|greptile|sourcery|codex|claude|cursor|qodo|ellipsis|graphite|korbit|cubic|bito|devin"
    r"|augment|bonk|macroscope|depthfirst|review-bot|cat-bot|^clickhouse-gh$|^vercel$|^sentry$",
    re.I,
)
# Repos whose LLM review workflow posts inline threads as the generic github-actions account.
AI_REVIEW_VIA_ACTIONS = frozenset({"systemd/systemd", "elastic/kibana", "apache/doris", "dotnet/runtime"})


# --------------------------------------------------------------------------------------------------------------
# Repos
# --------------------------------------------------------------------------------------------------------------


def canonical_repo(repo: str) -> str:
    """The repo's current name (search does not follow renames), lower-cased for comparison."""
    lowered = repo.strip().lower()
    renamed = {old.lower(): new.lower() for old, new in RENAMED.items()}
    return renamed.get(lowered, lowered)


def is_excluded(repo: str, excluded: Iterable[str]) -> bool:
    """True when `repo`, or its renamed form, is on the exclusion list (case-insensitive)."""
    return canonical_repo(repo) in {canonical_repo(r) for r in excluded}


def landed_qualifier(repo: str) -> str:
    """The search qualifier selecting PRs that landed, honoring the repo's landing convention."""
    return LANDED.get(repo, "is:merged")


# --------------------------------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------------------------------


def is_agent(actor: Actor | None) -> bool:
    return actor is not None and bool(AI_AGENT_LOGIN.search(actor.login))


def is_bot(actor: Actor | None) -> bool:
    """Automation of any kind, including coding agents. A missing (deleted) account counts as a bot."""
    if actor is None:
        return True
    return actor.login not in HUMAN_LOGIN and (
        actor.typename == "Bot" or bool(BOT_LOGIN.search(actor.login)) or is_agent(actor)
    )


def is_ai(actor: Actor | None, repo: str | None = None) -> bool:
    """An AI reviewer or coding agent. Copilot's reviewer is typed as a User, hence the login check."""
    if actor is None:
        return False
    if repo in AI_REVIEW_VIA_ACTIONS and actor.login == "github-actions":
        return True
    return (bool(AI_LOGIN.search(actor.login)) or is_agent(actor)) and (
        is_bot(actor) or "copilot" in actor.login.lower()
    )


def author_kind(actor: Actor | None, repo: str | None = None) -> AuthorKind:
    if is_ai(actor, repo):
        return AuthorKind.AI
    return AuthorKind.BOT if is_bot(actor) else AuthorKind.HUMAN


def thread_author_kind(opener: Actor | None, pr_author: Actor | None, repo: str) -> AuthorKind:
    """Who opened a review thread. A thread the PR author opened is `PR_AUTHOR`, not review."""
    if opener is not None and pr_author is not None and opener.login == pr_author.login:
        return AuthorKind.PR_AUTHOR
    return author_kind(opener, repo)


def pr_author_kind(actor: Actor | None) -> AuthorKind:
    """PR authors are humans or coding agents; other automation is filtered out before this is asked."""
    return AuthorKind.AI if is_agent(actor) else author_kind(actor)


# --------------------------------------------------------------------------------------------------------------
# PRs
# --------------------------------------------------------------------------------------------------------------


def keep_pr(title: str, author: Actor | None) -> bool:
    """Humans' and coding agents' PRs, minus release ceremony, backports and automation by title."""
    return (is_agent(author) or not is_bot(author)) and not EXCLUDE_TITLE.search(title)


def on_release_branch(title: str, base_ref: str | None, default_branch: str | None) -> bool:
    """See `RELEASE_BASE`: an unmarked backport or maintenance-line PR, judged from its base branch."""
    base = base_ref
    if not base or not default_branch or base == default_branch or base in DEV_BRANCHES:
        return False
    if re.match(r"feat(ure)?[/-]", base):
        return False
    lowered = title.strip().lower()
    ver = re.search(r"\d+(?:[._-](?:\d+|x))+(?:-y)?", base)
    return bool(
        MAINT_LINE.search(base)
        or lowered.startswith(base.lower() + ":")
        or (ver and re.search(rf"(?<![\w.]){re.escape(ver.group(0).lower())}(?!\w|\.\d)", lowered))
        or (RELEASE_BASE.search(base) and re.search(r"\(#\d+\)$", lowered))
    )


def _normalized_title(title: str) -> str:
    return re.sub(r"\s*\(?#\d+\)?\s*$", "", title).strip().lower()


def drop_branch_copies(prs: Sequence[PRSummary]) -> list[PRSummary]:
    """Drop same-title copies aimed at a different branch (manual cherry-picks: "Fix X (#123)" reopened against
    release-11.7). The earliest-created copy is kept, as are copies aimed at the same branch. Order is preserved."""
    first: dict[str, PRSummary] = {}
    for pr in sorted(prs, key=lambda p: p.created_at):
        first.setdefault(_normalized_title(pr.title), pr)
    return [
        pr
        for pr in prs
        if first[_normalized_title(pr.title)] is pr or first[_normalized_title(pr.title)].base_ref == pr.base_ref
    ]


def reviewed_by_human(pr: PRSummary) -> bool:
    """A human other than the author submitted a review (opening a review thread submits one) in the listing."""
    author = pr.author.login if pr.author else None
    humans = [a for a in pr.review_authors if not is_bot(a) and a is not None and a.login != author]
    return bool(humans)


def reviewed_by_ai(pr: PRSummary) -> bool:
    return any(is_ai(a, pr.repo) for a in pr.review_authors)


def changes_requested_by_human(pr: PRSummary) -> bool:
    """A human other than the author submitted a CHANGES_REQUESTED review (the bug-targeted sample). GitHub's
    `review:changes_requested` search qualifier can't select these: it matches the current review decision, which a
    later approval replaces, so it finds almost no merged PRs."""
    author = pr.author.login if pr.author else None
    return any(a is not None and a.login != author and author_kind(a, pr.repo) is AuthorKind.HUMAN
               for a in pr.changes_requested_by)  # fmt: skip
