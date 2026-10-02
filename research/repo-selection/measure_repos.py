#!/usr/bin/env python3
"""Measure how rigorous each repo's GitHub PR review is, from a sample of merged PRs.

Stdlib only; talks to GitHub through `gh api graphql`. Raw responses are cached per repo
so re-runs only fetch repos not yet measured or whose cache is stale (see `is_stale`); deleting a
repo's raw file forces a refetch.

Usage: measure_repos.py OUT_DIR owner/repo [owner/repo ...]
"""
import concurrent.futures as cf
import json
import re
import statistics as st
import subprocess
import sys
import time
from pathlib import Path

WINDOW = "2026-03-01..2026-06-30"      # PRs CREATED in this window (and since merged); ends 3 months ago so
                                      # slow, heavily reviewed PRs are included, not just fast merges
VOLUME_WINDOW = "2025-09-29..2026-09-29"
TARGET_PRS = 60
MIN_SAMPLE = 50          # refetch a cached repo whose filtered sample falls below this, if the search had more
EXCLUDE_TITLE = re.compile(
    r"^(rollup of|automated cherry pick|\[automated\]|bump |chore\(deps|update dependency|\[bot\])"
    r"|^(\s*\[[^\]]*\])*\s*\[[^\]]*(\d+\.(\d+|x)|release|backport)[^\]]*\]"   # "[3.15] ...", "[SPARK-1][SQL][4.1] ..."
    r"|^\s*\[(cp-[^\]]*|stable|beta|lts|v\d+-\d+(-\w+)?)\]"       # "[CP-stable] ...", "[stable] ...", "[v3-2-test] ..."
    r"|^(branch|release)-\d+(\.\d+)*\s*:"                         # "branch-4.0: ...", "release-26.2: ..."
    r"|\(uplift to [^)]*\)|\bstable batch\b"                      # "... (uplift to 1.93.x)", "v260 stable batch up to ..."
    r"|\b(backport|cherry[- ]?pick)\b|\U0001F352"               # Swift marks cherry-picks with a cherry emoji
    # release ceremony and branch syncs: "chore(release): 2.260.0", "Version Packages", "Release v8.2.0",
    # "v0.46.0", "nginx-1.30.2-RELEASE", "REL: ...", "chore: prepare Tokio v1.52.3", "Merge 'tokio-1.51.3' into
    # 'tokio-1.52.x'", "Merge main to features/x"
    r"|^(chore\((release|merge-back)\): *v?\d|\[ci\] (release|format)$|version packages\b|patch release$|(rel|rls)[:,]"
    r"|(chore: )?prepare (\w+ )?v?\d+\.\d+"
    r"|release[/ ]+v?\d+(\.\d+)+\S*$|v?\d+\.\d+\.\d+\S*$|\S+-\d+(\.\d+)+-release$"
    r"|merge (back\b|(main|master)$|v?\d+(\.\d+)+$|(release |branch )?[`'\"]?(main|master|[\w.-]*(\d|/|release|stable|feature)[\w./-]*)"
    r"[`'\"]?( branch)? (in)?to [`'\"]?[\w./-]+[`'\"]?( branch)?$)"
    # automation that runs under a human-typed account: dependency rolls, l10n syncs, generated SDKs
    r"|roll \S+ from [0-9a-f]{7,} to [0-9a-f]{7,}|localization updates\b|translations update from|\[autopr\b)", re.I)
BOT_LOGIN = re.compile(
    r"(\[bot\]$|bot$|-robot$|^robot-|^bors$|^homu$|^rustbot$|^codecov|^netlify|^vercel$|^miss-islington$|^trop$"
    r"|machine$|autoroll|-automation$|-builds?$|-publish$|^publish-|-devprod$|-service$|-integration$|wpt-sync"
    r"|^azure-sdk$|^hc-github-team-|^consvc$|^weblate$|-teamcity$)", re.I)
