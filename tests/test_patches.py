from honed.core import patches

REPLACE_LINE_10 = "@@ -10 +10 @@ def fit(self):\n-    return x\n+    return y"
INSERT_AFTER_12 = "@@ -12,0 +13,2 @@\n+    check(x)\n+    log(x)"
DELETE_20_21 = "@@ -20,2 +19,0 @@\n-a\n-b"
WITH_CONTEXT = "@@ -5,5 +5,6 @@ class A:\n ctx5\n ctx6\n-old7\n+new7\n+extra\n ctx8\n ctx9"


def test_parse_hunks_reads_counts_and_section_header():
    (hunk,) = patches.parse_hunks(REPLACE_LINE_10)
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (10, 1, 10, 1)
    assert hunk.header == "def fit(self):"


def test_old_side_changes_tracks_deletions_and_insertion_points():
    changes = patches.old_side_changes(WITH_CONTEXT)
    assert changes.deleted == {7}
    assert changes.inserted_after == set()  # "+new7 +extra" replace line 7; nothing is purely inserted


def test_a_replacement_does_not_touch_the_next_line():
    assert not patches.touches(REPLACE_LINE_10, 11, 11)


def test_zero_count_hunk_starts_after_the_named_line():
    assert patches.old_side_changes(INSERT_AFTER_12).inserted_after == {12}
    assert patches.old_side_changes(DELETE_20_21).deleted == {20, 21}


def test_touches_replaced_line():
    assert patches.touches(REPLACE_LINE_10, 10, 10)
    assert not patches.touches(REPLACE_LINE_10, 9, 9)


def test_touches_insertion_directly_after_or_before_the_range():
    assert patches.touches(INSERT_AFTER_12, 12, 12)  # inserted right after the flagged line
    assert patches.touches(INSERT_AFTER_12, 13, 14)  # inserted right before the flagged range
    assert not patches.touches(INSERT_AFTER_12, 15, 16)


def test_slack_widens_the_range():
    assert not patches.touches(REPLACE_LINE_10, 12, 12)
    assert patches.touches(REPLACE_LINE_10, 12, 12, slack=2)


def test_unified_diff_round_trips_through_the_parser():
    old = "a\nb\nc\nd\n"
    new = "a\nB\nc\nd\ne\n"
    diff = patches.unified_diff(old, new, "f.txt")
    changes = patches.old_side_changes(diff)
    assert changes.deleted == {2}
    assert 4 in changes.inserted_after
    assert patches.added_lines(diff) == ["B", "e"]
    assert patches.removed_lines(diff) == ["b"]


def test_unified_diff_keeps_removed_lines_that_look_like_headers():
    diff = patches.unified_diff("a\n---\nb\n", "a\nb\n", "x.md", context=1)
    assert patches.removed_lines(diff) == ["---"] and not diff.startswith("---")
    assert patches.unified_diff("same\n", "same\n", "x.md") == ""
