"""Review rounds, round selection, the per-round gold rule, splits, the bootstrap over PR-rounds, `--data-dir`,
and diff-file parsing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from builders import comment, thread
from honed import config
from honed.core import patches, scoring
from honed.core.rounds import ReviewRound, review_rounds, select_rounds, target_round
from honed.core.types import (
    Actor,
    Corpus,
    EvalResult,
    Finding,
    FindingClass,
    GoldIssue,
    GoldProvenance,
    Match,
    PRFact,
    PRKey,
    PRSource,
    PullRequest,
    Severity,
)
from honed.learn import splits
from reviewkit import ROOT, SETTINGS, STORE

R1, R2, R3, R4 = "1" * 40, "2" * 40, "3" * 40, "4" * 40


def pr_with(threads) -> PullRequest:
    return PullRequest("o/r", 1, "t", Actor("dev"), "2026-03-01T00:00:00Z", None, "main", "b" * 40, R4,
                       threads=tuple(threads))  # fmt: skip


def t(tid: str, commit: str, at: str):
    return thread(comment("rev", commit=commit, at=at, cid=f"{tid}-0"), tid=tid)


def test_rounds_come_from_thread_anchor_commits():
    threads = [t("a", R1, "2026-03-01T10:00:00Z"), t("b", R2, "2026-03-02T10:00:00Z"),
               t("c", R2, "2026-03-02T11:00:00Z"), t("d", R3, "2026-03-03T10:00:00Z"),
               t("e", R4, "2026-02-28T10:00:00Z"), t("x", R3, "2026-03-03T09:00:00Z")]  # fmt: skip
    rounds = review_rounds(pr_with(threads), R1, {"a", "b", "c", "d", "e"})
    assert [(r.index, r.commit, r.thread_ids) for r in rounds] == [
        (1, R1, ("a", "e")),
        (2, R2, ("b", "c")),
        (3, R3, ("d",)),
    ]  # "e" predates round 1, "x" is not a human thread
    assert [r.index for r in select_rounds(rounds, 2)] == [1, 2] and [r.index for r in select_rounds(rounds, 1)] == [1]


def test_each_issue_counts_once_in_the_latest_replayed_round_that_has_its_code():
    replayed = [ReviewRound(1, R1, ""), ReviewRound(3, R3, "")]
    everywhere = lambda commit: True  # noqa: E731
    assert target_round(2, replayed, everywhere) == 1  # round 2 isn't replayed: its code was already at round 1
    assert target_round(3, replayed, everywhere) == 3
    assert target_round(3, replayed, lambda c: c == R1) == 1
    assert target_round(2, replayed, lambda c: c == R3) is None  # the code came later than the thread's round


def fact(repo: str, n: int, lang: str, day: int, corpus: Corpus = Corpus.HUMAN) -> PRFact:
    return PRFact(repo, n, lang, corpus, None, f"2026-01-{day:02d}T00:00:00Z", 1)  # type: ignore[arg-type]


def test_splits_are_time_ordered_per_language_and_skip_held_out_repos():
    facts = [fact("o/ts", n, "typescript", n) for n in range(1, 11)] + [fact("o/py", n, "python", n) for n in (1, 2)]
    facts += [fact("getsentry/sentry", 1, "python", 3), fact("o/ts", 99, "typescript", 20, Corpus.APPROVAL_ONLY),
              fact("o/ts", 50, "typescript", 5, Corpus.AI_FEEDBACK)]  # fmt: skip
    gold = {PRKey(f.repo, f.number) for f in facts if f.number != 10}
    items = splits.eligible(facts, gold, SETTINGS.corpus.excluded)
    assert {i.key.repo for i in items} == {"o/ts", "o/py"} and PRKey("o/ts", 10) not in {i.key for i in items}
    assigned = splits.assign(items, SETTINGS.eval.split_fractions)
    ts = sorted((k.number for k, s in assigned.items() if k.repo == "o/ts" and s == "train"))
    assert ts == [1, 2, 3, 4, 5] and assigned[PRKey("o/ts", 99)] == "test"  # 9 with gold: 5, 2, 2; clean by date
    assert len(splits.select("dev", items, SETTINGS.eval.split_fractions, "dev")) == len(items)


FRACTIONS = {"train": 0.6, "validation": 0.2, "test": 0.2}


def ranked(assigned: dict[PRKey, str], repo: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for key, split in sorted(assigned.items(), key=lambda kv: kv[0].number):
        if key.repo == repo:
            out.setdefault(split, []).append(key.number)
    return out


def test_validation_takes_its_floor_per_language_out_of_train_never_out_of_test():
    items = splits.eligible([fact("o/go", n, "other", n) for n in range(1, 21)], {PRKey("o/go", n) for n in
                            range(1, 21)}, ())  # fmt: skip
    plain = ranked(splits.assign(items, FRACTIONS), "o/go")
    assert [len(plain[s]) for s in ("train", "validation", "test")] == [12, 4, 4]
    floored = ranked(splits.assign(items, FRACTIONS, min_validation=7), "o/go")
    assert floored == {"train": list(range(1, 10)), "validation": list(range(10, 17)), "test": list(range(17, 21))}
    tiny = ranked(splits.assign(items, FRACTIONS, min_validation=50), "o/go")
    assert "train" not in tiny and len(tiny["test"]) == 4  # a floor larger than the language empties train only


def test_clean_prs_follow_the_human_prs_cut_dates():
    facts = [fact("o/py", n, "python", n) for n in range(1, 11)]  # train 1-6, validation 7-8, test 9-10
    facts += [fact("o/py", 100 + day, "python", day, Corpus.APPROVAL_ONLY) for day in (3, 6, 7, 8, 9, 25)]
    items = splits.eligible(facts, {PRKey("o/py", n) for n in range(1, 11)}, ())
    clean = {k.number - 100: s for k, s in splits.assign(items, FRACTIONS).items() if k.number > 100}
    assert clean == {3: "train", 6: "train", 7: "validation", 8: "validation", 9: "test", 25: "test"}
    only_clean = splits.eligible([fact("o/rs", n, "other", n, Corpus.APPROVAL_ONLY) for n in range(1, 6)], (), ())
    alone = sorted(splits.assign(only_clean, FRACTIONS, min_validation=30).values())
    assert alone == ["test", "train", "train", "train", "validation"]  # ranked among themselves, no floor


def test_the_private_half_of_test_is_stratified_spread_over_time_and_deterministic():
    facts = [fact("o/ts", n, "typescript", n % 28 + 1) for n in range(1, 41)]
    facts += [fact("o/py", n, "python", n) for n in range(1, 21)]
    facts += [fact("o/py", 200 + n, "python", 25, Corpus.APPROVAL_ONLY) for n in range(4)]
    gold = {PRKey(f.repo, f.number) for f in facts if f.corpus is Corpus.HUMAN}
    items = splits.eligible(facts, gold, ())
    first = splits.assign(items, FRACTIONS, private_share=0.5)
    assert first == splits.assign(list(reversed(items)), FRACTIONS, private_share=0.5)  # order-independent
    plain = splits.assign(items, FRACTIONS)
    assert {k for k, s in plain.items() if s == "test"} == {k for k, s in first.items() if s.startswith("test-")}
    assert {k: s for k, s in plain.items() if s != "test"} == {k: s for k, s in first.items()
                                                               if not s.startswith("test-")}  # fmt: skip
    for repo, corpus in (("o/ts", Corpus.HUMAN), ("o/py", Corpus.HUMAN), ("o/py", Corpus.APPROVAL_ONLY)):
        tested = sorted((f for f in facts if f.repo == repo and f.corpus is corpus
                         and first[PRKey(f.repo, f.number)].startswith("test-")),
                        key=lambda f: (f.created_at, f.number))  # fmt: skip
        halves = [first[PRKey(f.repo, f.number)] for f in tested]
        assert halves.count("test-private") == len(halves) // 2  # stratified by language and corpus
        for pair in zip(halves[0::2], halves[1::2], strict=False):
            assert sorted(pair) == ["test-private", "test-public"]  # one of each consecutive pair: same weeks


def test_test_selects_the_whole_family_and_dev_refuses_a_store_with_stored_splits():
    facts = [fact("o/py", n, "python", n) for n in range(1, 11)]
    facts[0] = PRFact(**{**facts[0].__dict__, "split": "test-private"})
    facts[1] = PRFact(**{**facts[1].__dict__, "split": "test-public"})
    items = splits.eligible(facts, {PRKey("o/py", n) for n in range(1, 11)}, ())
    test = splits.select("test", items, FRACTIONS, "dev")
    assert {1, 2} <= {k.number for k in test} and len(test) == 4  # stored ones plus 2 of the 8 time-ordered
    assert [k.number for k in splits.select("test-private", items, FRACTIONS, "dev")] == [1]
    with pytest.raises(splits.SplitError, match=r"--split train .* --split validation"):
        splits.select("dev", items, FRACTIONS, "dev")  # it would silently mean train plus validation
    benchmark = splits.eligible([*facts[2:], replace(fact("o/bench", 1, "python", 5), source=PRSource.BENCHMARK)],
                                {PRKey("o/py", n) for n in range(1, 11)} | {PRKey("o/bench", 1)}, ())  # fmt: skip
    dev = splits.select("dev", benchmark, FRACTIONS, "dev")  # a benchmark PR's split is not a stored split
    assert {k.number for k in dev} == set(range(3, 11)) and PRKey("o/bench", 1) not in dev
    with pytest.raises(splits.SplitError, match="unknown split"):
        splits.select("holdout", items, FRACTIONS, "dev")


def test_the_dev_split_refuses_on_the_command_line_and_points_to_train_and_validation(tmp_path, capsys):
    from honed import cli
    from honed.adapters.blobs import BlobStore
    from honed.adapters.sqlite_store import SqliteStore
    from honed.core.types import AuthorKind, GoldSet, HarvestedPR

    store = SqliteStore(tmp_path / STORE, BlobStore(tmp_path / "blobs"))
    for n in range(1, 11):
        pr = replace(pr_with([]), number=n, created_at=f"2026-01-{n:02d}T00:00:00Z")
        store.upsert_pr(HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, R1))
        store.save_gold(GoldSet("o/r", n, R1, ()))
    store.close()
    config = ["--config", str(ROOT / "honed.toml"), "--data-dir", str(tmp_path)]
    assert cli.main([*config, "export-gold", "--split", "dev", "--out", str(tmp_path / "out")]) == 0  # no stored splits
    assert cli.main([*config, "splits", "--assign"]) == 0
    capsys.readouterr()
    for command in (["eval"], ["eval", "--split", "dev"], ["improve", "--split", "dev"],
                    ["export-gold", "--split", "dev", "--out", str(tmp_path / "out")]):  # fmt: skip
        assert cli.main([*config, *command]) == 2, command
        err = capsys.readouterr().err
        assert "stored splits" in err and "--split train" in err and "--split validation" in err, command


def test_splits_assign_stores_once_and_never_reshuffles(tmp_path, capsys):
    from honed import cli
    from honed.adapters.blobs import BlobStore
    from honed.adapters.sqlite_store import SqliteStore
    from honed.core.types import AuthorKind, GoldSet, HarvestedPR

    store = SqliteStore(tmp_path / STORE, BlobStore(tmp_path / "blobs"))
    for n in range(1, 41):
        corpus = Corpus.AI_FEEDBACK if n > 36 else Corpus.APPROVAL_ONLY if n > 30 else Corpus.HUMAN
        pr = replace(pr_with([]), number=n, created_at=f"2026-0{1 + n % 6}-{n % 27 + 1:02d}T00:00:00Z")
        store.upsert_pr(HarvestedPR(pr, "python", corpus, AuthorKind.HUMAN, R1))
        if corpus is Corpus.HUMAN:
            store.save_gold(GoldSet("o/r", n, R1, ()))
    store.close()
    config = ["--config", str(ROOT / "honed.toml"), "--data-dir", str(tmp_path)]
    assert cli.main([*config, "splits"]) == 0
    assert "40 don't" in capsys.readouterr().out  # a dry run stores nothing
    assert cli.main([*config, "splits", "--assign"]) == 0
    assert "stored: 40 PRs assigned now" in capsys.readouterr().out
    store = SqliteStore(tmp_path / STORE, BlobStore(tmp_path / "blobs"))
    first = {f.number: f.split for f in store.pr_facts()}
    assert {first[n] for n in range(37, 41)} == {"train"}  # AI-feedback PRs are train
    assert {"train", "validation", "test-public", "test-private"} == set(first.values())
    store.upsert_pr(HarvestedPR(replace(pr_with([]), number=99, created_at="2026-01-01T00:00:00Z"), "python",
                                Corpus.APPROVAL_ONLY, AuthorKind.HUMAN, R1))  # fmt: skip
    store.close()
    assert cli.main([*config, "splits", "--assign"]) == 0
    assert "stored: 1 PRs assigned now" in capsys.readouterr().out  # only the new PR; nothing stored moved
    store = SqliteStore(tmp_path / STORE, BlobStore(tmp_path / "blobs"))
    again = {f.number: f.split for f in store.pr_facts()}
    store.close()
    assert {n: s for n, s in again.items() if n != 99} == first and again[99] is not None


def _result(n: int, rnd: int, tp: bool) -> EvalResult:
    gold = GoldIssue(f"g{n}{rnd}", "a.py", 1, 1, Severity.IMPORTANT, GoldProvenance.HUMAN_FIXED, 1.0)
    f = Finding(f"f{n}{rnd}", "a.py", 1, 1, Severity.IMPORTANT, "correctness", "x")
    match = Match(f.id, FindingClass.TP if tp else FindingClass.FP, gold.id if tp else None)
    return EvalResult(PRKey("o/r", n), "python", (gold,), (f,), (match,), round=rnd)


def test_the_bootstrap_resamples_whole_prs_with_their_rounds():
    params = SETTINGS.metrics.scoring_params()
    good = [_result(n, r, True) for n in (1, 2, 3) for r in (1, 2)]
    bad = [_result(n, r, r == 1) for n in (1, 2, 3) for r in (1, 2)]
    boot = scoring.paired_bootstrap(good, bad, params, resamples=200, seed=3, ci_level=0.95)
    assert boot.delta > 0 and boot.low > 0 and boot.sd >= 0
    same = scoring.paired_bootstrap(good, good, params, resamples=50, seed=3, ci_level=0.95)
    assert same.delta == same.low == same.high == same.sd == 0
    assert scoring.min_gain(0.001, floor=0.01, multiplier=2.0) == 0.01
    assert scoring.min_gain(0.02, floor=0.01, multiplier=2.0) == 0.04


def test_data_dir_moves_every_data_path_and_nothing_else(tmp_path):
    settings = config.relocate_data(config.load(ROOT / "honed.toml"), tmp_path / "copy")
    p = settings.paths
    for path in (p.data, p.sqlite, p.blobs, p.clones, p.cache, p.llm_status, p.reports):
        assert path.is_relative_to(tmp_path / "copy")
    assert p.sqlite == tmp_path / "copy" / STORE and p.llm_status == tmp_path / "copy" / "llm_status.json"
    assert p.yardstick_prompts == ROOT / "yardstick" / "prompts" and p.policy == ROOT / "policy"
    assert p.human_labels == ROOT / "yardstick" / "human_labels"  # the yardstick's, not the data's


def test_a_git_diff_file_parses_into_file_patches():
    text = (
        "diff --git a/src/x.py b/src/x.py\nindex 1..2 100644\n--- a/src/x.py\n+++ b/src/x.py\n"
        "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
        "diff --git a/n.ts b/n.ts\nnew file mode 100644\n--- /dev/null\n+++ b/n.ts\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
    )
    x, n, moved = patches.parse_diff_file(text)
    assert (x.path, x.status, patches.new_ranges(x.patch or "")) == ("src/x.py", "modified", [(1, 3)])
    assert (n.path, n.status) == ("n.ts", "added")
    assert (moved.path, moved.status, moved.previous_path) == ("b.py", "renamed", "a.py")
