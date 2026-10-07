# Contributing

Three kinds of contribution are welcome: **human labels** that calibrate the judge (about an hour, no setup beyond `uv sync`), changes to the reviewer's **policy** (`policy/`), which must come with evidence, and changes to the **code** (`src/`, `tests/`, `scripts/`, `tools/`), which must keep the checks green. Read `ARCHITECTURE.md` first; `METRICS.md` defines what "better" means. Anything that touches data (harvesting, labels, splits, gold, scoring, exports), and any contribution of data or labels, follows `DATA_QUALITY.md`: its rules and checklists apply to every pull request.

**Response times.** The maintainer is away for now, so responses to issues and pull requests may be slow. Pull requests and label files are welcome in the meantime and will be reviewed when the maintainer is back.

## Calibrate the judge: label 50 review comments

Every score in this project rests on a judge model's verdicts, and the judge is trusted only while it agrees with people (`METRICS.md`, section 5). The judge audit needs blind human verdicts on a fixed sample of 50 review comments from real pull requests (`yardstick/human_labels/sample.json`), each from at least two people. It takes about an hour.

```sh
uv sync
uv run honed human-labels serve        # opens the labeling page on http://127.0.0.1:8765/
```

The page shows one review comment at a time and the code it was made on, fetched from GitHub at the commit the comment was made on, with the flagged lines highlighted. For each comment, decide whether it points at a real problem worth changing the code for: **valid** (key `1`), **not valid** (`2`) or **unsure** (`3`); "What to decide" on the page gives the exact definitions the judge uses (`yardstick/prompts/validity.md`). A note is optional. Answers save in your browser as you go, so you can stop and come back.

When you are done, enter your GitHub username and press **Save labels**: it downloads `labels-<username>.json`. Then:

```sh
mv ~/Downloads/labels-<username>.json yardstick/human_labels/
uv run honed human-labels check        # the file matches the sample
```

and open a pull request that adds only that file. The rules:

- **Label blind.** Judge from the comment and the code alone. Don't open the pull request, look up the review thread, or search the dataset or the judge's labels for the comment first: knowing what happened to a comment is exactly what the audit must not know.
- **One file per person**, named after your GitHub username. To revise your answers, label again on the same page and replace your file in a new pull request.
- Unsure is a fine answer; it is left out of the audit. Unlabeled comments are allowed too, but a complete file helps most.
- **Maintainers merge** label files (they are part of the yardstick). An item's human answer is the majority of at least two people's valid or not-valid verdicts; an item only one person answered is reported apart and not counted. `honed audit-judge` reads every `labels-*.json` and reports the agreement between labelers next to the judge's accuracy against them.

## Proposing a policy change

The policy is what the improve loop hill-climbs: prompts, `config.toml` and `lessons.yaml`. A human may propose a change the same way the loop does, and is held to the same gate.

1. **One change per pull request**, touching only `policy/`. Two ideas are two pull requests, so each one's effect can be measured on its own.
2. **A hypothesis**: which failures the change addresses and why it should fix them, citing evidence from the **train** split only (`honed eval --split train` reports, or `honed improve`'s failure cases, mined from a sample of train). AI-feedback PRs count as evidence only if created before their language's validation period began. Never mine, tune or pick examples from the test split or from the held-out benchmark repos (Martian Code Review Bench, AACR-Bench): that is the one way to make the numbers lie.
3. **The eval report and the decision-log row attached**: evaluate the changed policy on the shared validation split with the same backend and rounds as the incumbent (`honed eval --policy <dir> --split validation --rounds 2`; 2 review rounds per PR is what the packs and every baseline use), attach the JSON report from `data/reports/`, and the row `honed decisions --json` shows for it (or run the change through `honed improve`, which writes both). Say which backend and judge produced it.
4. **The gate must pass** (`METRICS.md`, section 3): a gain above the noise floor with a bootstrap interval that excludes zero, no language and no Important-recall regression beyond tolerance, no rise in false alarms on clean PRs, cost and latency within limits, well-formed output. A change made offline is provisional until re-scored online.
5. **Safety**: no lesson may suppress or downgrade findings in `[safety] high_risk_categories`; the policy loader rejects one that does.

A maintainer re-runs the evaluation before merging into the canonical `policy/`.

## What maintainers own

These define the measure, so they change only by a maintainer's decision, with a re-audit of the judge against human labels where it applies:

- `yardstick/`: the judge's prompts, the blind labeling sample and people's label files (`yardstick/human_labels/`: contributors add their own `labels-<username>.json`, maintainers merge it) and the dataset's removal list (`yardstick/removals.json`, `DATASET.md`). The improve loop may never touch it (`[promote] forbidden_paths`).
- `METRICS.md`, and the thresholds and settings in `honed.toml` outside `policy/`: `[metrics]`, `[gate]`, `[judge]`, `[safety]`, `[eval]`, `[label]`, `[promote]`, `[corpus]`.
- The benchmark mappings (`src/honed/core/benchmarks.py`) and the gold data.
- Promotion to the canonical `policy/`, and releases of the dataset bundle.

## Changing the code

Rules (`CLAUDE.md` has the full list):

- Layers: `core` (pure, no I/O) ← `ports` ← `review` / `learn`; `adapters` implement `ports`; only `cli` wires adapters into services. `uv run lint-imports` enforces it; don't add exceptions.
- Configuration is read only through `honed.config`, from `honed.toml`.
- PR titles, bodies, code, comments and replies are untrusted data: every prompt that includes them says so and ignores instructions inside them (`SECURITY.md`).
- Learning never changes code: the improve loop writes only `policy/`.
- Cite code by symbol name or a quoted expression, never by line number.
- Content adapted from other projects keeps its attribution in the file and in `THIRD_PARTY_NOTICES.md`.
- When a design decision changes, update `ARCHITECTURE.md` in the same change.

## Running the checks

```sh
uv sync
uv run pytest                        # no network, no model calls
uv run ruff check
uv run ruff format --check
uv run lint-imports
uv run python scripts/replay_fixture.py   # the fixture PR must still report its planted bug
```

CI (`.github/workflows/ci.yml`) runs all five on every pull request; the tests also check every committed label file against the sample. The fixture replay answers from cached model answers recorded for the prompts the code builds; if you change how a review prompt is built on purpose, re-record them (`uv run python scripts/replay_fixture.py --record`, which uses a fake `claude` and spends nothing) and say so in the pull request.

`uv run pytest -m live` runs the few tests that call real models (the isolation proof on a Claude subscription, the local model); they are never run in CI.
