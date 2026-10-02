"""The promote step (ARCHITECTURE.md section 7, step 4): the only writer of `policy/`.

- A candidate whose diff touches any path the promote settings don't permit is refused: only `[promote]
  allowed_paths`, never `forbidden_paths` (the judge's prompts, `src/`, the metric settings), whatever it changes.
- On acceptance the new version is recorded in the store (hash, parent, diff, rationale, the gate's numbers, the
  provisional flag, and every file, so it can be restored), written to the policy directory, logged in
  `policy/CHANGELOG.md` (append-only), and its evaluation becomes the incumbent run for its split.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from honed.core.evals import EvalRun
from honed.core.improve import CandidateRecord, GateVerdict, PolicyRecord, changed_paths
from honed.ports.policy import PolicyDirectory
from honed.ports.store import EvalStore, ImproveStore


class PromotionRefused(RuntimeError):
    """The candidate touches paths the promote step may not write."""


def refused_paths(diff: str, permits: Callable[[str], bool]) -> list[str]:
    """The paths a candidate diff touches that the promote step may not write (empty: all allowed)."""
    paths = changed_paths(diff)
    if not paths and diff.strip():
        return ["(a diff whose files can't be read)"]
    return [p for p in paths if not permits(p)]


@dataclass(frozen=True)
class IncumbentKey:
    split: str
    backend: str
    rounds: int


class Promoter:
    def __init__(self, store: ImproveStore, evals: EvalStore, directory: PolicyDirectory,
                 permits: Callable[[str], bool]) -> None:  # fmt: skip
        self._store = store
        self._evals = evals
        self._directory = directory
        self._permits = permits

    def promote(self, record: CandidateRecord, old: Mapping[str, str], new: Mapping[str, str], run: EvalRun,
                key: IncumbentKey, verdict: GateVerdict, *, provisional: bool) -> PolicyRecord:  # fmt: skip
        refused = refused_paths(record.diff, self._permits)
        if refused:
            raise PromotionRefused(f"candidate {record.id} touches paths the promote step may not write: {refused}")
        if dict(self._directory.files()) != dict(old):
            raise PromotionRefused("the policy directory changed since the candidate was proposed")
        now = dt.datetime.now(dt.UTC).isoformat()
        proposal = record.proposal
        deltas = {r.rule: dict(r.values) for r in verdict.rules}
        version = PolicyRecord(
            hash=record.policy_hash, parent=record.parent_hash, created_at=now,
            rationale=f"{proposal.hypothesis}\n\nChange: {proposal.change}", diff=record.diff, deltas=deltas,
            provisional=provisional, files=dict(new), candidate=record.id,
        )  # fmt: skip
        self._store.save_policy_version(version)
        self._directory.replace(old, new)
        gain = verdict.rules[0].values if verdict.rules else {}
        self._directory.append_changelog("\n".join([
            f"## {now[:19]}Z: {record.policy_hash[:12]} (parent {record.parent_hash[:12]})"
            + (" PROVISIONAL" if provisional else ""),
            "",
            f"- Candidate {record.id} ({proposal.generator.value}): {proposal.change}",
            f"- Hypothesis: {proposal.hypothesis}",
            f"- Evidence: {'; '.join(proposal.evidence) or 'none'}",
            f"- Gate: delta-S {gain.get('delta_S')} [{gain.get('ci_low')}, {gain.get('ci_high')}], min_gain "
            f"{verdict.min_gain}; eval run {run.id} on {key.split}",
            f"- Numbers: `{json.dumps(deltas, default=str)}`",
        ]))  # fmt: skip
        self._evals.set_incumbent(key.split, key.backend, key.rounds, run.id)
        return version
