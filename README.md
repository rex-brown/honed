# Honed

[![CI](https://github.com/rex-brown/honed/actions/workflows/ci.yml/badge.svg)](https://github.com/rex-brown/honed/actions/workflows/ci.yml)

A code reviewer that hones itself against real review outcomes.

Honed reviews a pull request (PR) diff with a panel of Claude models (intent, parallel finders, a verifier that keeps only what it can trace in the code), and learns from what happened to review comments on real pull requests: which ones people fixed, ignored or rejected. Its behavior lives in a versioned policy directory (`policy/`: prompts, settings and learned lessons), and an improve loop proposes one policy change at a time, measures it against a fixed yardstick (a judge, gold issues from human reviews, and the scoring in `METRICS.md`), and keeps it only if a statistical gate says it really helped. Everything runs locally, and the improve loop can run fully offline on a local model.

Status: research software. The pipeline, evaluation and improve loop work end to end; the first public baseline (`reports/baseline-v0.1.md`) and dataset bundle come with release v0.1.0 (`ROADMAP.md`, phase 5b).

## Install

Needs Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and git. Harvesting new pull requests also needs the GitHub command-line tool `gh`, logged in (`gh auth login`).

```sh
uv sync                  # the package and its development tools
uv sync --extra local    # also MLX-LM, for the local model (Apple Silicon only)
```

`uv sync` installs exactly what it is asked for, so a plain `uv sync` uninstalls MLX-LM again: on a machine that runs the local model, always sync with `uv sync --extra local`, which keeps the local-model packages.

All configuration lives in one file, `honed.toml`. Data goes to `data/` (ignored by git); `--data-dir PATH` runs any command on another data directory.

## Quickstart: pick a backend

Every model call goes through one backend, set by `[llm] backend` in `honed.toml`. Each one caches every answer, records tokens and cost per stage (`uv run honed usage --last`), and stops cleanly at its limit; running a command again resumes where it stopped.

### With an Anthropic API key (`anthropic`)

For scale, for Message Batches at half price, and for any use on behalf of other people.

```sh
export ANTHROPIC_API_KEY=sk-ant-api...    # an API key, never a subscription token: the backend refuses one
```

In `honed.toml`: `[llm] backend = "anthropic"`, a dollar ceiling for each run in `[llm.anthropic] run_budget_usd`, and, for labeling and evaluation, `use_batches = true`. A call or batch whose worst case doesn't fit in what is left of the budget is refused, and the run stops; `data/llm_status.json` shows `spent_usd` and `budget_usd` as it goes. Then:

```sh
uv run honed review path/to/change.diff --repo-name owner/name   # prints the review; posts nothing
```

### With a Claude subscription (`claude_code`, the default)

Install [Claude Code](https://code.claude.com) and log in. Honed calls it headless (`claude -p`), one isolated call at a time: an empty working directory, no tools, no MCP servers, no memory or CLAUDE.md, a replaced system prompt (`ARCHITECTURE.md`, section 8). The plan-usage guard stops before any call once a plan window is 80% used (85% for a weekly one), and at any sign of overage or usage credits. Never enable paid overage for this account while `honed` runs (`SECURITY.md`). Check `data/llm_status.json` during long runs; `--wait-for-reset` sleeps through a plan-window stop, for at most `[llm.claude_code] max_wait_s` (6 hours).

```sh
uv run honed review path/to/change.diff
```

### Fully local, on a Mac (`offline = true`)

```sh
uv sync --extra local
uv run honed fetch-local-model          # Qwen3.8-27B, 4-bit MLX: 16 GB of weights, into data/models/
```

Set `offline = true` in `honed.toml`: every stage, the judge's verdicts included, runs on the local model, and promotions are marked provisional until an online run re-scores them with the judge. Online-only commands (harvest, label, mine-defects) refuse to run.

## Reproduce the baseline from a bundle

The dataset is published as a bundle: one compressed JSON-lines file with the PR metadata, review threads, outcomes, the judge's labels, gold issues, the fixed splits, the decision log, policy versions and the judge's cached answers. It holds no code; `rehydrate` rebuilds the patches and context packs from git. It carries the train, validation and test-public splits; the maintainers keep the test-private half of the test split, scored on every promotion, out of every published bundle (`DATASET.md`, the private test split). `DATASET.md` says what is in it and under which terms (below).

```sh
uv run honed bundle import honed-dataset-v0.1.jsonl.gz       # idempotent; refuses a newer bundle schema
uv run honed rehydrate                                        # clones each repo once (bare, blobless)
uv run honed eval --split validation --rounds 2               # replays, judges and scores the incumbent policy
uv run honed eval --split validation --sensitivity --samples 3   # the noise floor and a weakened policy
```

Compare the report in `data/reports/` with `reports/baseline-v0.1.md`. On the `anthropic` backend with `use_batches = true` the replay goes out as message batches; `honed usage --last` shows what it cost.

## Run offline

Rehydrate (or `pack`) while online once; after that, `review`, `eval` and `improve` read only context packs and the call cache. With `offline = true` the local model answers every stage; with `[llm] backend = "replay"` nothing but cached answers is used (deterministic re-scoring, and the CI's fixture replay).

## The dataset and its licenses

Our annotations (outcomes, the judge's labels and reasons, gold issues, splits, the decision log, the judge's cached answers) are licensed under CC BY 4.0 (`DATA_LICENSE`). The review comments and PR text a bundle quotes are not ours to license: each keeps its author's login, its URL and a hash of its text. Before export, that text goes through a secrets and personal-data scan (keys and tokens, email addresses, phone numbers, public IP addresses are replaced by `[redacted:<kind>]`), and a removal list (`yardstick/removals.json`) takes out anything someone asked to have removed. `DATASET.md` has the details: what a bundle contains and omits, provenance, the privacy note, how to request a removal, the judge's version, and how to rebuild the code and, from a stripped bundle (`bundle export --strip-comments`), the comment text:

```sh
uv run honed bundle export honed-dataset.jsonl.gz                     # writes honed-dataset.report.json beside it
uv run honed rehydrate --comments                                     # after importing a stripped bundle: GitHub's API
uv run honed export-gold --format martian                             # our gold as Martian golden-comment files
```

## Held-out benchmarks

```sh
uv run honed import-benchmark martian --fetch   # Martian Code Review Bench: 50 PRs, 173 golden comments
uv run honed import-benchmark aacr --fetch      # AACR-Bench: 200 PRs, 1,506 confirmed comments
uv run honed rehydrate --repo gofr-dev/gofr     # context packs for one benchmark repo (all: no --repo)
uv run honed eval --split test                  # the whole test family: benchmarks and test-public (test-private too, for maintainers)
```

Benchmark PRs are test-split gold only. Their repos are on `[corpus.exclude]`: never harvested, labeled, mined or used to tune the policy.

## Improve the policy

```sh
uv run honed improve --rounds 3 --policy /tmp/policy-trial     # 3 improve rounds: propose, self-review, evaluate, gate, promote
uv run honed decisions                                          # the decision log
```

Each evaluation replays 2 review rounds per PR (`[eval] max_rounds`, the rounds the packs are built for; `eval --rounds 2`). The proposer mines the incumbent's failures on a sample of 150 train PR-rounds (`[improve] feed_sample_rounds`), not on all of train. The loop refuses to run until the latest sensitivity check shows the evaluation separates a weakened policy from the incumbent. `CONTRIBUTING.md` says how to propose a policy change.

## Calibrate the judge: an hour, no API key

Every score here rests on a judge model, trusted only while it agrees with people. Label 50 review comments, blind, on a local page, and send your `labels-<username>.json` as a pull request:

```sh
uv sync
uv run honed human-labels serve    # opens the labeling page; "Save labels" downloads your file
```

`CONTRIBUTING.md` ("Calibrate the judge") has the rules. Each comment needs two people, so every labeler counts.

## Repository layout

```
honed.toml           all configuration and feature flags
policy/              the reviewer's policy: prompts, config.toml, lessons.yaml (what the improve loop changes)
yardstick/           the fixed measure, maintainer-owned: the judge's prompts, the labeling sample and labels, the removal list
src/honed/
  core/              pure domain logic: types, outcomes, scoring, policy validation (no I/O)
  ports/             interfaces: LLM, Store, CodeHost, CodeReader, Judge, Reviewer, Bundle, ...
  review/            the review pipeline
  learn/             harvesting, labeling, evaluation, the improve loop, bundles, benchmarks
  adapters/          implementations: claude_code, anthropic, local and replay LLMs; GitHub; git; SQLite; ...
  cli/               the command line, and the only place adapters are wired into services
tests/               unit, contract and replay tests (no network, no model calls); fixtures/replay/ is CI's PR
tools/human-labels/  the local page for labeling the judge's blind sample (`honed human-labels serve`)
scripts/             the fixture replay, and the full-corpus run
research/            one-off studies (how the corpus repos were chosen)
```

## Related work

As far as a survey on 2026-10-01 found, no other project combines harvested review outcomes, an outcome judge, replay evaluation and gated automatic promotion. The nearest:

- **[Martian Code Review Bench](https://github.com/withmartian/code-review-benchmark)** (MIT): 50 PRs with 173 human-verified golden comments, an LLM judge that matches a tool's comments to them (precision, recall, category profiles), and an online set sampled from fresh PRs. It ranks tools; Honed uses it as held-out test gold and exports its own answer keys in Martian's format (`honed export-gold --format martian`), and adds gold harvested from what happened to real review comments, per-round replay, and an evaluation that gates each policy change rather than ranking finished tools.
- **[Pica](https://github.com/mrSamDev/pica)** (MIT): a self-learning review agent that turns its findings' dismissals and confirmations into rules online, with an offline replay harness. The closest in spirit; Honed also learns from human reviewers' own threads, checks with a judge that a "fixed" comment was really addressed (the addressed check), and keeps a rule only if a statistical gate on replayed PRs says it helped.
- **[PR-Agent](https://github.com/The-PR-Agent/pr-agent)** (MIT; started by Qodo, now community-maintained): an open-source reviewer (`/review`, `/improve`, `/describe`) configured through prompts and TOML files. Qodo's hosted product documents "best practices" learned from accepted suggestions; Honed's reviewer is comparable, and what it adds is the measured loop around it: harvested outcomes, per-round replay, gated promotion.
- **[Kodus](https://github.com/kodustech/kodus-ai)** (AGPL-3.0 community edition): a self-hostable reviewer with team rules written in plain language (Kody Rules) and a memory of the team's context. Honed adds evaluation of every rule change against held-out gold before it is kept, and governance that keeps the yardstick (judge, gold, metrics) out of the learner's reach.
- **[GEPA](https://github.com/gepa-ai/gepa)** (MIT; Agrawal et al., 2025): a general reflective prompt optimizer that reflects on execution traces to propose edits and keeps a Pareto front of candidates. Honed's proposer is reflective in the same way; Honed adds the domain's data and measure (harvested outcomes, the addressed check, per-round replay, a noise-floor gate) and the governance around promotion. Trying GEPA as an alternative proposer is on the roadmap.

## Documentation

- `ARCHITECTURE.md`: the design, the layers, every backend, offline mode and the dataset.
- `METRICS.md`: how a review is scored, and the gate a policy change must pass.
- `ROADMAP.md`: what is built, and the plan for the open-source release.
- `DATASET.md`: the published dataset: contents, provenance, licensing, privacy, removal, rehydration.
- `CONTRIBUTING.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`.

## Acknowledgements

- [Alex Owen](https://github.com/aowen14) provided inputs and inspiration through [review-kit](https://github.com/aowen14/review-kit), which shaped Honed's review-thread outcome taxonomy, lessons that must cite their evidence, reading review rules from the base branch, and the fixture-PR regression check (ideas only; no code or text copied).
- Lauren Tan (poteto) wrote [pstack](https://github.com/cursor/plugins/tree/main/pstack) (MIT), whose review rubric, lenses and lesson format are adapted, with attribution, into the seed policy and the proposer's prompts (`THIRD_PARTY_NOTICES.md`).
- Anthropic's [Code Review documentation](https://code.claude.com/docs/en/code-review) is the source of the finder, verifier and ranking pipeline, the severity scheme and `REVIEW.md`.
- [Martian Code Review Bench](https://github.com/withmartian/code-review-benchmark) (MIT) and [AACR-Bench](https://github.com/alibaba/aacr-bench) (Apache-2.0) are the held-out test data, and Martian's golden comments and judge matching shaped the evaluation.

`NOTICE` carries these credits for redistributors; `ARCHITECTURE.md` section 12 lists every design input.

## License

The code: Apache License 2.0 (`LICENSE`, with `NOTICE`). Parts of the seed policy adapt pstack (MIT); the benchmarks keep their own licenses (`THIRD_PARTY_NOTICES.md`). The dataset's annotations: CC BY 4.0 (`DATA_LICENSE`); the review comments it quotes remain their authors' (`DATASET.md`).
