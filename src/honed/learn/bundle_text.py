"""What happens to quoted text on its way into a bundle (ARCHITECTURE.md section 8, DATASET.md).

Every string a person wrote, and every annotation that may quote one, goes through the secrets and personal-data
scan (`core.redaction`): PR titles, descriptions and commit headlines, review comments, judgment reasons, gold and
escaped-defect descriptions, and the judge's cached answers. A hit is replaced by `[redacted:<kind>]`, counted, and
listed in the export report by location (record, field, offset), never by value. A benchmark's answer key (its
benchmark record, and the gold issues made from its golden comments) keeps its text as the benchmark published it;
the benchmark PR's own GitHub text (title, description, review comments) is scanned like any other.

Each review comment and PR description then gets its attribution (author login, URL, SHA-256 of the text as
exported). With `strip` (`bundle export --strip-comments`) their text is left out and the hashes stay, so
`honed rehydrate --comments` can refetch it and check it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from honed.core import redaction
from honed.core.evals import EscapedDefect
from honed.core.types import GoldProvenance, GoldSet, HarvestedPR, Judgment, PullRequest
from honed.ports.bundle import Attribution
from honed.ports.call_store import CachedCall


def pr_url(pr: PullRequest) -> str:
    """The PR's GitHub URL (as harvested, else built from its repo and number)."""
    return pr.url or f"https://github.com/{pr.repo}/pull/{pr.number}"


@dataclass(frozen=True)
class RedactionHit:
    location: str  # the record and field: `o/r#1 comment PRRC_x`, `cached answer 3f2a... data.issues[0].reason`
    kind: str
    offset: int  # characters into the original text


@dataclass
class ExportReport:
    """What the export changed, by location only: `<bundle>.report.json`."""

    redactions: list[RedactionHit] = field(default_factory=list)
    removed_prs: list[str] = field(default_factory=list)
    removed_comments: list[str] = field(default_factory=list)  # listed comment ids found in the store
    removed_threads: list[str] = field(default_factory=list)
    stripped_texts: int = 0
    private_prs: int = 0  # test-private PRs left out (counted, never listed: the holdout stays unnamed)
    private_answers: int = 0  # judge answers left out with them

    @property
    def redaction_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(h.kind for h in self.redactions).items()))


@dataclass(frozen=True)
class TextOptions:
    strip: bool = False  # leave review comments' and PR descriptions' text out, keep the hashes
    link: Callable[[str, str], str] = lambda url, comment_id: url  # (PR URL, comment id) -> the comment's URL


class TextPolicy:
    """Redacts, attributes and (optionally) strips quoted text, recording every hit in `report`."""

    def __init__(self, options: TextOptions, report: ExportReport) -> None:
        self._o = options
        self.report = report

    def _redact(self, text: str, location: str) -> redaction.Redacted:
        done = redaction.redact(text)
        self.report.redactions += [RedactionHit(location, h.kind, h.start) for h in done.hits]
        return done

    def scrub(self, text: str, location: str) -> str:
        """`text` redacted, its hits recorded at `location`."""
        return self._redact(text, location).text

    # ---- quoted text -------------------------------------------------------------------------------------------

    def quote_pr(self, item: HarvestedPR, pending: Mapping[str | None, str] | None = None,
                 ) -> tuple[HarvestedPR, tuple[Attribution, ...], Attribution]:  # fmt: skip
        """The PR with its text redacted (and stripped, with `strip`), and the attribution of every comment and of
        the description. `pending` holds the text a stripped bundle left out of this PR and nobody has refetched
        yet (comment id, or None for the description -> its hash): it stays stripped, under its hash."""
        pending = pending or {}
        pr, key, url = item.pr, str(item.key), pr_url(item.pr)
        title = self.scrub(pr.title, f"{key} title")
        author = pr.author.login if pr.author else None
        body, description = self._quote(pr.body, f"{key} body", "", author, url, pending.get(None))
        commits = tuple(replace(c, message_headline=self.scrub(c.message_headline, f"{key} commit {c.oid[:12]}"))
                        for c in pr.commits)  # fmt: skip
        threads, attributions = [], []
        for thread in pr.threads:
            comments = []
            for c in thread.comments:
                login = c.author.login if c.author else None
                text, attribution = self._quote(c.body, f"{key} comment {c.id}", c.id, login, self._o.link(url, c.id),
                                                pending.get(c.id))  # fmt: skip
                comments.append(replace(c, body=text))
                attributions.append(attribution)
            threads.append(replace(thread, comments=tuple(comments)))
        bare = replace(pr, title=title, body=body, commits=commits, threads=tuple(threads))
        return replace(item, pr=bare), tuple(attributions), description

    def _quote(self, text: str, where: str, id_: str, author: str | None, url: str,
               pending: str | None) -> tuple[str, Attribution]:  # fmt: skip
        """The text as exported, and its attribution."""
        if pending is not None:  # still stripped from an earlier bundle: its hash is all there is
            self.report.stripped_texts += 1
            return "", Attribution(id_, author, url, pending, (), True)
        done = self._redact(text, where)
        kinds = tuple(sorted({h.kind for h in done.hits}))
        self.report.stripped_texts += self._o.strip
        attribution = Attribution(id_, author, url, redaction.sha256(done.text), kinds, self._o.strip)
        return ("" if self._o.strip else done.text), attribution

    # ---- annotations -------------------------------------------------------------------------------------------

    def judgment(self, judgment: Judgment) -> Judgment:
        where = f"{judgment.repo}#{judgment.number} judgment {judgment.thread_id} {judgment.kind.value} reason"
        return replace(judgment, reason=self.scrub(judgment.reason, where))

    def gold(self, gold: GoldSet, what: str = "gold") -> GoldSet:
        """Our gold descriptions redacted; a benchmark's golden comments are its answer key, under its license, and
        stay as published (its benchmark record carries the same text)."""
        issues = tuple(g if g.provenance is GoldProvenance.BENCHMARK else
                       replace(g, description=self.scrub(g.description, f"{gold.key} {what} {g.id}"))
                       for g in gold.issues)  # fmt: skip
        return replace(gold, issues=issues)

    def defect(self, defect: EscapedDefect) -> EscapedDefect:
        where = f"{defect.pr} escaped defect {defect.issue.id}"
        issue = replace(defect.issue, description=self.scrub(defect.issue.description, where))
        return replace(defect, issue=issue, fix_title=self.scrub(defect.fix_title, f"{where} fix title"))

    def cached(self, entry: CachedCall) -> CachedCall:
        where = f"cached answer {entry.key[:16]}"
        text = self.scrub(entry.text, f"{where} text")
        data, hits = redaction.redact_value(entry.data)
        self.report.redactions += [RedactionHit(f"{where} data {path}", h.kind, h.start) for path, h in hits]
        return replace(entry, text=text, data=data)
