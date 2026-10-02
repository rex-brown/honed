"""The feed selector (ARCHITECTURE.md section 7, Splits; `learn/feed.py`): the improve loop's feed sample (stratified
by language, PRs with a gold issue first, Important ones at the pool's share, fixed by the incumbent's hash and
recorded in the round report), the time cut on AI-feedback PRs, and the evidence a mined lesson may cite. Scripted
models only."""

from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from evalkit import _pr, build_store
from honed.adapters.policy_dir import PolicyDirectory
from honed.core.improve import EditKind, GeneratorKind, PolicyEdit, Proposal
from honed.core.sampling import largest_remainder
from honed.core.types import AuthorKind, Corpus, HarvestedPR, PRKey
from honed.learn import feed, lessons, policy_edit, splits
from honed.learn.feed import FeedOptions, FeedPR, FeedSelector
from honed.learn.improve import ImproveLoop
from reviewkit import ROOT, seed_policy
from test_improve import CODEC, NEW_CHECK, LoopLLM, _options, _sensitivity_rows, _services, answer

CUT = "2026-04-01T00:00:00Z"


# ---- the draw ----------------------------------------------------------------------------------------------


def _pool() -> list[FeedPR]:
    """TypeScript: 40 PRs (every fifth has 2 rounds), 10 with an Important issue, 20 with Nits only, 6 without a
    gold issue, 4 clean; Python: 20 PRs, 4 with an Important issue, 10 with Nits only, 6 without; other: 5 PRs."""
    out = []
    for n in range(40):
        important, issues = (1, 2) if n < 10 else (0, 1) if n < 30 else (0, 0)
        out.append(FeedPR(PRKey("o/ts", n), "typescript", (1, 2) if n % 5 == 0 else (1,), n < 36, issues, important))
    for n in range(20):
        important, issues = (1, 1) if n < 4 else (0, 3) if n < 14 else (0, 0)
        out.append(FeedPR(PRKey("o/py", n), "python", (1,), True, issues, important))
    out += [FeedPR(PRKey("o/go", n), "other", (1,), True, 1, 0) for n in range(5)]
    return out


def test_the_draw_is_stratified_by_language_and_prefers_prs_with_a_gold_issue():
    pool = _pool()
    sample = feed.draw(pool, 30, salt="incumbent-a")
    pool_rounds = {"typescript": 48, "python": 20, "other": 5}
    quotas = largest_remainder(30, pool_rounds)
    assert len(sample.units) == 30 and sample.composition["all"]["pr_rounds"] == 30
    for language, quota in quotas.items():
        cell = sample.composition[language]
        assert cell["pr_rounds"] == quota and cell["pool_pr_rounds"] == pool_rounds[language]
        assert cell["with_gold_issue"] == cell["prs"]  # every language has gold PRs to spare: no gold-less PR drawn
    by_key = {p.key: p for p in pool}
    assert all(by_key[k].issues > 0 for k in sample.keys)
    assert [u for k in sample.keys for u in by_key[k].units()] == list(sample.units)  # whole PRs, every round
    assert any(u.endswith("@2") for u in sample.units) and sample.composition["all"]["later_rounds"] > 0


def test_important_prs_are_drawn_at_the_pools_share_per_language():
    pool = _pool()
    for salt in ("a", "b", "c", "d"):
        sample = feed.draw(pool, 30, salt=salt)
        for language in ("typescript", "python"):
            cell = sample.composition[language]
            share = cell["pool_important_share"]  # of the language's human-review PRs: 10/36 and 4/20
            assert share == {"typescript": round(10 / 36, 4), "python": 0.2}[language]
            assert abs(cell["with_important"] - share * cell["prs"]) <= 1


def test_the_draw_is_fixed_by_its_salt_and_a_new_incumbent_redraws_it():
    pool = _pool()
    first = feed.draw(pool, 30, salt="incumbent-a")
    assert first == feed.draw(list(reversed(pool)), 30, salt="incumbent-a")  # deterministic, order-independent
    assert any(feed.draw(pool, 30, salt=f"incumbent-{s}").keys != first.keys for s in "bcde")


def test_a_small_pool_is_taken_whole_and_a_two_round_pr_never_overshoots_the_quota():
    pool = _pool()
    everything = feed.draw(pool, 1000, salt="x")
    assert len(everything.units) == 73 and len(everything.keys) == 65
    tight = [FeedPR(PRKey("o/r", 1), "python", (1, 2), True, 1, 0), FeedPR(PRKey("o/r", 2), "python", (1,), False)]
    one = feed.draw(tight, 1, salt="x")
    assert one.units == ("o/r#2@1",)  # the gold PR's two rounds don't fit one slot: the clean PR fills it
    assert feed.draw([], 10, salt="x").keys == ()


