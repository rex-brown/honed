"""Outcome traces inside a review comment's own text, removed where a reader must judge the comment blind (the human
labeling sample, `learn/audit_sample.py`). Pure functions.

AI review bots edit their comments after the fact to report what happened: "✅ Resolved in <commit>" (Macroscope),
"✅ Addressed in commits <a> to <b>" and "✅ Confirmed as addressed by @someone" (CodeRabbit). Such a line answers the
question the labeler is asked. HTML comments (bot fingerprints and markers, which GitHub doesn't display) go too.
"""

from __future__ import annotations

import re

_STATUS = re.compile(
    r"(?im)^[ \t>]*(?:✅|☑️|✔️)[ \t]*(?:\*\*)?(?:resolved|addressed|confirmed as addressed|fixed)\b[^\n]*(?:\n|$)"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLANK_RUNS = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def blind_comment(text: str) -> str:
    """`text` without bot status lines that report the comment's outcome, and without HTML comments; unchanged when it
    has neither."""
    out = _HTML_COMMENT.sub("", _STATUS.sub("", text))
    return text if out == text else _BLANK_RUNS.sub("\n\n", out).strip()
