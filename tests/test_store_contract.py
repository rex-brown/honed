"""Store contract: every Store implementation must pass these. Only SqliteStore exists so far."""

from dataclasses import replace

import pytest

from builders import ANCHOR, HEAD, comment, compare, patch, thread
from honed.adapters.blobs import BlobStore
from honed.adapters.sqlite_store import SCHEMA_VERSION, SchemaMismatch, SqliteStore
from honed.core.types import (
    Actor,
    Addressed,
    AuthorKind,
    CommitInfo,
    ContextPack,
    Corpus,
    Cursor,
    DateWindow,
    FilePatch,
    GoldIssue,
    GoldProvenance,
    GoldSet,
    HarvestedPR,
    JudgedLabel,
    Judgment,
    JudgmentKind,
    LineBasis,
    Outcome,
    PackFile,
    PackRole,
    Polarity,
    PRKey,
    PRSource,
    PullRequest,
    Review,
    SampledAs,
    Severity,
    SkippedFile,
    Stance,
    Strength,
    ThreadLabel,
)


@pytest.fixture(params=["sqlite"])
def store(request, tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    yield s
    s.close()


def harvested(number: int = 7, *, threads: int = 2, created: str = "2026-02-10T12:00:00Z") -> HarvestedPR:
    ts = tuple(
        thread(
            comment("rev", reactions=(("THUMBS_UP", "dev"),), cid=f"c{number}-{i}-0"),
            comment("dev", at="2026-02-11T00:00:00Z", cid=f"c{number}-{i}-1", line=None),
            tid=f"t{number}-{i}",
            lines=(3, 5),
            resolved=i % 2 == 0,
        )
        for i in range(threads)
    )
    pr = PullRequest(
        repo="o/r", number=number, title=f"PR {number}", author=Actor("dev"), created_at=created,
        landed_at="2026-02-20T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=HEAD, url="u", body="body",
        additions=3, deletions=1, changed_files=2, force_pushes=1, threads=ts,
        reviews=(Review("r1", Actor("rev"), "COMMENTED", "2026-02-11T00:00:00Z", ANCHOR),
                 Review("r2", None, "APPROVED", None, None)),
        commits=(CommitInfo(ANCHOR, "2026-02-10", "2026-02-10"), CommitInfo(HEAD, "2026-02-12", "2026-02-12")),
        commit_count=2,
        compares=(compare(patch("src/app.py", "@@ -4 +4 @@\n-a\n+b"), FilePatch("big.bin", "modified", None)),),
        reviewed_diff=replace(compare(patch("src/app.py", "@@ -1 +1 @@\n-x\n+y"), status="ahead"), merge_base="m" * 40),
    )  # fmt: skip
    labels = tuple(ThreadLabel(t.id, AuthorKind.HUMAN, Outcome.FIXED, True, LineBasis.COMPARE) for t in ts)
    return HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, ANCHOR, labels)


def test_upsert_then_get_round_trips(store):
    item = harvested()
    store.upsert_pr(item)
    assert store.get_pr(item.key) == item
    assert store.get_pr(PRKey("o/r", 999)) is None


def test_upsert_replaces_in_place(store):
    store.upsert_pr(harvested(threads=3))
    smaller = harvested(threads=1)
    store.upsert_pr(smaller)
    assert store.get_pr(smaller.key) == smaller
    assert len(store.thread_facts()) == 1


def test_keys_and_counts(store):
    store.upsert_pr(harvested(1, created="2026-01-15T00:00:00Z"))
    store.upsert_pr(harvested(2, created="2026-03-15T00:00:00Z"))
    assert store.has_pr(PRKey("o/r", 1)) and not store.has_pr(PRKey("o/r", 3))
    assert store.pr_keys() == [PRKey("o/r", 1), PRKey("o/r", 2)]
    assert store.pr_keys("x/y") == []
    assert store.count_prs("o/r", Corpus.HUMAN) == 2
    assert store.count_prs("o/r", Corpus.HUMAN, DateWindow("2026-01-01", "2026-01-31")) == 1
    assert store.count_prs("o/r", Corpus.AI_FEEDBACK) == 0


def test_cursors(store):
    window = DateWindow("2026-01-01", "2026-01-31")
    assert store.get_cursor("o/r", Corpus.HUMAN, window) == Cursor()
    store.set_cursor("o/r", Corpus.HUMAN, window, Cursor("abc", 3, False))
    store.set_cursor("o/r", Corpus.HUMAN, window, Cursor("def", 1, True))
    assert store.get_cursor("o/r", Corpus.HUMAN, window) == Cursor("def", 1, True)
    assert store.get_cursor("o/r", Corpus.AI_FEEDBACK, window) == Cursor()