# Release-branch PRs that the title does not mark. A PR is treated as one when it targets a branch other than the
# development branch and that branch is a maintenance line ("v1.82.x", "43-x-y", "maintenance/2.5.x"), or the
# title is prefixed with the branch ("tentacle: ..."), names its version ("... (1.93.x)", "[4.1]", "on 25.8"), or
# ends with the original PR's number ("Fix X (#31609)") on a release-like branch. Fix-first-on-stable workflows
# (php-src PHP-8.x, duckdb v1.5-*, nestjs/apollo next-version branches) are deliberately NOT caught.
DEV_BRANCHES = {"main", "master", "trunk", "develop", "dev", "canary", "next", "unstable"}
RELEASE_BASE = re.compile(r"release|stable|maint|hotfix|lts|^rc$|^branch|\d+[._-](\d+|x)", re.I)
MAINT_LINE = re.compile(r"[.-]x(-y|-dev)?$|^maintenance/", re.I)
HUMAN_LOGIN = {"paleolimbot"}     # people whose login happens to match BOT_LOGIN
# Coding-agent accounts. Their PRs stay in the sample (human review of agent code is still review) and are counted;
# their own review comments count as AI, not human. Includes personal agent accounts (Deno's divybot etc.,
# Bun's robobun "farm", ClickHouse's groeneai/clickgapai, Astro's Houston triage bot, Brave's netzenbot).
AI_AGENT_LOGIN = re.compile(
    r"^(copilot-swe-agent|copilot|claude|cursor|devin-ai-integration|chatgpt-codex-connector|codex|google-labs-jules"
    r"|openhands(-agent)?|sweep-ai|agent-sandbox-\S+|robobun|groeneai|clickgapai|astrobot-houston|netzenbot|divybot"
    r"|crowlbot|nathanwhitbot|zaniebot)$", re.I)
# Repos that land by closing the PR (merge scripts, Skara, merge bots, Meta's Phabricator export) rather than
# GitHub's merge.
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
LANDED_BY_COMMIT = {"curl/curl", "vim/vim"}   # no label: keep only PRs closed by the commit that landed them
RENAMED = {"facebook/react": "react/react", "facebook/react-native": "react/react-native", "prisma/prisma": "prisma/orm"}
MAX_PAGES = 8
AI_LOGIN = re.compile(
    r"copilot|coderabbit|gemini|greptile|sourcery|codex|claude|cursor|qodo|ellipsis|graphite|korbit|cubic|bito|devin|augment"
    r"|bonk|macroscope|depthfirst|review-bot|cat-bot|^clickhouse-gh$|^vercel$|^sentry$", re.I)
# Repos whose LLM review workflow posts inline threads as the generic github-actions account.
AI_REVIEW_VIA_ACTIONS = {"systemd/systemd", "elastic/kibana", "apache/doris", "dotnet/runtime"}
SUGGESTION = re.compile(r"```suggestion")
NIT = re.compile(r"^\W*(nit|minor|optional)\b", re.I)

QUERY = """
query($q: String!, $vq: String!, $n: Int!, $after: String) {
  rateLimit { cost remaining }
  volume: search(query: $vq, type: ISSUE, first: 1) { issueCount }
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title createdAt mergedAt additions deletions changedFiles baseRefName
      author { login __typename }
      commits { totalCount }
      forcePushes: timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) { filteredCount }
      closed: timelineItems(itemTypes: [CLOSED_EVENT], last: 1) { nodes { ... on ClosedEvent { closer { __typename } } } }
      comments(first: 40) { totalCount nodes { author { login __typename } body } }
      reviews(first: 40) { totalCount nodes { state author { login __typename } } }
      reviewThreads(first: 60) { totalCount nodes {
        isResolved isOutdated
        comments(first: 8) { totalCount nodes { author { login __typename } body reactions { totalCount } } }
      } }
    } }
  }
}"""


