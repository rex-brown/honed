"""Harvesting end to end without the network: a fake CodeHost replays recorded GitHub payloads."""

from pathlib import Path

import pytest

from builders import ANCHOR, HEAD, comment, thread
from fakes import FakeCodeHost
from honed.adapters.blobs import BlobStore
from honed.adapters.sqlite_store import SqliteStore
from honed.core.types import (
    Actor,
    AuthorKind,
    Compare,
    CompareSource,
    Corpus,
    Cursor,
    DateWindow,
    FilePatch,
    LineBasis,
    Outcome,
    PRKey,
    PRSummary,
    PullRequest,
    RepoInfo,
    Review,
    SampledAs,
)
from honed.learn.harvest import Harvester, HarvestOptions
from honed.learn.plan import ExcludedRepo, RepoPlan

FIXTURE = Path(__file__).parent / "fixtures" / "github_sklearn.json"
SKLEARN = "scikit-learn/scikit-learn"
JUNE = DateWindow("2026-06-01", "2026-06-30")


def options(limit=None, slices=(JUNE,), excluded=frozenset()):
    return HarvestOptions(
        slices=slices, page_size=5, line_slack=0, excluded=excluded, github_languages={"Python": "python"},
        fallback_language="other", limit=limit,
    )  # fmt: skip


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    yield s
    s.close()


def test_harvests_filters_and_labels_the_recorded_prs(store):
    host = FakeCodeHost.from_fixture(FIXTURE)
    (report,) = Harvester(host, store, options()).run([RepoPlan(SKLEARN, Corpus.HUMAN, 10, "python")])

    # scikit-learn-bot's lock-file PR (#34409) is automation under a User account.
    assert report.added == 4 and report.skipped == {"bot_or_release_title": 1}
    assert [k.number for k in store.pr_keys()] == [34412, 34413, 34417, 34419]

    item = store.get_pr(PRKey(SKLEARN, 34412))
    assert item.language == "python" and item.corpus is Corpus.HUMAN and item.author_kind is AuthorKind.HUMAN
    assert item.reviewed_commit.startswith("8341c202")  # head of the first human review round
    assert item.pr.reviewed_diff is not None and item.pr.reviewed_diff.files
    assert len(item.labels) == len(item.pr.threads) == 8
    # sklearn does not force-push, so compare patches decide nearly everything; a LEFT-side thread falls back.
    bases = [label.line_basis for label in item.labels]
    assert bases.count(LineBasis.COMPARE) == 7 and bases.count(LineBasis.IS_OUTDATED) == 1

    facts = store.thread_facts()
    human = [f for f in facts if f.author_kind is AuthorKind.HUMAN]
    assert {f.author_kind for f in facts} == {AuthorKind.HUMAN, AuthorKind.PR_AUTHOR}
    assert sum(f.outcome is Outcome.FIXED for f in human) == 7
    # #34417: GitHub marks a thread outdated whose line no later commit touched; the compare patch knows better.
    spurious = [lab for lab in store.get_pr(PRKey(SKLEARN, 34417)).labels if lab.author_kind is AuthorKind.PR_AUTHOR]
    assert [(lab.outcome, lab.lines_changed) for lab in spurious] == [(Outcome.OPEN_AT_MERGE, False)]


def test_a_second_run_resumes_without_refetching(store):
    plan = [RepoPlan(SKLEARN, Corpus.HUMAN, 10, "python")]
    Harvester(FakeCodeHost.from_fixture(FIXTURE), store, options()).run(plan)
    host = FakeCodeHost.from_fixture(FIXTURE)
    (report,) = Harvester(host, store, options()).run(plan)
    assert report.added == 0
    assert host.calls["fetch_pr"] == 0 and host.calls["list"] == 0  # the slice's cursor is exhausted


def test_limit_caps_a_run_and_the_cursor_continues_mid_page(store):
    plan = [RepoPlan(SKLEARN, Corpus.HUMAN, 10, "python")]
    first = FakeCodeHost.from_fixture(FIXTURE)
    assert Harvester(first, store, options(limit=2)).run(plan)[0].added == 2
    assert [k.number for k in store.pr_keys()] == [34417, 34419]
    second = FakeCodeHost.from_fixture(FIXTURE)
    (report,) = Harvester(second, store, options(limit=5)).run(plan)
    assert report.added == 2 and second.calls["fetch_pr"] == 2  # nothing fetched twice
    assert report.skipped == {"bot_or_release_title": 1}