# ---- the time cut and the evidence a lesson may cite --------------------------------------------------------


def test_the_time_cut_admits_only_prs_created_before_their_languages_validation_period():
    cuts = {"python": CUT, "typescript": "2026-04-20T00:00:00Z"}
    assert feed.admitted("2026-03-31T23:59:59Z", "python", cuts)
    assert not feed.admitted(CUT, "python", cuts) and not feed.admitted("2026-05-02T00:00:00Z", "python", cuts)
    assert feed.admitted("2026-04-10T00:00:00Z", "typescript", cuts)
    assert not feed.admitted("2026-04-10T00:00:00Z", "cpp", cuts)  # no cut of its own: the earliest one
    assert feed.admitted("2026-03-10T00:00:00Z", "cpp", cuts)
    assert not feed.admitted("2026-01-01T00:00:00Z", "python", {})  # no validation split: nothing


def test_validation_cuts_are_where_each_languages_validation_period_begins():
    from test_rounds_splits import FRACTIONS, fact

    facts = [fact("o/py", n, "python", n) for n in range(1, 11)] + [fact("o/ts", n, "typescript", n + 10)
                                                                     for n in range(1, 11)]  # fmt: skip
    items = splits.eligible(facts, {PRKey(f.repo, f.number) for f in facts}, ())
    cuts = splits.validation_cuts(items, splits.assign(items, FRACTIONS))
    assert cuts == {"python": "2026-01-07T00:00:00Z", "typescript": "2026-01-17T00:00:00Z"}  # rank 6 of 10 each


def _with_ai_feedback(store) -> None:
    """AI-feedback PRs 10 (March, before the cut) and 11 (April 15, after it), by authors cy and dee."""
    for number, created, author in ((10, "2026-03-05T00:00:00Z", "cy"), (11, "2026-04-15T00:00:00Z", "dee")):
        pr = replace(_pr(1, author=author), number=number, created_at=created)
        store.upsert_pr(HarvestedPR(pr, "python", Corpus.AI_FEEDBACK, AuthorKind.HUMAN, pr.head_oid))


@pytest.fixture
def store(tmp_path):
    store = build_store(tmp_path, humans=("ann", "bob"))
    _with_ai_feedback(store)
    yield store
    store.close()


def _selector(store, cuts=None) -> FeedSelector:
    ai = (PRKey("o/r", 10), PRKey("o/r", 11))
    return FeedSelector(store, store, FeedOptions(150, 2, {"python": CUT} if cuts is None else cuts, ai))


def test_a_lesson_may_cite_ai_feedback_prs_from_before_the_validation_cut_only(store):
    train = {k: store.get_pr(k) for k in (PRKey("o/r", 1), PRKey("o/r", 2))}
    selector = _selector(store)
    assert set(selector.ai_feedback()) == {PRKey("o/r", 10)}
    evidence = selector.evidence(train)
    assert set(evidence) == {PRKey("o/r", 1), PRKey("o/r", 2), PRKey("o/r", 10)}
    assert evidence[PRKey("o/r", 10)].author == "cy" and evidence[PRKey("o/r", 10)].files

    def problems(*cited: str) -> list[str]:
        lesson = {**NEW_CHECK, "evidence": [f"{c}: unchecked index" for c in cited]}
        edit = PolicyEdit(EditKind.LESSON_ADD, lesson=lesson)
        _, candidate = policy_edit.apply(seed_policy(), edit, CODEC)
        proposal = Proposal(GeneratorKind.LESSON_MINER, "h", "c", edit)
        return lessons.problems(proposal, candidate, seed_policy(), evidence, lessons.LessonRules(2, 2))

    assert problems("o/r#1", "o/r#10") == []  # ann and cy: two PRs by two authors, the AI-feedback one in time
    late = problems("o/r#1", "o/r#11")
    assert any("outside the feed split" in p and "o/r#11" in p for p in late)
    assert _selector(store, cuts={}).ai_feedback() == {}  # no validation split, no AI-feedback PR


