"""core.filters, on the examples named in the research code's comments (research/repo-selection/measure_repos.py)."""

import pytest

from honed.core import filters
from honed.core.types import Actor, AuthorKind, PRSummary

EXCLUDED_TITLES = [
    "[3.15] gh-12345: Fix crash in parser",
    "[SPARK-1][SQL][4.1] Fix window aggregation",
    "[CP-stable] Fix scrolling regression",
    "[stable] Bump engine",
    "[v3-2-test] Fix task mapping",
    "branch-4.0: fix scheduler race",
    "release-26.2: backport memory fix",
    "Fix borrowck ICE (uplift to 1.93.x)",
    "v260 stable batch up to 1234abc",
    "chore(release): 2.260.0",
    "Version Packages",
    "Release v8.2.0",
    "v0.46.0",
    "nginx-1.30.2-RELEASE",
    "REL: prepare 2.1 notes",
    "chore: prepare Tokio v1.52.3",
    "Merge 'tokio-1.51.3' into 'tokio-1.52.x'",
    "Merge main to features/x",
    "Backport of #123 to 1.x",
    "Bump lodash from 4.17.20 to 4.17.21",
    "Roll Skia from 0123abcd to 4567cdef (3 revisions)",
    "\U0001f352 Fix availability attribute",
]
KEPT_TITLES = [
    "Fix race in the scheduler",
    "Add Array API support to newton-cg",
    "Improve release notes rendering",
    "Merge sort: handle empty input",
]


@pytest.mark.parametrize("title", EXCLUDED_TITLES)
def test_release_ceremony_and_automation_titles_are_excluded(title):
    assert filters.EXCLUDE_TITLE.search(title)
    assert not filters.keep_pr(title, Actor("dev"))


@pytest.mark.parametrize("title", KEPT_TITLES)
def test_ordinary_titles_are_kept(title):
    assert filters.keep_pr(title, Actor("dev"))


@pytest.mark.parametrize(
    ("title", "base"),
    [
        ("Fix foo", "v1.82.x"),  # maintenance line
        ("Fix foo", "43-x-y"),
        ("Fix foo", "maintenance/2.5.x"),
        ("tentacle: fix osd crash", "tentacle"),  # title prefixed with the branch
        ("Fix borrowck ICE (1.93.x)", "release-1.93"),  # title names the branch's version
        ("Fix thing on 25.8", "release-25.8"),
        ("Fix X (#31609)", "release-11.7"),  # original PR number on a release-like branch
    ],
)
def test_unmarked_release_branch_prs(title, base):
    assert filters.on_release_branch(title, base, "main")


@pytest.mark.parametrize(
    ("title", "base"),
    [
        ("Fix foo", "main"),
        ("Fix foo", "develop"),  # a development branch
        ("Fix foo", "feature/new-parser"),
        ("Fix bug in fopen", "PHP-8.4"),  # fix-first-on-stable workflows are deliberately not caught
    ],
)
def test_development_and_fix_first_branches_are_not_release_branches(title, base):
    assert not filters.on_release_branch(title, base, "main")


def test_bots_automation_and_the_human_allowlist():
    assert filters.is_bot(Actor("dependabot[bot]", "Bot"))
    assert filters.is_bot(Actor("k8s-ci-robot"))  # automation typed as a User
    assert filters.is_bot(Actor("bors"))
    assert filters.is_bot(None)  # a deleted account
    assert not filters.is_bot(Actor("paleolimbot"))  # matches BOT_LOGIN but is a person
    assert not filters.is_bot(Actor("octocat"))


def test_ai_reviewers_and_agents():
    assert filters.is_ai(Actor("copilot-pull-request-reviewer", "Bot"))
    assert filters.is_ai(Actor("Copilot"))  # typed as a User, recognized by login
    assert filters.is_ai(Actor("coderabbitai[bot]", "Bot"))
    assert filters.is_ai(Actor("copilot-swe-agent", "Bot"))
    assert not filters.is_ai(Actor("claude-fan"))  # a person whose login mentions an AI
    assert filters.is_agent(Actor("robobun"))


def test_ai_review_posted_as_github_actions_only_in_listed_repos():
    actions = Actor("github-actions", "Bot")
    assert filters.author_kind(actions, "elastic/kibana") is AuthorKind.AI
    assert filters.author_kind(actions, "scikit-learn/scikit-learn") is AuthorKind.BOT


def test_agent_prs_stay_in_the_sample():
    assert filters.keep_pr("Fix flaky test", Actor("copilot-swe-agent", "Bot"))
    assert filters.pr_author_kind(Actor("copilot-swe-agent", "Bot")) is AuthorKind.AI
    assert not filters.keep_pr("Fix flaky test", Actor("renovate[bot]", "Bot"))


def test_thread_opened_by_the_pr_author():
    author = Actor("dev")
    assert filters.thread_author_kind(author, author, "o/r") is AuthorKind.PR_AUTHOR
    assert filters.thread_author_kind(Actor("reviewer"), author, "o/r") is AuthorKind.HUMAN


def test_branch_copies_keep_the_earliest():
    original = PRSummary("o/r", 1, "Fix X (#1)", Actor("a"), "2026-01-01T00:00:00Z", "main")
    copy = PRSummary("o/r", 2, "Fix X", Actor("a"), "2026-01-05T00:00:00Z", "release-11.7")
    same_branch = PRSummary("o/r", 3, "Fix X", Actor("b"), "2026-01-06T00:00:00Z", "main")
    assert filters.drop_branch_copies([same_branch, copy, original]) == [same_branch, original]


def test_landing_conventions_and_renames():
    assert filters.landed_qualifier("openjdk/jdk") == "is:closed label:integrated"
    assert filters.landed_qualifier("django/django") == "is:merged"
    assert filters.canonical_repo("Facebook/React") == "react/react"
    assert filters.is_excluded("react/react", ["facebook/react"])
    assert filters.is_excluded("KEYCLOAK/keycloak", ["keycloak/keycloak"])
    assert not filters.is_excluded("django/django", ["keycloak/keycloak"])


def test_reviewed_by_human_ignores_the_author_and_bots():
    pr = PRSummary(
        "o/r", 1, "t", Actor("dev"), "2026-01-01", "main", review_authors=(Actor("dev"), Actor("ci[bot]", "Bot"))
    )
    assert not filters.reviewed_by_human(pr)
    assert filters.reviewed_by_human(
        PRSummary("o/r", 1, "t", Actor("dev"), "2026-01-01", "main", review_authors=(Actor("rev"),))
    )
    assert filters.reviewed_by_ai(
        PRSummary("o/r", 1, "t", Actor("dev"), "2026-01-01", "main", review_authors=(Actor("Copilot"),))
    )


def test_changes_requested_by_a_human_other_than_the_author():
    def pr(*by):
        return PRSummary("o/r", 1, "t", Actor("dev"), "2026-01-01", "main", changes_requested_by=by)

    assert not filters.changes_requested_by_human(pr())
    assert not filters.changes_requested_by_human(pr(Actor("dev"), Actor("ci[bot]", "Bot"), None))
    assert not filters.changes_requested_by_human(pr(Actor("coderabbitai[bot]", "Bot")))
    assert filters.changes_requested_by_human(pr(Actor("ci[bot]", "Bot"), Actor("rev")))
