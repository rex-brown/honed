"""The blind human-labeling sample: stratified across repos and outcomes, located by commit and flagged lines (no code),
redacted, no answers."""

from __future__ import annotations

import json

import pytest

from builders import ANCHOR, HEAD, comment, thread
from evalkit import build_store
from honed import cli
from honed.adapters.blobs import BlobStore
from honed.adapters.github_parse import comment_database_id, discussion_url
from honed.adapters.sqlite_store import SqliteStore
from honed.core.sampling import round_robin
from honed.core.types import (
    Actor,
    AuthorKind,
    Corpus,
    HarvestedPR,
    JudgedLabel,
    LineBasis,
    Outcome,
    PRKey,
    PullRequest,
    ThreadLabel,
)
from honed.learn import audit_sample
from reviewkit import ROOT, STORE

FIELDS = {"id", "repo", "pr", "path", "language", "comment", "author", "url", "commit", "lines"}


def harvested(repo: str, number: int, specs: list[tuple[str, AuthorKind, Outcome]], *, side: str = "RIGHT",
              body: str = "") -> HarvestedPR:  # fmt: skip
    threads, labels = [], []
    for tid, kind, outcome in specs:
        login = "coderabbitai[bot]" if kind is AuthorKind.AI else "rev"
        threads.append(thread(comment(login, body=body or f"comment {tid}", typename="Bot" if kind is AuthorKind.AI
                                      else "User", line=20, cid=f"c-{tid}"), lines=(20, 21), tid=tid,
                              side=side))  # fmt: skip
        labels.append(ThreadLabel(tid, kind, outcome, outcome is Outcome.FIXED, LineBasis.COMPARE))
    pr = PullRequest(
        repo=repo, number=number, title="t", author=Actor("dev"), created_at="2026-03-01T00:00:00Z",
        landed_at="2026-03-05T00:00:00Z", base_ref="main", base_oid="b" * 40, head_oid=HEAD,
        url=f"https://github.com/{repo}/pull/{number}", threads=tuple(threads),
    )  # fmt: skip
    return HarvestedPR(pr, "python", Corpus.HUMAN, AuthorKind.HUMAN, ANCHOR, tuple(labels))


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    fixed, ignored, down = Outcome.FIXED, Outcome.IGNORED, Outcome.THUMBS_DOWN
    s.upsert_pr(harvested("a/one", 1, [(f"a{i}", AuthorKind.HUMAN, fixed) for i in range(8)]
                          + [("a-ai", AuthorKind.AI, ignored), ("a-own", AuthorKind.PR_AUTHOR, ignored)]))  # fmt: skip
    s.upsert_pr(harvested("b/two", 2, [("b1", AuthorKind.HUMAN, fixed), ("b2", AuthorKind.HUMAN, down)]))
    s.upsert_pr(harvested("held/out", 3, [("h1", AuthorKind.HUMAN, fixed)]))
    s.upsert_pr(harvested("c/old", 4, [("o1", AuthorKind.HUMAN, fixed)], side="LEFT"))  # old-side lines: not sampled
    # the judge found b1's change did not address it: the judged outcome is its stratum
    s.save_judged_labels(PRKey("b/two", 2), [JudgedLabel("b1", AuthorKind.HUMAN, Outcome.CHANGED_UNADDRESSED, None,
                                                         None)])  # fmt: skip
    yield s
    s.close()


def link(item, t):
    return discussion_url(item.pr.url, t.first.id)


def export(store, n):
    return audit_sample.export(store, store, n=n, excluded=["held/out"], link=link)


def test_the_sample_spreads_over_repos_then_outcomes_and_skips_held_out_repos_and_authors(store):
    items = export(store, 5)
    ids = [i.id for i in items]
    assert {i.repo for i in items} == {"a/one", "b/two"} and "h1" not in ids and "a-own" not in ids
    assert {"b1", "b2", "a-ai"} <= set(ids)  # every stratum of a small repo, and the AI-bot thread
    assert [i.repo for i in items] == sorted(i.repo for i in items)  # ordered by repo, not by stratum
    assert export(store, 5) == items  # deterministic