def test_the_pool_holds_the_packed_replayed_rounds_with_stored_gold_counts(store):
    keys = (PRKey("o/r", 1), PRKey("o/r", 2), PRKey("o/r", 3))
    unpacked = _pr(5, author="eve")
    store.upsert_pr(HarvestedPR(unpacked, "python", Corpus.HUMAN, AuthorKind.HUMAN, unpacked.head_oid))
    items = {k: store.get_pr(k) for k in (*keys, PRKey("o/r", 5))}
    pool = {p.key: p for p in _selector(store).pool(items)}
    assert set(pool) == set(keys)  # PR 5 has no context pack: out of the pool, not a skipped sample unit
    assert pool[PRKey("o/r", 1)] == FeedPR(PRKey("o/r", 1), "python", (1,), True, 1, 1)
    assert pool[PRKey("o/r", 2)] == FeedPR(PRKey("o/r", 2), "python", (1,), False, 0, 0)  # the clean PR


# ---- the loop ----------------------------------------------------------------------------------------------


TRAIN = [PRKey("o/r", n) for n in (1, 2, 3)]  # human PRs 1 and 3 (an Important gold issue each), clean PR 2
VALIDATION = [PRKey("o/r", n) for n in (4, 5)]


@pytest.fixture
def loop_store(tmp_path):
    store = build_store(tmp_path, humans=("ann", "bob", "cid", "dan"))  # human PRs 1, 3, 4, 5; clean PR 2
    directory = PolicyDirectory(tmp_path / "policy")
    shutil.copytree(ROOT / "policy", directory.path)
    for row in _sensitivity_rows(separates=True, min_gain=0.01):
        store.add_decision(replace(row, id=None))
    yield store, directory
    store.close()


def _feed_loop(store, directory, rounds: int, **services):
    config_edit = answer("config_set", settings=[{"key": "rank.confidence_threshold", "value_json": "1.0"}],
                         hypothesis="Posting only certain findings removes false positives.")  # fmt: skip
    llm = LoopLLM({"reflective": [config_edit] * rounds})
    built = _services(store, directory, llm, split_of={"train": TRAIN, "validation": VALIDATION},
                      feed_options=FeedOptions(sample_rounds=2, review_rounds=1, cuts={"python": CUT}),
                      **services)  # fmt: skip
    options = _options(rounds=rounds, candidates_per_round=1, generators=(GeneratorKind.REFLECTIVE,),
                       feed_split="train", gate_split="validation")  # fmt: skip
    return ImproveLoop(built, options), built, llm


def test_the_loop_mines_the_feed_sample_and_records_its_pr_rounds(loop_store):
    store, directory = loop_store
    evaluated, labels = [], []
    loop, _, llm = _feed_loop(store, directory, 2, evaluated=evaluated, labels=labels)
    first, second = loop.run().rounds
    assert not first.promoted  # the incumbent stays, so the second round draws the same sample
    assert first.feed is not None and first.feed == second.feed
    assert first.feed["units"] == ["o/r#1@1", "o/r#3@1"]  # the gold PRs, not the clean one
    assert first.feed["split"] == "train" and first.feed["salt"] == first.incumbent[:12]
    assert first.feed["composition"]["python"]["with_important"] == 2 and first.feed["eval_run"]
    assert first.to_json()["feed"]["pr_rounds"] == 2 and any(n.startswith("feed: 2 PR-rounds") for n in first.notes)
    assert "train/feed" in labels and "train" not in labels  # never the whole feed split
    feed_runs = [k for h, k in evaluated if h == first.incumbent and k == (TRAIN[0], TRAIN[2])]
    assert len(feed_runs) == 1  # evaluated once, reused by the second round
    propose_call = next(c for c in llm.calls if c.stage.startswith("propose:"))
    assert "o/r#1" in propose_call.user and "o/r#4" not in propose_call.user  # failures from the feed, not the gate


def test_a_round_stopped_on_the_feed_evaluation_still_records_its_sample(loop_store):
    from honed.ports.llm import UsageLimitReached

    store, directory = loop_store
    loop, services, llm = _feed_loop(store, directory, 1)
    evaluate = services.evaluate

    def limited(policy, split, subset=None, suffix=splits.SCREEN_SPLIT_SUFFIX):
        if suffix == splits.FEED_SPLIT_SUFFIX and subset is not None:
            raise UsageLimitReached("plan window at 80%")
        return evaluate(policy, split, subset, suffix)

    services.evaluate = limited
    (round_,) = loop.run().rounds
    assert round_.stopped.startswith("UsageLimitReached") and not round_.attempts
    assert round_.feed["units"] == ["o/r#1@1", "o/r#3@1"] and round_.feed["eval_run"] is None
    assert not [c for c in llm.calls if c.stage.startswith("propose:")]
