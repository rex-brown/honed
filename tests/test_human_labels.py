"""People's labels on the blind audit sample: one `labels-<username>.json` per labeler, validated against the committed
sample; an item's human answer is the majority of at least two labelers ("unsure" left out); single-labeler items are
judged but flagged and never counted; inter-annotator agreement is Fleiss' kappa with three or more labelers, Cohen's
with two. Also the committed sample itself (no code, nothing about outcomes), the labeling page and its server."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import pytest

from evalkit import build_store
from honed import cli, config
from honed.adapters import human_labels
from honed.adapters.label_server import LabelServer, PageFiles
from honed.cli.human_labels import load_people
from honed.core import agreement, redaction
from honed.core.blinding import blind_comment
from honed.core.consensus import AnswerStatus, HumanLabel, HumanVerdict, answers, inter_annotator
from honed.learn.audit import HUMAN_MAJORITY, HUMAN_SINGLE, AuditItem, JudgeAudit, human_items, with_human
from honed.ports.judge import Validity
from reviewkit import ROOT, STORE

V, N, U = HumanVerdict.VALID, HumanVerdict.NOT_VALID, HumanVerdict.UNSURE
SAMPLE = {"items": [{"id": tid, "repo": "o/r", "pr": pr, "path": "app/service.py", "language": "python",
                     "comment": "items can be empty here", "author": "rev",
                     "url": f"https://github.com/o/r/pull/{pr}#discussion_r{pr}", "commit": "f" * 40,
                     "lines": [11, 11]} for tid, pr in (("t1", 1), ("t3", 3), ("t4", 4), ("t9", 9))]}  # fmt: skip


def labels_file(labeler: str, verdicts: dict[str, str], **extra) -> dict:
    return {"labeler": labeler, "labeled_at": "2026-10-02T10:00:00Z", "honed_version": "0.1.0",
            "items": [{"id": i, "verdict": v, "note": ""} for i, v in verdicts.items()], **extra}  # fmt: skip


def project(tmp_path, files: dict[str, dict]) -> config.Settings:
    """A copy of the config whose yardstick (the sample and the labels files) lives under `tmp_path`."""
    (tmp_path / "honed.toml").write_text((ROOT / "honed.toml").read_text())
    directory = tmp_path / "yardstick" / "human_labels"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sample.json").write_text(json.dumps(SAMPLE))
    for name, content in files.items():
        (directory / name).write_text(json.dumps(content))
    return config.load(tmp_path / "honed.toml")


# ---- combining verdicts ----------------------------------------------------------------------------------------


def test_an_answer_needs_a_majority_of_at_least_two_usable_verdicts():
    labels = [
        HumanLabel("a", "ann", V), HumanLabel("a", "bob", V), HumanLabel("a", "cat", N),  # 2-1: valid
        HumanLabel("b", "ann", N), HumanLabel("b", "bob", U),  # one usable verdict: single, flagged
        HumanLabel("c", "ann", V), HumanLabel("c", "bob", N),  # 1-1: a tie, no answer
        HumanLabel("d", "ann", U), HumanLabel("d", "bob", U),  # only unsure
        HumanLabel("e", "ann", N), HumanLabel("e", "bob", N), HumanLabel("e", "BOB", V),  # bob's later label wins
    ]  # fmt: skip
    got = {a.thread_id: (a.status, a.answer) for a in answers(labels)}
    assert got == {"a": (AnswerStatus.MAJORITY, True), "b": (AnswerStatus.SINGLE, False),
                   "c": (AnswerStatus.TIE, None), "d": (AnswerStatus.UNSURE, None),
                   "e": (AnswerStatus.TIE, None)}  # fmt: skip


def test_fleiss_kappa_matches_the_textbook_example_and_reduces_to_none_when_undefined():
    # Fleiss (1971) as worked on Wikipedia: 10 subjects, 14 raters each, 5 categories: kappa 0.210
    table = [[0, 0, 0, 0, 14], [0, 2, 6, 4, 2], [0, 0, 3, 5, 6], [0, 3, 9, 2, 0], [2, 2, 8, 1, 1],
             [7, 7, 0, 0, 0], [3, 2, 6, 3, 0], [2, 5, 3, 2, 2], [6, 5, 2, 1, 0], [0, 2, 2, 3, 7]]  # fmt: skip
    kappa = agreement.fleiss_kappa([dict(enumerate(row)) for row in table])
    assert kappa == pytest.approx(0.20993, abs=1e-4)
    assert agreement.fleiss_kappa([{"v": 1}, {}]) is None  # no item with two ratings
    assert agreement.fleiss_kappa([{"v": 3}, {"v": 2}]) is None  # one category only: chance agreement is 1
    assert agreement.fleiss_kappa([{"v": 2}, {"n": 2}]) == 1.0


def test_inter_annotator_is_cohen_with_two_labelers_and_fleiss_with_three_unsure_left_out():
    two = [HumanLabel("a", "ann", V), HumanLabel("a", "bob", V), HumanLabel("b", "ann", N), HumanLabel("b", "bob", N),
           HumanLabel("c", "ann", V), HumanLabel("c", "bob", U)]  # fmt: skip
    cohen = inter_annotator(two)
    assert (cohen.method, cohen.kappa, cohen.labelers, cohen.items) == ("cohen", 1.0, 2, 2)  # c: bob unsure
    three = [*two, HumanLabel("a", "cat", N), HumanLabel("b", "cat", N)]
    fleiss = inter_annotator(three)
    assert fleiss.method == "fleiss" and fleiss.labelers == 3 and fleiss.items == 2
    # a: 2 valid 1 not, b: 3 not -> P = (1/3 + 1) / 2, p = (2/6, 4/6), Pe = 5/9
    assert fleiss.kappa == pytest.approx((2 / 3 - 5 / 9) / (1 - 5 / 9))
    assert inter_annotator([HumanLabel("a", "ann", V)]).method == ""


# ---- labels files ----------------------------------------------------------------------------------------------


def test_labels_files_are_validated_against_the_sample(tmp_path):
    settings = project(tmp_path, {
        "labels-ann.json": labels_file("ann", {"t1": "valid", "t3": "not_valid"}),
        "labels-bob.json": labels_file("bob", {"t1": "maybe", "t1x": "valid"}),  # bad verdict, unknown id
        "labels-cat.json": labels_file("dan", {"t1": "valid"}),  # named for someone else
        "labels-eve.json": {"labeler": "eve", "items": [{"id": "t1", "verdict": "valid"}, {"id": "t1"}]},
    })  # fmt: skip
    directory = settings.paths.human_labels
    files, errors = human_labels.load_all(directory, human_labels.load_sample(directory / "sample.json").keys())
    assert [(f.labeler, [(lab.thread_id, lab.verdict) for lab in f.labels]) for f in files] == [
        ("ann", [("t1", V), ("t3", N)])]  # fmt: skip
    problems = {e.path.name: e.problems for e in errors}
    assert any("maybe" in p for p in problems["labels-bob.json"])
    assert any("'t1x' is not in the sample" in p for p in problems["labels-bob.json"])
    assert problems["labels-cat.json"] == ("the file must be named labels-dan.json",)
    assert {"`labeled_at` must be text", "item 2: 't1' appears twice"} <= set(problems["labels-eve.json"])


def test_one_labels_file_per_person(tmp_path, monkeypatch):
    for sub, name in (("a", "labels-ann.json"), ("b", "labels-ANN.json")):  # differ only in case
        (tmp_path / sub).mkdir()
        (tmp_path / sub / name).write_text(json.dumps(labels_file(name[7:-5], {"t1": "valid"})))
    monkeypatch.setattr(human_labels, "label_paths", lambda d: [tmp_path / "a" / "labels-ann.json",
                                                                tmp_path / "b" / "labels-ANN.json"])  # fmt: skip
    files, errors = human_labels.load_all(tmp_path, {"t1"})
    assert [f.labeler for f in files] == ["ann"] and "already ANN's file" in str(errors[0])


def test_the_check_command_passes_good_files_and_fails_bad_ones(tmp_path, capsys):
    good = {"labels-ann.json": labels_file("ann", {"t1": "valid", "t3": "unsure"})}
    project(tmp_path, good)
    run = ["--config", str(tmp_path / "honed.toml"), "human-labels", "check"]
    assert cli.main(run) == 0 and "labels-ann.json: ok, ann, 2 of 4 items" in capsys.readouterr().out
    bad = tmp_path / "labels-eve.json"
    bad.write_text(json.dumps(labels_file("eve", {"t1": "valid", "zz": "valid"})))
    assert cli.main([*run, str(bad)]) == 1 and "'zz' is not in the sample" in capsys.readouterr().out


# ---- the audit with several labelers ---------------------------------------------------------------------------


class AlwaysValid:
    """A judge that rules every comment valid."""

    model = "fake"

    def validity(self, evidence, *, sample: int = 0) -> Validity:
        return Validity(True, "looks right")


class NoCode:
    def read(self, path, commit):
        return None


def test_the_audit_counts_majorities_flags_single_labeler_items_and_reports_agreement(tmp_path):
    settings = project(tmp_path, {
        "labels-ann.json": labels_file("ann", {"t1": "valid", "t3": "not_valid", "t4": "unsure", "t9": "valid"}),
        "labels-bob.json": labels_file("bob", {"t1": "valid", "t3": "valid", "t4": "not_valid"}),
        "labels-cat.json": labels_file("cat", {"t1": "not_valid", "t3": "not_valid"}),
    })  # fmt: skip
    people = load_people(settings)
    assert not people.errors and sorted(f.labeler for f in people.files) == ["ann", "bob", "cat"]
    status = {a.thread_id: (a.status, a.answer) for a in people.answers}
    assert status == {"t1": (AnswerStatus.MAJORITY, True), "t3": (AnswerStatus.MAJORITY, False),
                      "t4": (AnswerStatus.SINGLE, False), "t9": (AnswerStatus.SINGLE, True)}  # fmt: skip
    # t1 2-1, t3 1-2 (t4 and t9 have one usable verdict each): P = 1/3, Pe = 1/2
    assert people.agreement.method == "fleiss" and people.agreement.items == 2
    assert people.agreement.kappa == pytest.approx(-1 / 3)
    assert "2 with a majority of at least 2 (counted), 2 single-labeler (flagged, not counted)" in people.summary()

    store = build_store(tmp_path / "data", humans=("dev", "ann", "bob"), name=STORE)  # PRs 1, 3, 4: threads t1, t3, t4
    try:
        human = human_items(people.answers, people.where, store.get_pr)  # t9's PR isn't in the store: left out
        assert [(a.id, a.truth, a.source, a.counted) for a in human] == [
            ("t1", True, HUMAN_MAJORITY, True), ("t3", False, HUMAN_MAJORITY, True),
            ("t4", False, HUMAN_SINGLE, False)]  # fmt: skip
        outcome = AuditItem(human[0].item, human[0].thread, False, "thumbs_down")
        assert [a.source for a in with_human([outcome], human)] == [HUMAN_MAJORITY, HUMAN_MAJORITY, HUMAN_SINGLE]
        audit = JudgeAudit(AlwaysValid(), lambda repo: NoCode(), context_lines=3, concurrency=1)
        report = audit.run(human, consistency_items=0, repeats=1)
    finally:
        store.close()
    assert report.n == 2 and report.accuracy == 0.5  # the single-labeler item is not counted
    assert dict(report.by_source) == {HUMAN_MAJORITY: 2, HUMAN_SINGLE: 1}
    assert report.accuracy_by_source == {HUMAN_MAJORITY: 0.5, HUMAN_SINGLE: 0.0}
    assert report.kappa_by_source[HUMAN_MAJORITY] == 0.0  # the judge said valid every time


# ---- what is committed -----------------------------------------------------------------------------------------

COMMITTED = ROOT / "yardstick" / "human_labels"
ITEM_FIELDS = {"id", "repo", "pr", "path", "language", "comment", "author", "url", "commit", "lines"}


def test_the_committed_sample_holds_no_code_and_nothing_about_outcomes():
    sample = json.loads((COMMITTED / "sample.json").read_text())
    assert set(sample) == {"generated_at", "note", "redaction_rules", "items"}
    items = sample["items"]
    assert len(items) == len({i["id"] for i in items}) == 50
    for item in items:
        assert set(item) == ITEM_FIELDS  # no code excerpt, no diff hunk
        assert item["commit"] is None or re.fullmatch(r"[0-9a-f]{40}", item["commit"])
        assert item["lines"] is None or 1 <= item["lines"][0] <= item["lines"][1]
        assert re.fullmatch(rf"https://github\.com/{re.escape(item['repo'])}/pull/{item['pr']}#discussion_r\d+",
                            item["url"])  # fmt: skip
        assert blind_comment(item["comment"]) == item["comment"]  # no bot status line saying what happened
        for value in item.values():
            assert not isinstance(value, str) or not redaction.redact(value).hits


def test_every_committed_labels_file_is_valid():
    refs = human_labels.load_sample(COMMITTED / "sample.json")
    _, errors = human_labels.load_all(COMMITTED, refs.keys())
    assert errors == [], "; ".join(str(e) for e in errors)  # `uv run honed human-labels check` says what is wrong


def test_bot_status_lines_are_cut_from_comments():
    text = "🔴 **Critical** `a.ts:3`\n\nThe cast throws.\n\n✅ Resolved in 7c9763c3000de653ed3eb867233d25d154fb35cf"
    assert blind_comment(text) == "🔴 **Critical** `a.ts:3`\n\nThe cast throws."
    rabbit = "Unsafe cast.\n\n<!-- fingerprinting:x -->\n\n✅ Confirmed as addressed by @someone\n\n<!-- end -->"
    assert blind_comment(rabbit) == "Unsafe cast."
    assert blind_comment("✅ Addressed in commits abc to def\nkeep") == "keep"
    plain = "  Looks fine, but `x` can be None.  \n"
    assert blind_comment(plain) == plain  # nothing to cut: unchanged


# ---- the labeling page and its server --------------------------------------------------------------------------

PAGE = ROOT / "tools" / "human-labels" / "index.html"


def test_the_page_shows_the_judges_definitions_and_never_the_thread_or_author():
    page = PAGE.read_text()
    prompt = (ROOT / "yardstick" / "prompts" / "validity.md").read_text()
    for label in ("valid", "not valid"):
        definition = next(line for line in prompt.splitlines() if line.startswith(f"- {label}: "))
        assert definition.removeprefix(f"- {label}: ") in page
    assert "Decide from the code, not from the reviewer's confidence." in page
    assert "it.author" not in page and "it.url" in page and page.count("it.url") == 1  # only for the diff hunk API
    assert "<script src" not in page and "<link" not in page  # self-contained


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def test_the_server_serves_the_page_the_sample_and_meta_and_nothing_else(tmp_path):
    (tmp_path / "secret.txt").write_text("not for the browser")
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(SAMPLE))
    server = LabelServer(PageFiles(PAGE, sample, {"honed_version": "9.9", "context_lines": 12}), 0)
    server.start()
    try:
        assert _get(server.url) == (200, PAGE.read_bytes())
        assert json.loads(_get(server.url + "sample.json")[1]) == SAMPLE
        assert json.loads(_get(server.url + "meta.json")[1]) == {"honed_version": "9.9", "context_lines": 12}
        for path in ("secret.txt", "../secret.txt", "honed.toml", "%2e%2e/secret.txt"):
            assert _get(server.url + path)[0] == 404
    finally:
        server.shutdown()
