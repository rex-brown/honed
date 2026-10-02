"""Which repos to harvest, for which corpus, and how many PRs each (ARCHITECTURE.md section 11)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from honed.core import sampling
from honed.core.filters import canonical_repo, is_excluded
from honed.core.types import Corpus


@dataclass(frozen=True)
class RepoPlan:
    repo: str
    corpus: Corpus
    quota: int  # PRs wanted in total, across runs
    language: str | None  # group key; None: decide from the repo's primary language


class ExcludedRepo(ValueError):
    """A held-out or otherwise excluded repo was asked for."""


class UnknownRepo(ValueError):
    """A repo that is not in the corpus was asked for."""


def corpus_plans(
    *,
    groups: Mapping[str, Sequence[str]],
    weights: Mapping[str, float],
    target: int,
    ai_repos: Sequence[str],
    ai_target: int,
) -> list[RepoPlan]:
    """Human-corpus quotas split by language weight, then evenly within each group; the AI-feedback target split
    evenly across its repos."""
    quotas = sampling.repo_quotas(groups, weights, target)
    language = {repo: key for key, repos in groups.items() for repo in repos}
    plans = [RepoPlan(repo, Corpus.HUMAN, quotas[repo], language[repo]) for repos in groups.values() for repo in repos]
    ai_quotas = sampling.spread(ai_target, len(ai_repos))
    plans += [RepoPlan(r, Corpus.AI_FEEDBACK, q, language.get(r)) for r, q in zip(ai_repos, ai_quotas, strict=True)]
    return plans


def select(plans: Sequence[RepoPlan], repos: Iterable[str] | None, excluded: Iterable[str]) -> list[RepoPlan]:
    """The plans for `repos` (all plans when None). An excluded repo is refused even if a plan names it."""
    excluded = tuple(excluded)
    wanted = None if repos is None else [canonical_repo(r) for r in repos]
    for repo in wanted or ():
        if is_excluded(repo, excluded):
            raise ExcludedRepo(f"{repo} is excluded from harvesting (held out or unsuitable)")
    known = {canonical_repo(p.repo) for p in plans}
    unknown = sorted(set(wanted or ()) - known)
    if unknown:
        raise UnknownRepo(f"not in the corpus (add them to [corpus] in honed.toml): {unknown}")
    chosen = [p for p in plans if wanted is None or canonical_repo(p.repo) in wanted]
    for plan in chosen:
        if is_excluded(plan.repo, excluded):
            raise ExcludedRepo(f"{plan.repo} is excluded from harvesting (held out or unsuitable)")
    return chosen
