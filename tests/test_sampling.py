import itertools

import pytest

from honed.core import sampling
from honed.core.types import Corpus, DateWindow
from honed.learn import plan


def test_spread_and_allocate():
    assert sampling.spread(15, 6) == [3, 3, 3, 2, 2, 2]
    assert sampling.spread(0, 3) == [0, 0, 0]
    assert sampling.allocate(10, [1, 10, 10]) == [1, 5, 4]
    assert sampling.allocate(100, [2, 3]) == [2, 3]


def test_largest_remainder_sums_exactly():
    shares = sampling.largest_remainder(1000, {"typescript": 0.45, "cpp": 0.25, "python": 0.20, "other": 0.10})
    assert shares == {"typescript": 450, "cpp": 250, "python": 200, "other": 100}
    odd = sampling.largest_remainder(7, {"a": 1, "b": 1, "c": 1})
    assert sum(odd.values()) == 7


def test_repo_quotas_follow_language_weights_then_spread_evenly():
    groups = {"typescript": ["t1", "t2", "t3"], "python": ["p1"]}
    quotas = sampling.repo_quotas(groups, {"typescript": 0.7, "python": 0.3}, 100)
    assert quotas == {"t1": 24, "t2": 23, "t3": 23, "p1": 30}


def test_split_window_covers_it_without_overlap():
    slices = sampling.split_window(DateWindow("2026-01-01", "2026-06-30"), 6)
    assert slices[0] == DateWindow("2026-01-01", "2026-01-30")
    assert slices[-1].end == "2026-06-30"
    for before, after in itertools.pairwise(slices):
        assert before.end < after.start


def test_corpus_plans_and_selection():
    plans = plan.corpus_plans(
        groups={"typescript": ["a/ts"], "python": ["b/py"]},
        weights={"typescript": 0.6, "python": 0.4},
        target=10,
        ai_repos=["a/ts", "c/ai"],
        ai_target=5,
    )
    assert [(p.repo, p.corpus, p.quota, p.language) for p in plans] == [
        ("a/ts", Corpus.HUMAN, 6, "typescript"),
        ("b/py", Corpus.HUMAN, 4, "python"),
        ("a/ts", Corpus.AI_FEEDBACK, 3, "typescript"),
        ("c/ai", Corpus.AI_FEEDBACK, 2, None),
    ]
    assert [p.corpus for p in plan.select(plans, ["A/TS"], [])] == [Corpus.HUMAN, Corpus.AI_FEEDBACK]
    with pytest.raises(plan.UnknownRepo):
        plan.select(plans, ["z/z"], [])


def test_excluded_repos_are_refused_even_when_named():
    plans = [plan.RepoPlan("facebook/react", Corpus.HUMAN, 5, "typescript")]
    with pytest.raises(plan.ExcludedRepo):
        plan.select(plans, ["react/react"], ["facebook/react"])
    with pytest.raises(plan.ExcludedRepo):
        plan.select(plans, None, ["facebook/react"])


def test_share_of_is_at_least_the_share_in_whole_items():
    assert sampling.share_of(10, 0.3) == 3  # 10 * 0.3 is 3.0000000000000004 in floating point
    assert sampling.share_of(56, 0.3) == 17 and sampling.share_of(20, 0.3) == 6
    assert sampling.share_of(3, 0.0) == 0 and sampling.share_of(3, 1.0) == 3 and sampling.share_of(0, 0.3) == 0