def test_items_carry_no_answers_and_no_code_only_where_the_code_is(store):
    (item, *_) = export(store, 1)
    record = vars(item)
    assert set(record) == FIELDS and item.comment.startswith("comment ")
    assert item.commit == ANCHOR and item.lines == (20, 21) and item.author in ("rev", "coderabbitai[bot]")
    assert item.url == "https://github.com/a/one/pull/1/files"  # the test ids aren't GitHub node ids
    text = repr(record).lower()
    assert not any(word in text for word in ("fixed", "ignored", "thumbs", "unaddressed", "outcome", "verdict"))
    assert "o1" not in [i.id for i in export(store, 50)]  # old-side comments are never sampled


def test_an_earlier_sample_is_rebuilt_redacted_and_blind(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite", BlobStore(tmp_path / "blobs"))
    body = "Reach me at jane.doe@corp.io\n\n✅ Resolved in " + "c" * 40
    s.upsert_pr(harvested("z/last", 9, [("z1", AuthorKind.AI, Outcome.FIXED)], body=body))
    s.upsert_pr(harvested("a/one", 1, [("a1", AuthorKind.HUMAN, Outcome.FIXED)]))
    items = audit_sample.rebuild(s, [("z/last", 9, "z1"), ("a/one", 1, "a1")], link=link)
    assert [i.id for i in items] == ["a1", "z1"]  # ordered by repo and PR, whatever the earlier order
    assert items[1].comment == "Reach me at jane.doe@corp.io"  # the bot's status line is gone
    records, hits = audit_sample.publishable(items)
    assert records[1]["comment"] == "Reach me at [redacted:email]" and records[1]["lines"] == [20, 21]
    assert [(where, hit.kind) for where, hit in hits] == [("z1.comment", "email")]
    with pytest.raises(audit_sample.MissingItems, match="a/one#1 nope"):
        audit_sample.rebuild(s, [("a/one", 1, "nope")], link=link)
    s.close()


def test_round_robin_takes_one_from_each_group_in_turn():
    assert round_robin([[1, 2, 3], [4], [], [5, 6]]) == [1, 4, 5, 2, 6, 3]
    assert round_robin([]) == []


def test_thread_permalinks_decode_the_comment_node_id():
    # verified against GitHub's `url` for this comment
    assert comment_database_id("PRRC_kwDOAAzd1s6jyU9g") == 2747879264
    assert (discussion_url("https://github.com/o/r/pull/1", "PRRC_kwDOAAzd1s6jyU9g")
            == "https://github.com/o/r/pull/1#discussion_r2747879264")  # fmt: skip
    assert comment_database_id("MDI0OlB1bGxSZXF1ZXN0UmV2aWV3Q29tbWVudDEyMzQ1") == 12345  # legacy id
    assert discussion_url("https://github.com/o/r/pull/1", "c-x") == "https://github.com/o/r/pull/1/files"


def test_the_command_rebuilds_an_earlier_sample_into_the_yardstick_without_overwriting_by_accident(tmp_path, capsys):
    build_store(tmp_path / "data", humans=("dev", "ann"), name=STORE).close()  # PRs 1 and 3: threads t1 and t3
    (tmp_path / "honed.toml").write_text((ROOT / "honed.toml").read_text())
    earlier = tmp_path / "earlier.json"
    earlier.write_text(json.dumps({"items": [{"id": "t3", "repo": "o/r", "pr": 3, "code": "11 | x"},
                                             {"id": "t1", "repo": "o/r", "pr": 1, "code": "11 | x"}]}))  # fmt: skip
    run = ["--config", str(tmp_path / "honed.toml"), "--data-dir", str(tmp_path / "data"), "export-audit-sample",
           "--ids-from", str(earlier)]  # fmt: skip
    assert cli.main(run) == 0 and "redaction: 0 hits" in capsys.readouterr().out
    out = tmp_path / "yardstick" / "human_labels" / "sample.json"
    items = json.loads(out.read_text())["items"]
    assert [i["id"] for i in items] == ["t1", "t3"] and all(set(i) == FIELDS for i in items)  # no code
    assert cli.main(run) == 2 and "--force replaces it" in capsys.readouterr().err
    assert cli.main([*run, "--force"]) == 0
