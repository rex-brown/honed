"""Every outcome, the precedence between them, and how "lines changed" is decided."""

from builders import ANCHOR, comment, compare, content_compare, patch, thread
from honed.core import outcomes
from honed.core.types import AuthorKind, Compare, LineBasis, Outcome

TOUCHES_10 = patch("src/app.py", "@@ -10 +10 @@\n-old\n+new")
MISSES_10 = patch("src/app.py", "@@ -40 +40 @@\n-old\n+new")
NO_CHANGE = compare()  # a complete compare in which the file does not appear


def classify(t, c: Compare | None = NO_CHANGE, slack=0):
    changed, basis = outcomes.lines_changed(t, c, slack)
    return outcomes.outcome(t, changed), changed, basis


# ---- each outcome ------------------------------------------------------------------------------------------


def test_thumbs_down_from_someone_else():
    t = thread(comment("bot[bot]", typename="Bot", reactions=(("THUMBS_DOWN", "maintainer"),)))
    assert classify(t)[0] is Outcome.THUMBS_DOWN


def test_thumbs_down_by_the_finding_author_is_ignored():
    # A reviewer bot pre-attaches its own reactions.
    t = thread(comment("bot[bot]", typename="Bot", reactions=(("THUMBS_DOWN", "bot[bot]"),)))
    assert classify(t)[0] is Outcome.IGNORED


def test_fixed_when_a_later_commit_changes_the_flagged_lines():
    assert classify(thread(), compare(TOUCHES_10)) == (Outcome.FIXED, True, LineBasis.COMPARE)


def test_resolved_no_change():
    assert classify(thread(resolved=True), compare(MISSES_10)) == (
        Outcome.RESOLVED_NO_CHANGE,
        False,
        LineBasis.COMPARE,
    )


def test_open_at_merge_when_someone_replied():
    t = thread(comment("reviewer"), comment("author", at="2026-03-02T00:00:00Z"))
    assert classify(t)[0] is Outcome.OPEN_AT_MERGE


def test_ignored_when_nobody_replied():
    assert classify(thread())[0] is Outcome.IGNORED


def test_a_bot_reply_is_not_a_reply():
    t = thread(comment("reviewer"), comment("ci-bot", typename="Bot", at="2026-03-02T00:00:00Z"))
    assert classify(t)[0] is Outcome.IGNORED


def test_the_finding_authors_own_follow_up_is_not_a_reply():
    t = thread(comment("reviewer"), comment("reviewer", at="2026-03-02T00:00:00Z"))
    assert classify(t)[0] is Outcome.IGNORED


# ---- precedence: first match wins --------------------------------------------------------------------------


def test_thumbs_down_beats_fixed():
    t = thread(comment("ai[bot]", typename="Bot", reactions=(("THUMBS_DOWN", "dev"),)), resolved=True)
    assert classify(t, compare(TOUCHES_10))[0] is Outcome.THUMBS_DOWN


def test_fixed_beats_resolved_and_reply():
    t = thread(comment("reviewer"), comment("author", at="2026-03-02T00:00:00Z"), resolved=True)
    assert classify(t, compare(TOUCHES_10))[0] is Outcome.FIXED


def test_resolved_beats_reply():
    t = thread(comment("reviewer"), comment("author", at="2026-03-02T00:00:00Z"), resolved=True)
    assert classify(t)[0] is Outcome.RESOLVED_NO_CHANGE


# ---- how "lines changed" is decided ------------------------------------------------------------------------


def test_compare_overrides_is_outdated():
    # Force-push-heavy repos mark threads outdated although their lines did not change.
    assert classify(thread(outdated=True), compare(MISSES_10))[0] is Outcome.IGNORED


def test_no_compare_falls_back_to_is_outdated():
    assert classify(thread(outdated=True), None) == (Outcome.FIXED, True, LineBasis.IS_OUTDATED)
    assert classify(thread(outdated=False), None) == (Outcome.IGNORED, False, LineBasis.IS_OUTDATED)


def test_a_merge_base_compare_from_a_rewritten_anchor_is_not_trusted():
    diverged = compare(TOUCHES_10, status="diverged")
    assert classify(thread(outdated=False), diverged)[2] is LineBasis.IS_OUTDATED


def test_a_content_diff_is_trusted_for_rewritten_anchors():
    assert classify(thread(), content_compare(TOUCHES_10)) == (Outcome.FIXED, True, LineBasis.COMPARE)


def test_file_missing_from_a_capped_compare_is_unknown():
    assert classify(thread(outdated=True), compare(complete=False))[2] is LineBasis.IS_OUTDATED


def test_file_patch_omitted_by_the_host_is_unknown():
    omitted = patch("src/app.py", None)
    assert classify(thread(outdated=True), compare(omitted))[2] is LineBasis.IS_OUTDATED


def test_removed_file_counts_as_changed():
    removed = patch("src/app.py", None, status="removed")
    assert classify(thread(), compare(removed))[0] is Outcome.FIXED


def test_renamed_file_is_found_by_its_previous_path():
    renamed = patch("src/new.py", "@@ -10 +10 @@\n-old\n+new", status="renamed", previous="src/app.py")
    assert classify(thread(), compare(renamed))[0] is Outcome.FIXED


def test_old_side_comment_falls_back_to_is_outdated():
    assert classify(thread(side="LEFT", outdated=False), compare(TOUCHES_10))[2] is LineBasis.IS_OUTDATED


def test_file_level_thread_changes_with_any_edit_to_the_file():
    assert classify(thread(lines=None), compare(MISSES_10))[0] is Outcome.FIXED


def test_multi_line_range_and_slack():
    ranged = thread(lines=(8, 9))
    assert classify(ranged, compare(TOUCHES_10))[0] is Outcome.IGNORED
    assert classify(ranged, compare(TOUCHES_10), slack=1)[0] is Outcome.FIXED


# ---- labels ------------------------------------------------------------------------------------------------


def test_a_capped_compare_defers_to_a_content_diff_that_lists_the_file():
    capped = compare(patch("other.py", "@@ -1 +1 @@\n-a\n+b"), complete=False)
    assert outcomes.compare_for(thread(), [capped, content_compare(TOUCHES_10)]).source.value == "content_diff"
    assert classify(thread(outdated=False), capped)[2] is LineBasis.IS_OUTDATED


def test_no_anchor_commit_means_no_compare():
    orphan = thread(comment(commit=None))  # GitHub returns originalCommit null once it loses the commit
    assert outcomes.compare_for(orphan, [compare(TOUCHES_10)]) is None


def test_label_picks_the_compare_from_the_thread_anchor():
    other = compare(TOUCHES_10, base="b" * 40)
    mine = compare(MISSES_10, base=ANCHOR)
    label = outcomes.label(thread(), AuthorKind.HUMAN, [other, mine], slack=0)
    assert (label.outcome, label.lines_changed, label.author_kind) == (Outcome.IGNORED, False, AuthorKind.HUMAN)