def test_quota_counts_prs_already_stored(store):
    plan = [RepoPlan(SKLEARN, Corpus.HUMAN, 3, "python")]
    Harvester(FakeCodeHost.from_fixture(FIXTURE), store, options()).run(plan)
    assert len(store.pr_keys()) == 3
    host = FakeCodeHost.from_fixture(FIXTURE)
    assert Harvester(host, store, options()).run(plan)[0].added == 0
    assert host.calls["list"] == 0


def test_excluded_repos_are_never_harvested(store):
    host = FakeCodeHost.from_fixture(FIXTURE)
    with pytest.raises(ExcludedRepo):
        Harvester(host, store, options(excluded=frozenset({"Scikit-Learn/scikit-learn"}))).run(
            [RepoPlan(SKLEARN, Corpus.HUMAN, 10, "python")]
        )
    assert sum(host.calls.values()) == 0


def test_ai_feedback_corpus_keeps_only_ai_threads(store):
    host = FakeCodeHost.from_fixture(FIXTURE)
    (report,) = Harvester(host, store, options()).run([RepoPlan(SKLEARN, Corpus.AI_FEEDBACK, 5, None)])
    assert report.added == 0 and report.skipped["no_ai_review"] == 4  # nobody reviewed these with AI


# ---- force-pushed anchors ----------------------------------------------------------------------------------


def force_pushed_host(anchor_text: str, head_text: str) -> FakeCodeHost:
    """One PR whose single thread is anchored on a commit that was later force-pushed away."""
    author = Actor("dev")
    summary = PRSummary("o/r", 1, "Fix parser", author, "2026-06-10T00:00:00Z", "main", 1, (Actor("rev"),))
    pr = PullRequest(
        repo="o/r", number=1, title="Fix parser", author=author, created_at="2026-06-10T00:00:00Z",
        landed_at="2026-06-12T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=HEAD, force_pushes=3,
        threads=(thread(comment("rev", line=2), lines=(2, 2), outdated=True, path="p.py"),),
        reviews=(Review("r", Actor("rev"), "COMMENTED", "2026-06-11T00:00:00Z", ANCHOR),),
    )  # fmt: skip
    merge_base_diff = Compare(ANCHOR, HEAD, "diverged", ())  # the host's merge-base compare: not the direct diff
    return FakeCodeHost(
        repos={"o/r": RepoInfo("o/r", "main", "Python")},
        pages={"o/r": [(summary,)]},
        prs={("o/r", 1): pr},
        compares={(ANCHOR, HEAD): merge_base_diff, ("b" * 40, ANCHOR): Compare("b" * 40, ANCHOR, "ahead", ())},
        files={("p.py", ANCHOR): anchor_text, ("p.py", HEAD): head_text},
    )


def test_threads_missing_from_a_capped_compare_are_diffed_from_contents(store):
    host = force_pushed_host("a\nb\nc\n", "a\nB\nc\n")
    # The anchor is an ancestor this time, but the compare hit the host's file cap without listing p.py.
    host.compares[(ANCHOR, HEAD)] = Compare(ANCHOR, HEAD, "ahead", (FilePatch("x.py", "modified", ""),), complete=False)
    Harvester(host, store, options()).run([RepoPlan("o/r", Corpus.HUMAN, 1, None)])
    item = store.get_pr(PRKey("o/r", 1))
    assert [c.source for c in item.pr.compares] == [CompareSource.GITHUB, CompareSource.CONTENT_DIFF]
    (label,) = item.labels
    assert (label.outcome, label.line_basis) == (Outcome.FIXED, LineBasis.COMPARE)


