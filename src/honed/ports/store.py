"""Store: the harvested dataset, plus the content-addressed blob store for large text."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from honed.core.benchmarks import BenchmarkPR
from honed.core.evals import Decision, EscapedDefect, EvalRun
from honed.core.improve import CandidateRecord, PolicyRecord
from honed.core.types import (
    Compare,
    ContextPack,
    Corpus,
    Cursor,
    DateWindow,
    GoldSet,
    HarvestedPR,
    JudgedLabel,
    Judgment,
    PRFact,
    PriorThread,
    PRKey,
    SampledAs,
    ThreadFact,
)


class Store(Protocol):
    # Harvested PRs. Upserting the same PR again replaces it in place.
    def upsert_pr(self, item: HarvestedPR) -> None: ...

    def get_pr(self, key: PRKey) -> HarvestedPR | None: ...

    def has_pr(self, key: PRKey) -> bool: ...

    def pr_keys(self, repo: str | None = None) -> list[PRKey]: ...

    def count_prs(self, repo: str, corpus: Corpus, created: DateWindow | None = None,
                  sampled_as: SampledAs | None = None) -> int: ...  # fmt: skip

    def set_corpus(self, key: PRKey, corpus: Corpus) -> None:
        """Move a stored PR to another corpus (the approval-only split), without refetching it."""
        ...

    def set_split(self, key: PRKey, split: str | None) -> None:
        """Fix a stored PR's split (a bundle's assignment); None returns it to the time-ordered assignment."""
        ...

    def set_splits(self, splits: Mapping[PRKey, str]) -> None:
        """Fix many PRs' splits in one transaction (`honed splits --assign`): all of them, or none."""
        ...

    # Resumable listing positions, per repo, corpus, window slice and sample (general or bug-targeted).
    def get_cursor(self, repo: str, corpus: Corpus, window: DateWindow,
                   sample: SampledAs = SampledAs.GENERAL) -> Cursor: ...  # fmt: skip

    def set_cursor(self, repo: str, corpus: Corpus, window: DateWindow, cursor: Cursor,
                   sample: SampledAs = SampledAs.GENERAL) -> None: ...  # fmt: skip

    # Flat rows for reporting.
    def pr_facts(self) -> list[PRFact]: ...

    def thread_facts(self) -> list[ThreadFact]: ...

    def threads_before(self, repo: str, paths: Sequence[str], before: str) -> list[PriorThread]:
        """Labeled threads on `paths` in `repo` opened strictly before `before` (an ISO timestamp), oldest first."""
        ...

    # Content-addressed blobs: `put_blob` returns the digest that `get_blob` takes.
    def put_blob(self, data: bytes) -> str: ...

    def get_blob(self, digest: str) -> bytes: ...

    # Context packs: the manifest is stored here, file contents as blobs.
    def save_pack(self, pack: ContextPack) -> None: ...

    def get_pack(self, key: PRKey) -> ContextPack | None: ...

    # Packs at later review rounds' commits (ARCHITECTURE.md section 6).
    def save_round_pack(self, pack: ContextPack) -> None: ...

    def get_round_pack(self, key: PRKey, commit: str) -> ContextPack | None:
        """The pack at `commit`: the PR's own pack when that is its head, else a round pack."""
        ...

    # Later review rounds' diffs (merge base -> the round's commit), with or without their round packs: a bundle
    # carries them without code, and `rehydrate` rebuilds the round packs from them.
    def save_round_diff(self, key: PRKey, diff: Compare) -> None: ...

    def round_diffs(self, key: PRKey) -> list[Compare]: ...

    # Patches a bundle import left out (it carries no code): `reviewed:<path>`, `thread:<n>:<path>` and
    # `round:<commit>:<path>` for the files that had one. `rehydrate` restores them from git, then clears the mark;
    # until then the PR is not reviewed.
    def mark_stripped(self, key: PRKey, files: Sequence[str]) -> None: ...

    def stripped(self, key: PRKey) -> list[str]: ...

    def stripped_keys(self) -> list[PRKey]: ...

    # Imported benchmark PRs: the benchmark's own record (golden and rejected comments), beside the stored PR.
    def save_benchmark_pr(self, record: BenchmarkPR, key: PRKey) -> None: ...

    def benchmark_prs(self) -> list[tuple[PRKey, BenchmarkPR]]: ...


class LabelStore(Protocol):
    """Labels derived from the harvested data (ARCHITECTURE.md section 5), kept apart from it so it can be relabeled."""

    # One verdict per (thread, kind); saving again replaces it.
    def save_judgment(self, judgment: Judgment) -> None: ...

    def judgments(self, key: PRKey) -> list[Judgment]: ...

    # The judged labels of a PR's threads, replaced as a whole.
    def save_judged_labels(self, key: PRKey, labels: Sequence[JudgedLabel]) -> None: ...

    def judged_labels(self, key: PRKey) -> list[JudgedLabel]: ...

    # A PR's gold set, replaced as a whole; None until built.
    def save_gold(self, gold: GoldSet) -> None: ...

    def get_gold(self, key: PRKey) -> GoldSet | None: ...

    # Gold issues per replayed review round, for a given number of replayed rounds; `reviewed_commit` is the round's.
    def save_round_gold(self, gold: GoldSet, rounds: int) -> None: ...

    def get_round_gold(self, key: PRKey, commit: str, rounds: int) -> GoldSet | None: ...

    def round_golds(self, key: PRKey) -> list[tuple[GoldSet, int]]:
        """Every round gold set of the PR, with the number of replayed rounds it was built for."""
        ...

    # Escaped defects (ARCHITECTURE.md section 5), replaced by (PR, issue id).
    def save_escaped_defect(self, defect: EscapedDefect) -> None: ...

    def escaped_defects(self, key: PRKey | None = None) -> list[EscapedDefect]: ...

    def clear_escaped_defects(self, repo: str) -> None: ...


class EvalStore(Protocol):
    """Evaluation runs keyed by policy hash, split, backend, round count and sample; the incumbent run per split,
    backend and round count; and the decision log."""

    def save_eval_run(self, run: EvalRun) -> None: ...

    def get_eval_run(self, run_id: str) -> EvalRun | None: ...

    def latest_eval_run(self, policy_hash: str, split: str, backend: str, rounds: int, sample: int) -> EvalRun | None:
        """The latest complete run with this key."""
        ...

    def eval_run_ids(self, split: str | None = None, backend: str | None = None) -> list[str]:
        """Ids of the complete runs, newest first, optionally of one split and backend."""
        ...

    def set_incumbent(self, split: str, backend: str, rounds: int, run_id: str) -> None: ...

    def incumbent(self, split: str, backend: str, rounds: int) -> str | None: ...

    def add_decision(self, decision: Decision) -> int: ...

    def decisions(self) -> list[Decision]: ...


class ImproveStore(Protocol):
    """The improve loop's records (ARCHITECTURE.md section 7): every candidate, kept or not, and every promoted
    policy version with its whole content, so any version can be restored."""

    def save_candidate(self, record: CandidateRecord) -> None: ...

    def candidates(self) -> list[CandidateRecord]: ...

    def save_policy_version(self, record: PolicyRecord) -> None: ...

    def policy_version(self, content_hash: str) -> PolicyRecord | None: ...

    def policy_versions(self) -> list[PolicyRecord]: ...