def test_facts(store):
    store.upsert_pr(harvested(threads=2))
    (pr,) = store.pr_facts()
    assert (pr.language, pr.corpus, pr.threads) == ("python", Corpus.HUMAN, 2)
    facts = store.thread_facts()
    assert [(f.outcome, f.lines_changed, f.line_basis) for f in facts] == [(Outcome.FIXED, True, LineBasis.COMPARE)] * 2


def test_threads_before_finds_earlier_review_of_the_same_files(store):
    store.upsert_pr(harvested(1, threads=1))  # one thread on src/app.py at 2026-03-01T00:00:00Z
    assert [t.number for t in store.threads_before("o/r", ["src/app.py", "other.py"], "2026-04-01")] == [1]
    assert store.threads_before("o/r", ["src/app.py"], "2026-03-01T00:00:00Z") == []  # strictly before
    assert store.threads_before("o/r", ["nope.py"], "2027-01-01") == []
    assert store.threads_before("x/y", ["src/app.py"], "2027-01-01") == []
    (prior,) = store.threads_before("o/r", ["src/app.py"], "2027-01-01")
    assert (prior.path, prior.outcome, prior.author_kind) == ("src/app.py", Outcome.FIXED, AuthorKind.HUMAN)


def test_blobs_are_content_addressed(store):
    digest = store.put_blob(b"hello")
    assert store.put_blob(b"hello") == digest
    assert store.get_blob(digest) == b"hello"
    with pytest.raises(KeyError):
        store.get_blob("0" * 64)


def test_packs_round_trip(store):
    blob = store.put_blob(b"print(1)\n")
    pack = ContextPack(
        repo="o/r", number=7, base_commit="b" * 40, head_commit=HEAD,
        files=(PackFile("a.py", HEAD, PackRole.CHANGED, "changed by the PR", blob, 9),),
        changed_paths=("a.py",), symbols=("fit",), grep_scope=("sklearn",),
        skipped=(SkippedFile("big.py", "over max_file_bytes"),), tree_blob=None, build_seconds=1.5,
    )  # fmt: skip
    store.save_pack(pack)
    assert store.get_pack(pack.key) == pack
    assert store.get_pack(PRKey("o/r", 8)) is None


def test_sqlite_refuses_a_different_schema_version(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    with s._tx() as db:
        db.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    s.close()
    with pytest.raises(SchemaMismatch):
        SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))


