#!/usr/bin/env python3
"""Rank repos from measure_repos.py output by a composite review-rigor score.

Each component is converted to a percentile rank across the measured repos, then weighted.
Usage: rank_repos.py repo_metrics/metrics.json [--min-volume 300] [--top 25] [--exclude a/b,c/d] [--by-language 12]
"""
import argparse
import json

# (metric, weight, why)
RIGOR = [
    ("threads_per_pr", 0.25, "depth: human inline review threads per PR"),
    ("pct_prs_inline_reviewed", 0.20, "coverage: share of PRs with any human inline thread"),
    ("pct_outdated", 0.20, "consequence: share of threads whose lines later changed"),
    ("reviewers_per_pr", 0.15, "breadth: distinct human reviewers per PR"),
    ("pct_discussion", 0.10, "engagement: threads with a back-and-forth"),
    ("median_first_comment_chars", 0.10, "substance: length of the opening comment"),
]
# Review happens somewhere GitHub does not record, so the GitHub-visible numbers understate it. Always excluded;
# --exclude adds to this.
OFF_GITHUB_REVIEW = {
    "cockroachdb/cockroach": "reviewed in Reviewable (reviewable.io banner on 112/120 cached PRs)",
    "react/react-native": "Meta-internal diffs (internalfb.com Diff links on 77/80 cached PRs; landed by meta-codesync)",
    "facebook/rocksdb": "Meta-internal diffs (internalfb.com Diff links on 60/60 cached PRs; landed by Meta bot)",
    "protocolbuffers/protobuf": "Copybara export of Google-internal review (157/160 cached PRs authored by copybara-service)",
}
LANGUAGE_GROUPS = [("TypeScript/JavaScript", {"TypeScript", "JavaScript"}), ("C/C++", {"C", "C++"}),
                   ("Python", {"Python"})]


def language_group(lang):
    return next((g for g, langs in LANGUAGE_GROUPS if lang in langs), "Other")


def pct_rank(values, v):
    vals = [x for x in values if x is not None]
    if v is None or not vals:
        return 0.0
    return sum(x < v for x in vals) / len(vals) + 0.5 * sum(x == v for x in vals) / len(vals)


def row(i, m):
    return [i, m["repo"] + ("" if m["enough_data"] else " (low vol)"), m.get("language"), m["rigor"], m["volume_12mo"],
            m["pct_prs_inline_reviewed"], m["threads_per_pr"], m["threads_per_pr_median"], m["reviewers_per_pr"],
            m["pct_outdated"], m["pct_author_reply"], m["pct_discussion"], m["median_first_comment_chars"],
            m["pct_suggestion"], m["reactions_per_100_comments"], m["force_pushes_per_pr"], m["pct_prs_ai_reviewed"],
            m.get("ai_agent_prs", 0), m["prs"]]


HEADER = ["rank", "repo", "lang", "rigor", "merged/yr", "cover%", "thr/PR", "thr_med", "revs/PR", "outdated%", "reply%", "discuss%", "chars", "sugg%", "react/100", "fpush/PR", "ai%", "agent_prs", "n"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("metrics")
    ap.add_argument("--min-volume", type=int, default=300)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--exclude", default="", help="comma-separated repos whose review happens off GitHub")
    ap.add_argument("--by-language", type=int, default=0, help="also print the top N of each language group")
    a = ap.parse_args()
    excluded = set(OFF_GITHUB_REVIEW) | set(filter(None, a.exclude.split(",")))
    everything = json.load(open(a.metrics))
    ms = [m for m in everything if m["prs"] >= 20 and m["repo"] not in excluded]
    cols = {k: [m[k] for m in ms] for k, _, _ in RIGOR}
    for m in ms:
        m["rigor"] = round(100 * sum(w * pct_rank(cols[k], m[k]) for k, w, _ in RIGOR), 1)
        m["enough_data"] = (m["volume_12mo"] or 0) >= a.min_volume
    ms.sort(key=lambda m: -m["rigor"])
    print("\t".join(HEADER))
    for i, m in enumerate(ms[: a.top], 1):
        print("\t".join(str(x) for x in row(i, m)))
    print(f"\n{len(ms)} repos ranked (>=20 sampled PRs).")
    if a.by_language:
        rank = {m["repo"]: i for i, m in enumerate(ms, 1)}
        for group in [g for g, _ in LANGUAGE_GROUPS] + ["Other"]:
            members = [m for m in ms if language_group(m.get("language")) == group]
            print(f"\n## {group} ({len(members)} ranked)\n" + "\t".join(HEADER))
            for m in members[: a.by_language]:
                print("\t".join(str(x) for x in row(rank[m["repo"]], m)))
    ai = sorted((m for m in ms if m["ai_threads"] >= 10), key=lambda m: -m["ai_threads"])
    if ai:
        print("\nRepos with AI-reviewer threads (>=10):")
        print("repo\tai_threads\tai%PRs\tai_outdated%\tai_resolved%\tai_reply%")
        for m in ai:
            print(f'{m["repo"]}\t{m["ai_threads"]}\t{m["pct_prs_ai_reviewed"]}\t{m["ai_pct_outdated"]}'
                  f'\t{m["ai_pct_resolved"]}\t{m["ai_pct_author_reply"]}')
    small = [m["repo"] for m in everything if m["prs"] < 20]
    if small:
        print("\nToo few PRs sampled to rank:", ", ".join(small))
    print("\nExcluded (review off GitHub):")
    for r in sorted(excluded):
        print(f"{r}\t{OFF_GITHUB_REVIEW.get(r, 'excluded via --exclude')}")


if __name__ == "__main__":
    main()