@pytest.mark.parametrize(
    ("head_text", "outcome"),
    [("a\nB\nc\n", Outcome.FIXED), ("a\nb\nc\nd\n", Outcome.IGNORED)],
)
def test_force_pushed_anchor_is_diffed_from_file_contents(store, head_text, outcome):
    host = force_pushed_host("a\nb\nc\n", head_text)
    Harvester(host, store, options()).run([RepoPlan("o/r", Corpus.HUMAN, 1, None)])
    item = store.get_pr(PRKey("o/r", 1))
    (compare,) = item.pr.compares
    assert compare.source is CompareSource.CONTENT_DIFF
    (label,) = item.labels
    assert (label.outcome, label.line_basis) == (outcome, LineBasis.COMPARE)  # isOutdated (True) is overruled
    assert item.language == "python"  # from the repo's primary language


# ---- approval-only split -----------------------------------------------------------------------------------


def approval_host() -> FakeCodeHost:
    """PR 1 has a human inline thread; PRs 2 and 3 were approved without one (2 has only the author's own thread)."""
    dev, rev = Actor("dev"), Actor("rev")

    def summary(n: int, threads: int) -> PRSummary:
        return PRSummary("o/r", n, f"Change {n}", dev, f"2026-06-1{n}T00:00:00Z", "main", threads, (rev,))

    def pr(n: int, threads: tuple) -> PullRequest:
        return PullRequest(
            repo="o/r", number=n, title=f"Change {n}", author=dev, created_at=f"2026-06-1{n}T00:00:00Z",
            landed_at="2026-06-20T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=HEAD, threads=threads,
            reviews=(Review(f"r{n}", rev, "APPROVED", "2026-06-15T00:00:00Z", HEAD),),
        )  # fmt: skip

    human = thread(comment("rev", commit=HEAD), path="p.py", tid="h1")
    own = thread(comment("dev", commit=HEAD), path="p.py", tid="o1")
    return FakeCodeHost(
        repos={"o/r": RepoInfo("o/r", "main", "Python")},
        pages={"o/r": [(summary(1, 1), summary(2, 1), summary(3, 0))]},
        prs={("o/r", 1): pr(1, (human,)), ("o/r", 2): pr(2, (own,)), ("o/r", 3): pr(3, ())},
        compares={(HEAD, HEAD): Compare(HEAD, HEAD, "identical"), ("b" * 40, HEAD): Compare("b" * 40, HEAD, "ahead")},
    )


def test_approval_only_prs_are_kept_outside_the_quota_up_to_their_share(store):
    host = approval_host()
    (report,) = Harvester(host, store, options()).run([RepoPlan("o/r", Corpus.HUMAN, 4, "python")])
    # quota 4 -> room for int(4 * 0.25) = 1 approval-only PR; #3 has no thread at all, so it is skipped unfetched
    assert (report.added, report.approval_only) == (1, 1)
    assert report.skipped == {"approval_only_over_cap": 1} and host.calls["fetch_pr"] == 2
    assert store.get_pr(PRKey("o/r", 1)).corpus is Corpus.HUMAN
    assert store.get_pr(PRKey("o/r", 2)).corpus is Corpus.APPROVAL_ONLY
    assert store.count_prs("o/r", Corpus.HUMAN) == 1  # the quota counts only PRs with a human inline thread


# ---- bug-targeted sampling ---------------------------------------------------------------------------------


def targeted_host(requested: set[int], n: int = 6) -> FakeCodeHost:
    """PRs 1..n (listed newest first), each with one human inline thread; a human requested changes on `requested`,
    and a bot on PR 1."""
    dev, rev, bot = Actor("dev"), Actor("rev"), Actor("ci[bot]", "Bot")

    def summary(k: int) -> PRSummary:
        by = (rev,) if k in requested else ((bot,) if k == 1 else ())
        return PRSummary("o/r", k, f"Change {k}", dev, f"2026-06-{10 + k}T00:00:00Z", "main", 1, (rev,),
                         changes_requested_by=by)  # fmt: skip

    def pr(k: int) -> PullRequest:
        human = thread(comment("rev", commit=HEAD), path="p.py", tid=f"h{k}")
        return PullRequest(
            repo="o/r", number=k, title=f"Change {k}", author=dev, created_at=f"2026-06-{10 + k}T00:00:00Z",
            landed_at="2026-06-28T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=HEAD, threads=(human,),
            reviews=(Review(f"r{k}", rev, "APPROVED", "2026-06-25T00:00:00Z", HEAD),),
        )  # fmt: skip

    numbers = range(n, 0, -1)
    return FakeCodeHost(
        repos={"o/r": RepoInfo("o/r", "main", "Python")},
        pages={"o/r": [tuple(summary(k) for k in list(numbers)[:3]), tuple(summary(k) for k in list(numbers)[3:])]},
        prs={("o/r", k): pr(k) for k in numbers},
        compares={(HEAD, HEAD): Compare(HEAD, HEAD, "identical"), ("b" * 40, HEAD): Compare("b" * 40, HEAD, "ahead")},
    )