def gql(variables):
    body = json.dumps({"query": QUERY, "variables": variables})
    out = subprocess.run(["gh", "api", "graphql", "--input", "-"], input=body,
                         capture_output=True, text=True, timeout=180)
    data = json.loads(out.stdout) if out.stdout.strip() else {}
    if out.returncode != 0 or data.get("errors"):
        raise RuntimeError((data.get("errors") or out.stderr)[:1] if data else out.stderr[:300])
    return data["data"]


def queries(repo):
    """(sample query, 12-month volume query) for a repo, honoring its landing convention."""
    landed = LANDED.get(repo, "is:merged")
    return (f"repo:{repo} is:pr {landed} created:{WINDOW} -author:app/dependabot -author:app/renovate sort:created-desc",
            f"repo:{repo} is:pr {landed} created:{VOLUME_WINDOW}")


def landed_by_commit(p):
    return any((e.get("closer") or {}).get("__typename") == "Commit" for e in p.get("closed", {}).get("nodes", []))


def repo_info(repo):
    """Default branch and primary language (REST: does not touch the GraphQL budget)."""
    out = subprocess.run(["gh", "api", f"repos/{repo}"], capture_output=True, text=True, timeout=60)
    info = json.loads(out.stdout) if out.returncode == 0 else {}
    return {"default_branch": info.get("default_branch"), "language": info.get("language")}