def test_sqlite_reopen_keeps_data(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    s.upsert_pr(harvested())
    s.close()
    again = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    assert again.get_pr(PRKey("o/r", 7)) == harvested()
    again.close()


def test_commit_headlines_round_trip(store):
    item = harvested()
    commits = (CommitInfo(ANCHOR, "2026-02-10", "2026-02-10", "Apply suggestions from code review"),)
    item = replace(item, pr=replace(item.pr, commits=commits))
    store.upsert_pr(item)
    assert store.get_pr(item.key).pr.commits == commits


def test_set_corpus_moves_a_pr_without_refetching(store):
    store.upsert_pr(harvested())
    store.set_corpus(PRKey("o/r", 7), Corpus.APPROVAL_ONLY)
    assert store.get_pr(PRKey("o/r", 7)).corpus is Corpus.APPROVAL_ONLY
    assert store.count_prs("o/r", Corpus.HUMAN) == 0 and store.count_prs("o/r", Corpus.APPROVAL_ONLY) == 1


def test_labels_round_trip(store):
    key = PRKey("o/r", 7)
    first = Judgment("o/r", 7, "t1", JudgmentKind.ADDRESSED, "partially", "half done", model="fable")
    store.save_judgment(first)
    store.save_judgment(replace(first, value="addressed"))  # replaces
    store.save_judgment(Judgment("o/r", 7, "t1", JudgmentKind.CLASSIFY, "", "r", "security", "fable"))
    assert [(j.kind, j.value) for j in store.judgments(key)] == [
        (JudgmentKind.ADDRESSED, "addressed"), (JudgmentKind.CLASSIFY, "")]  # fmt: skip
    judged = [
        JudgedLabel("t1", AuthorKind.HUMAN, Outcome.CHANGED_UNADDRESSED, Polarity.NEUTRAL, None,
                    addressed=Addressed.NOT_ADDRESSED),
        JudgedLabel("t2", AuthorKind.AI, Outcome.RESOLVED_NO_CHANGE, Polarity.NEUTRAL, None, stance=Stance.DISAGREE,
                    category="security", high_risk_dismissal=True),
        JudgedLabel("t3", AuthorKind.HUMAN, Outcome.FIXED, Polarity.POSITIVE, Strength.STRONG, applied_suggestion=True),
    ]  # fmt: skip
    store.save_judged_labels(key, judged)
    store.save_judged_labels(key, judged)
    assert store.judged_labels(key) == judged
    assert store.get_gold(key) is None
    gold = GoldSet("o/r", 7, ANCHOR, (
        GoldIssue("o/r#7:t3", "src/app.py", 3, 5, Severity.IMPORTANT, GoldProvenance.APPLIED_SUGGESTION, 1.0,
                  "None crash", ("t3", "t9"), "correctness"),
    ), candidates=3, excluded_later_round=("t4",), excluded_unreadable=())  # fmt: skip
    store.save_gold(gold)
    store.save_gold(gold)
    assert store.get_gold(key) == gold


def test_a_phase_1_store_is_migrated_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "db.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE commits (repo TEXT NOT NULL, number INTEGER NOT NULL, ord INTEGER NOT NULL, oid TEXT NOT NULL,"
        " committed_date TEXT, authored_date TEXT, PRIMARY KEY (repo, number, ord));"
        "INSERT INTO commits VALUES ('o/r', 7, 0, 'abc', '2026-01-01', '2026-01-01');"
    )
    db.commit()
    db.close()
    s = SqliteStore(path, BlobStore(tmp_path / "blobs"))
    row = s._db.execute("SELECT message_headline FROM commits").fetchone()
    assert row[0] is None
    assert s._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    s.close()


def test_a_phase_2_store_gains_sampling_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "db.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '2');"
        "CREATE TABLE prs (repo TEXT NOT NULL, number INTEGER NOT NULL, title TEXT NOT NULL, url TEXT, body TEXT,"
        " author_login TEXT, author_type TEXT, author_kind TEXT NOT NULL, created_at TEXT NOT NULL, landed_at TEXT,"
        " base_ref TEXT, base_oid TEXT, head_oid TEXT, additions INTEGER, deletions INTEGER, changed_files INTEGER,"
        " force_pushes INTEGER, commit_count INTEGER, language TEXT NOT NULL, corpus TEXT NOT NULL,"
        " reviewed_commit TEXT NOT NULL, harvested_at TEXT NOT NULL, PRIMARY KEY (repo, number));"
        "INSERT INTO prs VALUES ('o/r', 7, 't', '', '', 'dev', 'User', 'human', '2026-01-05T00:00:00Z', NULL, 'main',"
        " 'b', 'h', 0, 0, 0, 0, 0, 'python', 'human', 'h', '2026-09-01');"
        "CREATE TABLE cursors (repo TEXT NOT NULL, corpus TEXT NOT NULL, window TEXT NOT NULL, after TEXT,"
        " offset INTEGER NOT NULL, exhausted INTEGER NOT NULL, PRIMARY KEY (repo, corpus, window));"
        "INSERT INTO cursors VALUES ('o/r', 'human', '2026-01-01..2026-01-31', 'abc', 3, 0);"
    )
    db.commit()
    db.close()
    s = SqliteStore(path, BlobStore(tmp_path / "blobs"))
    window = DateWindow("2026-01-01", "2026-01-31")
    assert s.pr_facts()[0].sampled_as is SampledAs.GENERAL
    assert s.count_prs("o/r", Corpus.HUMAN, window, SampledAs.GENERAL) == 1
    assert s.count_prs("o/r", Corpus.HUMAN, window, SampledAs.TARGETED) == 0
    assert s.get_cursor("o/r", Corpus.HUMAN, window) == Cursor("abc", 3, False)
    assert s.get_cursor("o/r", Corpus.HUMAN, window, SampledAs.TARGETED) == Cursor()
    s.set_cursor("o/r", Corpus.HUMAN, window, Cursor("t", 1, True), SampledAs.TARGETED)
    assert s.get_cursor("o/r", Corpus.HUMAN, window) == Cursor("abc", 3, False)  # a cursor per sample
    assert s._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    s.close()


