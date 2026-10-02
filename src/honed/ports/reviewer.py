"""Reviewer: the review pipeline as a service (`review/pipeline.py`), for the evaluation, which can't import it."""

from __future__ import annotations

from typing import Protocol

from honed.core.reviews import ReviewRequest, ReviewResult
from honed.ports.code_reader import CodeReader


class Reviewer(Protocol):
    @property
    def policy_hash(self) -> str: ...

    def review(self, request: ReviewRequest, reader: CodeReader, *, sample: int = 0) -> ReviewResult:
        """Review the change in `request`, reading code only through `reader` (as of the request's commits).
        `sample` > 0 asks every model call for an independent sample (the noise floor)."""
        ...
