"""BenchmarkSource: a public code-review benchmark's PRs and golden comments, read from its downloaded files
(`adapters/bench_martian.py`, `adapters/bench_aacr.py`; ARCHITECTURE.md sections 6 and 11)."""

from __future__ import annotations

from typing import Protocol

from honed.core.benchmarks import BenchmarkPR


class BenchmarkError(ValueError):
    """The benchmark's files are missing or not in the expected shape."""


class BenchmarkSource(Protocol):
    @property
    def name(self) -> str:
        """`martian` or `aacr` (`core.benchmarks`)."""
        ...

    @property
    def license(self) -> str:
        """The benchmark's own license (SPDX id), recorded in THIRD_PARTY_NOTICES.md and bundle manifests."""
        ...

    @property
    def homepage(self) -> str: ...

    def prs(self) -> list[BenchmarkPR]:
        """Every PR of the benchmark, with its golden (and, where the benchmark has them, rejected) comments."""
        ...