def test_candidates_and_policy_versions_round_trip(store):
    from honed.core.improve import (
        CandidateRecord,
        EditKind,
        GateVerdict,
        GeneratorKind,
        Outcome,
        PolicyEdit,
        PolicyRecord,
        Proposal,
        RuleResult,
        SelfReview,
    )

    edit = PolicyEdit(EditKind.LESSON_ADD, lesson={"id": "x", "kind": "prompt", "evidence": ["o/r#1"]})
    proposal = Proposal(GeneratorKind.LESSON_MINER, "h", "c", edit, ("o/r#1",), "needs judgment", "m")
    verdict = GateVerdict(False, (RuleResult("real_gain", False, "d", {"delta_S": -0.1, "ci": [1, 2]}),), 0.01, True)
    record = CandidateRecord("c1", 2, proposal, "p" * 64, "q" * 64, "diff", Outcome.REJECTED, "n", False,
                             SelfReview(0, ({"title": "t", "bucket": "noted"},), 0.3), "run1", verdict,
                             "2026-10-01T00:00:00Z")  # fmt: skip
    store.save_candidate(record)
    assert store.candidates() == [record]
    version = PolicyRecord("q" * 64, "p" * 64, "2026-10-01T00:00:01Z", "why", "diff", {"real_gain": {"delta_S": 0.1}},
                           True, {"config.toml": "x = 1\n"}, "c1")  # fmt: skip
    store.save_policy_version(version)
    assert store.policy_version("q" * 64) == version and store.policy_versions() == [version]
    assert store.policy_version("nope") is None


def test_a_phase_3_store_gains_the_improve_tables(tmp_path):
    import sqlite3

    path = tmp_path / "db.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                     "INSERT INTO meta VALUES ('schema_version', '4');")  # fmt: skip
    db.commit()
    db.close()
    s = SqliteStore(path, BlobStore(tmp_path / "blobs"))
    assert s.candidates() == [] and s.policy_versions() == [] and s.benchmark_prs() == []
    version = s._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version == str(SCHEMA_VERSION) == "6"
    s.close()


def test_a_schema_5_store_gains_source_and_split_in_place(tmp_path):
    path, blobs = tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs")
    s = SqliteStore(path, blobs)
    s.upsert_pr(harvested(7))
    with s._tx() as db:  # back to schema 5: no source or split column
        db.execute("ALTER TABLE prs DROP COLUMN source")
        db.execute("ALTER TABLE prs DROP COLUMN split")
        db.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    s.close()
    s = SqliteStore(path, blobs)
    (fact,) = s.pr_facts()
    assert (fact.source, fact.split) == (PRSource.CORPUS, None)
    assert s.get_pr(PRKey("o/r", 7)) == harvested(7)
    s.upsert_pr(replace(harvested(8), source=PRSource.BENCHMARK, split="test"))  # positional insert still lines up
    assert s.get_pr(PRKey("o/r", 8)).source is PRSource.BENCHMARK
    s.close()


def test_split_round_diffs_and_benchmark_records_round_trip(store):
    from honed.core.benchmarks import AACR, BenchmarkComment, BenchmarkPR

    store.upsert_pr(harvested(7))
    key = PRKey("o/r", 7)
    store.set_split(key, "validation")
    assert store.get_pr(key).split == "validation" and store.pr_facts()[0].split == "validation"
    store.set_split(key, None)
    assert store.get_pr(key).split is None

    diff = replace(compare(FilePatch("src/app.py", "modified", None)), head="r" * 40, merge_base="m" * 40)
    store.save_round_diff(key, diff)
    blob = store.put_blob(b"x")
    pack = ContextPack(repo="o/r", number=7, base_commit="m" * 40, head_commit="s" * 40,
                       files=(PackFile("a.py", "s" * 40, PackRole.CHANGED, "c", blob, 1),),
                       diff=replace(diff, head="s" * 40))  # fmt: skip
    store.save_round_pack(pack)
    assert [d.head for d in store.round_diffs(key)] == ["r" * 40, "s" * 40]  # kept alone, and from a round pack
    assert store.round_diffs(PRKey("o/r", 8)) == []

    record = BenchmarkPR(AACR, "https://github.com/o/r/pull/7", "o/r", 7, "t", "Python", "b" * 40, HEAD,
                         comments=(BenchmarkComment("bug", category="Code Defect", path="a.py", start_line=3,
                                                    end_line=4), BenchmarkComment("meh", valid=False)))  # fmt: skip
    store.save_benchmark_pr(record, key)
    assert store.benchmark_prs() == [(key, record)]