def targeted_options(limit=None, share=0.5):
    return HarvestOptions(
        slices=(JUNE,), page_size=3, line_slack=0, excluded=frozenset(), github_languages={"Python": "python"},
        fallback_language="other", limit=limit, bug_targeted_share=share,
    )  # fmt: skip


def sampled(store) -> dict[int, SampledAs]:
    return {k.number: store.get_pr(k).sampled_as for k in store.pr_keys()}


def test_a_share_of_the_quota_comes_from_prs_where_a_human_requested_changes(store):
    host = targeted_host({5, 2})
    (report,) = Harvester(host, store, targeted_options()).run([RepoPlan("o/r", Corpus.HUMAN, 4, "python")])
    # targeted first (its own walk of the listing): 5 and 2; then general, newest first: 6 and 4 (5 is stored)
    assert sampled(store) == {6: SampledAs.GENERAL, 5: SampledAs.TARGETED, 4: SampledAs.GENERAL,
                              2: SampledAs.TARGETED}  # fmt: skip
    assert (report.added, report.targeted) == (4, 2)
    assert report.skipped == {"no_changes_requested": 3, "already_stored": 1}  # 6, 4, 3; then 5 for general
    assert store.get_cursor("o/r", Corpus.HUMAN, JUNE, SampledAs.TARGETED) != Cursor()  # a cursor of its own
    assert store.pr_facts()[0].sampled_as in set(SampledAs)

    again = targeted_host({5, 2})
    assert Harvester(again, store, targeted_options()).run([RepoPlan("o/r", Corpus.HUMAN, 4, "python")])[0].added == 0
    assert again.calls["list"] == 0 and again.calls["fetch_pr"] == 0  # both samples are full: nothing listed


def test_a_targeted_listing_that_runs_out_hands_its_shortfall_to_the_general_sample(store):
    host = targeted_host({3})
    (report,) = Harvester(host, store, targeted_options()).run([RepoPlan("o/r", Corpus.HUMAN, 4, "python")])
    assert (report.added, report.targeted) == (4, 1)
    assert sampled(store) == {6: SampledAs.GENERAL, 5: SampledAs.GENERAL, 4: SampledAs.GENERAL,
                              3: SampledAs.TARGETED}  # fmt: skip
    assert store.get_cursor("o/r", Corpus.HUMAN, JUNE, SampledAs.TARGETED).exhausted

    # A later run with a bigger quota knows the targeted listing is spent and fills the slice generally.
    (more,) = Harvester(targeted_host({3}), store, targeted_options()).run([RepoPlan("o/r", Corpus.HUMAN, 6, "python")])
    assert (more.added, more.targeted) == (2, 0) and len(store.pr_keys()) == 6


def test_a_capped_run_takes_from_both_samples(store):
    (report,) = Harvester(targeted_host({5, 2}), store, targeted_options(limit=2)).run(
        [RepoPlan("o/r", Corpus.HUMAN, 4, "python")]
    )
    assert sampled(store) == {6: SampledAs.GENERAL, 5: SampledAs.TARGETED}
    assert (report.added, report.targeted) == (2, 1)


def test_ai_feedback_and_a_zero_share_sample_only_generally(store):
    host = targeted_host({5, 2})
    Harvester(host, store, targeted_options(share=0.0)).run([RepoPlan("o/r", Corpus.HUMAN, 3, "python")])
    assert set(sampled(store).values()) == {SampledAs.GENERAL} and len(store.pr_keys()) == 3
