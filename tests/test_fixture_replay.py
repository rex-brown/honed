"""The fixture-PR replay CI runs (`scripts/replay_fixture.py`): the planted bug is reported from the cache alone, and
a review prompt that no longer matches the recording fails the check. No network, no model calls."""

from __future__ import annotations

import importlib.util
import shutil

from reviewkit import ROOT

_spec = importlib.util.spec_from_file_location("replay_fixture", ROOT / "scripts" / "replay_fixture.py")
assert _spec is not None and _spec.loader is not None
replay_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_fixture)


def test_the_planted_bug_is_reported_from_the_cache():
    passed, message = replay_fixture.replay()
    assert passed, message


def test_a_changed_review_prompt_fails_the_replay(tmp_path):
    fixture = tmp_path / "replay"
    shutil.copytree(replay_fixture.FIXTURE, fixture)
    intent = fixture / "policy" / "prompts" / "intent.md"
    intent.write_text(intent.read_text() + "\nOne more sentence.\n")
    passed, message = replay_fixture.replay(fixture)
    assert not passed and "ReplayMiss" in message and "--record" in message


def test_a_review_that_misses_the_bug_fails_the_replay(tmp_path):
    fixture = tmp_path / "replay"
    shutil.copytree(replay_fixture.FIXTURE, fixture)
    (fixture / "expected.json").write_text('{"path": "inventory/pages.py", "start_line": 4, "end_line": 6, '
                                           '"severity": "important", "category": "correctness"}')  # fmt: skip
    passed, message = replay_fixture.replay(fixture)
    assert not passed and "was not reported" in message
