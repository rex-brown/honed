"""Escaped-defect mining (ARCHITECTURE.md section 5): bugs the reviewers missed, found by later fixes (SZZ).

For each corpus repo, list PRs merged after the corpus window whose title or labels mark a bug fix. For each fix,
take the source-code lines its landing commit rewrote or removed (the fix must touch lines, not just the file;
hunks rewriting more than `max_fix_lines` old lines are refactors and skipped; pure insertions touch no old line;
lock files, data and docs are not source), and blame them
at the fix's parent. A blamed commit that belongs to a corpus PR (one of its commits, or a landing commit whose
message names the PR) made the lines the fix repaired: record an Important gold issue with provenance
`escaped_defect` at those lines, located at the PR's reviewed commit and described by the fix.

Known noise of the method: a fix can rewrite lines that were correct (a refactor bundled with the fix), and a line
the corpus PR only reformatted is blamed on it. The title/label filter and the touched-lines rule limit both;
nothing here is judged, so the gold issues carry the lower `escaped_defect` confidence.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from honed.core import patches, symbols, testfiles
from honed.core.evals import EscapedDefect
from honed.core.types import Corpus, DateWindow, GoldIssue, GoldProvenance, HarvestedPR, PRKey, Severity
from honed.ports.code_host import CodeHost, MergedPR
from honed.ports.code_reader import BlameLine, History, ReaderError
from honed.ports.store import LabelStore, Store

log = logging.getLogger(__name__)

_PR_NUMBER = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)\b", re.M)


@dataclass(frozen=True)
class DefectOptions:
    corpus_window: DateWindow
    after_days: int
    fix_title: str
    fix_labels: tuple[str, ...]
    max_fix_lines: int
    max_files: int
    conf: float  # `[metrics] gold_conf.escaped_defect`
    limit: int  # fix PRs listed per repo


@dataclass
class DefectReport:
    repo: str
    listed: int = 0
    fixes: int = 0  # listed PRs that look like bug fixes
    blamed_lines: int = 0
    defects: list[EscapedDefect] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def is_fix(pr: MergedPR, title: re.Pattern[str], labels: Sequence[str]) -> bool:
    wanted = {label.lower() for label in labels}
    return bool(title.search(pr.title)) or any(label.lower() in wanted for label in pr.labels)


def touched_old_lines(patch: str, max_lines: int) -> list[tuple[int, int]]:
    """Old-file line ranges a fix rewrote or removed, one per hunk, skipping hunks that rewrite more than
    `max_lines` lines (refactors) and pure insertions (no old line touched)."""
    out = []
    for hunk in patches.parse_hunks(patch):
        removed = sum(line.startswith("-") for line in hunk.lines)
        if removed == 0 or removed > max_lines:
            continue
        out.append((hunk.old_start, hunk.old_start + hunk.old_count - 1))
    return out


def _blocks(lines: Sequence[BlameLine]) -> list[list[BlameLine]]:
    """Runs of consecutive blamed lines from one commit."""
    out: list[list[BlameLine]] = []
    for b in sorted(lines, key=lambda b: (b.commit, b.line)):
        if out and out[-1][-1].commit == b.commit and b.line == out[-1][-1].line + 1:
            out[-1].append(b)
        else:
            out.append([b])
    return out


def locate(text: str, block: Sequence[str]) -> tuple[int, int] | None:
    """Where the lines of `block` sit, consecutively, in `text` (the first place), ignoring surrounding space."""
    lines = [line.strip() for line in text.splitlines()]
    wanted = [line.strip() for line in block]
    for start in range(len(lines) - len(wanted) + 1):
        if lines[start : start + len(wanted)] == wanted:
            return start + 1, start + len(wanted)
    return None


class DefectMiner:
    def __init__(self, store: Store, labels: LabelStore, host: CodeHost, history_for: Callable[[str], History],
                 options: DefectOptions) -> None:  # fmt: skip
        self._store = store
        self._labels = labels
        self._host = host
        self._history_for = history_for
        self._o = options

    def _window(self) -> DateWindow:
        end = dt.date.fromisoformat(self._o.corpus_window.end)
        return DateWindow((end + dt.timedelta(days=1)).isoformat(),
                          (end + dt.timedelta(days=self._o.after_days)).isoformat())  # fmt: skip

    def mine(self, repo: str) -> DefectReport:
        report = DefectReport(repo)
        corpus = [item for key in self._store.pr_keys(repo) if (item := self._store.get_pr(key)) is not None
                  and item.corpus in (Corpus.HUMAN, Corpus.APPROVAL_ONLY)]  # fmt: skip
        if not corpus:
            return report
        by_commit = {c.oid: item for item in corpus for c in item.pr.commits} | {i.pr.head_oid: i for i in corpus}
        by_number = {item.pr.number: item for item in corpus}
        history = self._history_for(repo)
        title = re.compile(self._o.fix_title)
        listed = self._host.list_merged_prs(repo, self._window(), limit=self._o.limit)
        report.listed = len(listed)
        self._labels.clear_escaped_defects(repo)  # a run replaces the repo's earlier results
        for fix in listed:
            if not is_fix(fix, title, self._o.fix_labels):
                continue
            report.fixes += 1
            try:
                self._fix(fix, repo, history, by_commit, by_number, report)
            except ReaderError as error:
                log.warning("%s#%d: %s", repo, fix.number, error)
                report.skip("git error")
        return report

    def _fix(self, fix: MergedPR, repo: str, history: History, by_commit: dict[str, HarvestedPR],
             by_number: dict[int, HarvestedPR], report: DefectReport) -> None:  # fmt: skip
        commit = self._host.merge_commit(repo, fix.number)
        if commit is None:
            report.skip("no merge commit")
            return
        history.deepen(commit, self._o.corpus_window.start)
        files = [f for f in history.commit_diff(commit) if f.status == "modified" and f.patch
                 and symbols.language_of(f.path) and not testfiles.is_test(f.path)]  # fmt: skip
        if not files or len(files) > self._o.max_files:
            report.skip("no source file rewritten" if not files else "too many files")
            return
        messages: dict[str, str] = {}
        for f in files:
            for start, end in touched_old_lines(f.patch or "", self._o.max_fix_lines):
                blamed = [b for b in history.blame(f.path, f"{commit}^", start, end) if not b.boundary]
                report.blamed_lines += len(blamed)
                for block in _blocks(blamed):
                    sha = block[0].commit
                    item = by_commit.get(sha)
                    if item is None:
                        message = messages.setdefault(sha, history.message(sha))
                        numbers = [int(a or b) for a, b in _PR_NUMBER.findall(message)]
                        item = next((by_number[n] for n in numbers if n in by_number), None)
                    if item is None:
                        continue
                    defect = self._defect(item, fix, commit, f.path, block, history)
                    if defect is None:
                        report.skip("lines not found at the reviewed commit")
                    elif all(d.issue.id != defect.issue.id for d in report.defects):
                        report.defects.append(defect)
                        self._labels.save_escaped_defect(defect)

    def _defect(self, item: HarvestedPR, fix: MergedPR, commit: str, path: str, block: Sequence[BlameLine],
                history: History) -> EscapedDefect | None:  # fmt: skip
        text = history.read(path, item.reviewed_commit)
        where = locate(text, [b.text for b in block]) if text is not None else None
        if where is None:
            return None
        issue = GoldIssue(
            id=f"{item.key}:fix{fix.number}:{path}:{where[0]}", path=path, start_line=where[0], end_line=where[1],
            severity=Severity.IMPORTANT, provenance=GoldProvenance.ESCAPED_DEFECT, conf=self._o.conf,
            description=f"A later fix, #{fix.number} \"{fix.title}\", rewrote these lines.", category="correctness",
        )  # fmt: skip
        return EscapedDefect(PRKey(item.pr.repo, item.pr.number), issue, fix.number, fix.title, commit, block[0].commit)
