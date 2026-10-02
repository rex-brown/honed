"""Bundle: the portable dataset (ARCHITECTURE.md section 8, `honed bundle export|import`).

A bundle is one compressed JSON-lines file: a manifest, then one record per line. It holds the PR metadata, review
threads (text, author login, URL), mechanical outcomes, judge labels, gold issues (round gold included), splits, the
decision log, policy versions, benchmark records, and the judge's call cache (so re-scoring is reproducible). It
holds no code: no context packs, no patches, no diff hunks. `honed rehydrate` rebuilds those from git.

Every review comment and PR description in it carries an `Attribution` (author login, URL, the SHA-256 of its text
as exported). Text went through the secrets and personal-data scan (`core.redaction`) before export, and the
removal list (`core.removals`) was applied. A stripped bundle (`bundle export --strip-comments`) leaves that text
out and keeps the hashes; `honed rehydrate --comments` refetches it and checks each hash.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from honed.core.benchmarks import BenchmarkPR
from honed.core.evals import Decision, EscapedDefect
from honed.core.improve import PolicyRecord
from honed.core.types import Compare, GoldSet, HarvestedPR, JudgedLabel, Judgment, PRKey
from honed.ports.call_store import CachedCall

BUNDLE_SCHEMA = 2  # the record format; a bundle with a newer one is refused. 2: attribution, stripped text, the
# release fields of the manifest (schema 1 bundles still import)


class BundleError(ValueError):
    """The file is not a bundle this code can read (unknown format, a newer schema, a broken record)."""


@dataclass(frozen=True)
class SourceRepo:
    repo: str
    license: str | None  # SPDX id from the code host; None when it wasn't looked up (offline)
    prs: int


@dataclass(frozen=True)
class SourceBenchmark:
    name: str
    license: str
    homepage: str
    prs: int


@dataclass(frozen=True)
class JudgeVersion:
    """The fixed judge whose labels and cached answers the bundle carries (from the yardstick)."""

    model: str
    effort: str
    prompts: Mapping[str, str] = field(default_factory=dict)  # yardstick prompt name -> SHA-256 of the file
    fingerprint: str = ""  # `<model>:<LLMJudge.fingerprint>`, as stored evaluation runs name their judge


@dataclass(frozen=True)
class LicenseSplit:
    """Which terms cover which part of the bundle (DATASET.md)."""

    annotations: str  # our outcomes, judgments, gold issues, splits, decision log, judge cache: an SPDX id
    comment_text: str  # the quoted review comments and PR text: not ours to license
    benchmarks: str  # the benchmarks' answer keys: their own licenses (`Manifest.benchmarks`)
    software: str  # honed itself, which the bundle doesn't contain


@dataclass(frozen=True)
class Manifest:
    bundle_schema: int
    store_schema: int  # of the store it was exported from
    created_at: str
    generator: str  # "honed <version>"
    judge_model: str  # the cached answers are this model's
    counts: Mapping[str, int] = field(default_factory=dict)  # records by kind
    repos: tuple[SourceRepo, ...] = ()
    benchmarks: tuple[SourceBenchmark, ...] = ()
    note: str = ""
    dataset_version: str = ""  # `[dataset] version`
    judge: JudgeVersion | None = None
    licenses: LicenseSplit | None = None
    comment_text: str = "full"  # "full", or "stripped" (`--strip-comments`: hashes only)
    redaction_rules: str = ""  # `core.redaction.RULES_VERSION`: a text hash is checked under these rules
    redactions: Mapping[str, int] = field(default_factory=dict)  # hits replaced, by kind
    removals: Mapping[str, int] = field(default_factory=dict)  # `core.removals.Applied.counts`
    test_private: str = ""  # "excluded" (the default: the maintainers' holdout left out) or "included"
    test_private_prs: int = 0  # test-private PRs left out, or included


# ---- records ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    """Quoted text in a bundle: who wrote it, where it lives, and the hash of its text as exported."""

    id: str  # the review comment's node id; "" for the PR description
    author: str | None  # the author's login (None: a deleted account)
    url: str  # the comment's permalink, or the PR's URL for its description
    text_sha256: str  # hex SHA-256 of the text after redaction: what the bundle holds, or held before stripping
    redacted: tuple[str, ...] = ()  # the kinds of the hits replaced in it
    stripped: bool = False  # the text is not in the bundle (`--strip-comments`)


@dataclass(frozen=True)
class PRRecord:
    item: HarvestedPR  # every patch and diff hunk removed
    patched: tuple[str, ...] = ()  # the files that had a patch: `reviewed:<path>` or `thread:<n>:<path>`
    comments: tuple[Attribution, ...] = ()  # one per review comment, in thread order
    description: Attribution | None = None  # the PR body's


@dataclass(frozen=True)
class JudgmentRecord:
    judgment: Judgment


@dataclass(frozen=True)
class JudgedLabelsRecord:
    pr: PRKey
    labels: tuple[JudgedLabel, ...]


@dataclass(frozen=True)
class GoldRecord:
    gold: GoldSet


@dataclass(frozen=True)
class RoundGoldRecord:
    gold: GoldSet  # `reviewed_commit` is the round's
    rounds: int


@dataclass(frozen=True)
class DefectRecord:
    defect: EscapedDefect


@dataclass(frozen=True)
class DecisionRecord:
    decision: Decision


@dataclass(frozen=True)
class PolicyVersionRecord:
    version: PolicyRecord


@dataclass(frozen=True)
class SplitRecord:
    pr: PRKey
    split: str  # train, validation or test: fixed on import


@dataclass(frozen=True)
class RoundDiffRecord:
    pr: PRKey
    diff: Compare  # a later review round's diff, without patches
    patched: tuple[str, ...] = ()  # its files that had a patch


@dataclass(frozen=True)
class BenchmarkRecord:
    pr: PRKey
    record: BenchmarkPR


@dataclass(frozen=True)
class CachedAnswerRecord:
    entry: CachedCall


Record = (
    PRRecord | JudgmentRecord | JudgedLabelsRecord | GoldRecord | RoundGoldRecord | DefectRecord | DecisionRecord
    | PolicyVersionRecord | SplitRecord | RoundDiffRecord | BenchmarkRecord | CachedAnswerRecord
)  # fmt: skip


class BundleWriter(Protocol):
    def write(self, record: Record) -> None: ...

    def close(self, manifest: Manifest) -> Manifest:
        """Write the file: the manifest (with the record counts filled in) first, then the records."""
        ...


class BundleReader(Protocol):
    def manifest(self) -> Manifest: ...

    def records(self) -> Iterator[Record]: ...