def fetch(repo):
    landed = LANDED.get(repo, "is:merged")
    q, vq = queries(repo)
    raw = {"repo": repo, **repo_info(repo), "prs": []}
    prs, after, n, cost, volume, pages, seen, has_next = raw["prs"], None, 20, 0, None, 0, 0, False
    while len(sample(raw)) < TARGET_PRS and pages < MAX_PAGES:
        pages += 1
        for attempt in range(4):
            try:
                d = gql({"q": q, "vq": vq, "n": n, "after": after})
                break
            except Exception as e:  # GitHub times out on heavy pages; shrink and retry
                n = max(5, n // 2)
                print(f"  {repo}: retry {attempt + 1} page={n} ({str(e)[:120]})", file=sys.stderr)
                time.sleep(3 * (attempt + 1))
        else:
            break
        cost += d["rateLimit"]["cost"]
        volume = d["volume"]["issueCount"]
        s = d["search"]
        nodes = [p for p in s["nodes"] if p]
        seen += len(nodes)
        prs += [p for p in nodes if repo not in LANDED_BY_COMMIT or landed_by_commit(p)]
        has_next = s["pageInfo"]["hasNextPage"]
        if not has_next:
            break
        after = s["pageInfo"]["endCursor"]
    if repo in LANDED_BY_COMMIT and volume and seen:   # the search also counts PRs closed without landing
        volume = round(volume * len(prs) / seen)
    return {**raw, "volume_12mo": volume, "graphql_cost": cost, "landed": landed,
            "has_more": has_next and pages < MAX_PAGES}


def is_stale(repo, raw):
    """A cache is stale if it was drawn with a different landing query, or if the current filters leave it
    short of MIN_SAMPLE while the search had more PRs to give."""
    return (raw.get("landed") != LANDED.get(repo, "is:merged")
            or (len(sample(raw)) < MIN_SAMPLE and raw.get("has_more", False)))


def keep(p):
    a = p.get("author")
    return (is_agent(a) or not is_bot(a)) and not EXCLUDE_TITLE.search(p["title"])


def on_release_branch(p, default):
    """See RELEASE_BASE: an unmarked backport or maintenance-line PR, judged from its base branch."""
    base = p.get("baseRefName")
    if not base or not default or base == default or base in DEV_BRANCHES or re.match(r"feat(ure)?[/-]", base):
        return False
    title = p["title"].strip().lower()
    ver = re.search(r"\d+(?:[._-](?:\d+|x))+(?:-y)?", base)
    return bool(MAINT_LINE.search(base)
                or title.startswith(base.lower() + ":")
                or (ver and re.search(rf"(?<![\w.]){re.escape(ver.group(0).lower())}(?!\w|\.\d)", title))
                or (RELEASE_BASE.search(base) and re.search(r"\(#\d+\)$", title)))


def sample(raw):
    """The measured PRs, newest first: kept by author and title, not on a release branch, and minus same-title
    copies aimed at a different branch (manual cherry-picks: "Fix X (#123)" reopened against release-11.7); the
    earliest-created copy is the one kept."""
    norm = lambda t: re.sub(r"\s*\(?#\d+\)?\s*$", "", t).strip().lower()
    kept = [p for p in raw["prs"] if keep(p) and not on_release_branch(p, raw.get("default_branch"))]
    first = {}
    for p in sorted(kept, key=lambda p: p["createdAt"]):
        first.setdefault(norm(p["title"]), p)
    return [p for p in kept if first[norm(p["title"])] is p
            or first[norm(p["title"])].get("baseRefName", 0) == p.get("baseRefName", 1)][:TARGET_PRS]


def is_agent(a):
    return bool(a) and bool(AI_AGENT_LOGIN.search(a.get("login", "")))


def is_bot(a):
    if not a:
        return True
    login = a.get("login", "")
    return login not in HUMAN_LOGIN and (a.get("__typename") == "Bot" or bool(BOT_LOGIN.search(login)) or is_agent(a))


def is_ai(a, repo=None):
    login = (a or {}).get("login", "")
    if repo in AI_REVIEW_VIA_ACTIONS and login == "github-actions":
        return True
    return bool(a) and (bool(AI_LOGIN.search(login)) or is_agent(a)) and is_bot(a) | ("copilot" in login.lower())


def pct(num, den):
    return round(100 * num / den, 1) if den else None


def mean(xs):
    return round(st.mean(xs), 2) if xs else 0.0


def metrics(raw):
    repo, prs = raw["repo"], sample(raw)
    per_pr_threads, per_pr_reviewers, per_pr_approvals, per_pr_conv = [], [], [], []
    h_threads = ai_threads = 0
    h_outdated = h_resolved = h_author_reply = h_multi = h_suggest = h_nit = 0
    ai_outdated = ai_resolved = ai_replied = 0
    first_lens, reactions, h_comments = [], 0, 0
    changes_requested = prs_with_ai = truncated = 0
    for p in prs:
        author = (p.get("author") or {}).get("login")
        reviewers, approvers, n_threads = set(), set(), 0
        for r in p["reviews"]["nodes"]:
            a = r.get("author")
            if a and not is_bot(a) and a["login"] != author:
                reviewers.add(a["login"])
                if r["state"] == "APPROVED":
                    approvers.add(a["login"])
        if any(r["state"] == "CHANGES_REQUESTED" and not is_bot(r.get("author")) for r in p["reviews"]["nodes"]):
            changes_requested += 1
        truncated += p["reviewThreads"]["totalCount"] > len(p["reviewThreads"]["nodes"])
        pr_has_ai = False
        for t in p["reviewThreads"]["nodes"]:
            cs = t["comments"]["nodes"]
            if not cs:
                continue
            a0 = cs[0].get("author")
            replied = any((c.get("author") or {}).get("login") == author for c in cs[1:])
            if is_ai(a0, repo):
                pr_has_ai = True
                ai_threads += 1
                ai_outdated += t["isOutdated"]
                ai_resolved += t["isResolved"]
                ai_replied += replied
                continue
            if is_bot(a0) or a0["login"] == author:
                continue
            n_threads += 1
            reviewers.add(a0["login"])
            h_threads += 1
            h_outdated += t["isOutdated"]
            h_resolved += t["isResolved"]
            h_author_reply += replied
            h_multi += t["comments"]["totalCount"] >= 2
            h_suggest += bool(SUGGESTION.search(cs[0]["body"]))
            h_nit += bool(NIT.search(cs[0]["body"]))
            first_lens.append(len(cs[0]["body"]))
            for c in cs:
                if not is_bot(c.get("author")):
                    h_comments += 1
                    reactions += c["reactions"]["totalCount"]
        prs_with_ai += pr_has_ai
        conv = [c for c in p["comments"]["nodes"]
                if not is_bot(c.get("author")) and (c.get("author") or {}).get("login") != author
                and len(c["body"]) >= 20]
        per_pr_conv.append(len(conv))
        per_pr_threads.append(n_threads)
        per_pr_reviewers.append(len(reviewers))
        per_pr_approvals.append(len(approvers))
    n = len(prs)
    return {
        "repo": repo, "language": raw.get("language"), "prs": n, "volume_12mo": raw["volume_12mo"],
        "ai_agent_prs": sum(is_agent(p.get("author")) for p in prs),
        "pct_prs_inline_reviewed": pct(sum(x > 0 for x in per_pr_threads), n),
        "threads_per_pr": mean([min(x, 20) for x in per_pr_threads]),
        "threads_per_pr_median": st.median(per_pr_threads) if per_pr_threads else 0,
        "reviewers_per_pr": mean(per_pr_reviewers),
        "approvals_per_pr": mean(per_pr_approvals),
        "pct_changes_requested": pct(changes_requested, n),
        "conv_comments_per_pr": mean(per_pr_conv),
        "human_threads": h_threads,
        "pct_outdated": pct(h_outdated, h_threads),         # proxy for "flagged lines changed"
        "pct_resolved": pct(h_resolved, h_threads),
        "pct_author_reply": pct(h_author_reply, h_threads),
        "pct_discussion": pct(h_multi, h_threads),
        "pct_suggestion": pct(h_suggest, h_threads),
        "pct_nit": pct(h_nit, h_threads),
        "median_first_comment_chars": st.median(first_lens) if first_lens else 0,
        "reactions_per_100_comments": round(100 * reactions / h_comments, 1) if h_comments else 0,
        "force_pushes_per_pr": mean([p["forcePushes"]["filteredCount"] for p in prs]),
        "pct_prs_threads_truncated": pct(truncated, n),
        "pct_prs_ai_reviewed": pct(prs_with_ai, n),
        "ai_threads": ai_threads,
        "ai_pct_outdated": pct(ai_outdated, ai_threads),
        "ai_pct_resolved": pct(ai_resolved, ai_threads),
        "ai_pct_author_reply": pct(ai_replied, ai_threads),
    }


def main():
    out = Path(sys.argv[1])
    (out / "raw").mkdir(parents=True, exist_ok=True)
    repos = list(dict.fromkeys(RENAMED.get(r, r) for r in sys.argv[2:]))   # search does not follow renames

    def one(repo):
        path = out / "raw" / (repo.replace("/", "__") + ".json")
        if not path.exists() or is_stale(repo, json.loads(path.read_text())):
            t = time.time()
            raw = fetch(repo)
            path.write_text(json.dumps(raw))
            print(f"fetched {repo}: {len(raw['prs'])} PRs, cost {raw['graphql_cost']}, {time.time() - t:.0f}s",
                  file=sys.stderr)
        return metrics(json.loads(path.read_text()))

    results = []
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(one, r): r for r in repos}
        for f in cf.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                print(f"FAILED {futs[f]}: {e}", file=sys.stderr)
    existing = {}
    summary = out / "metrics.json"
    if summary.exists():
        existing = {m["repo"]: m for m in json.loads(summary.read_text())}
    existing.update({m["repo"]: m for m in results})
    for old in RENAMED:
        existing.pop(old, None)
    summary.write_text(json.dumps(sorted(existing.values(), key=lambda m: m["repo"]), indent=1))
    print(f"wrote {len(existing)} repos to {summary}", file=sys.stderr)


if __name__ == "__main__":
    main()
