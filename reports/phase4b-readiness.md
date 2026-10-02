# Phase 4b-2 readiness

2026-10-01. The store, splits and packs for the 4b-2 re-baseline and improvement rounds, prepared without a single model call (store, git and GitHub work only). Nothing here has been evaluated. Design: `ARCHITECTURE.md` section 6; scoring: `METRICS.md`.

## 1. The store

`data/prreview.sqlite` was written by the corpus run's frozen copy (`data/runtime/`, package `prreview`). It was at **schema 3**, not 4 as expected: that copy's `SCHEMA_VERSION` is 3. It was backed up with SQLite's backup API to `data/prreview.phase4b-backup.sqlite` (integrity check ok, every count identical to the live file), then opened with the current code, which migrated it from 3 to 6 (`adapters/sqlite_store.py` `_MIGRATIONS`: two empty steps, then `_source_and_split`). The frozen copy now refuses this store and must not be run again.

| Count | Before (schema 3) | After migration (schema 6) | Now (benchmarks imported, packed) |
|---|---|---|---|
| PRs | 1,425 | 1,425 | 1,664 (+239 benchmark) |
| human-review corpus | 987 | 987 | 987 (+239 benchmark PRs, corpus `human`, source `benchmark`) |
| approval-only (clean) | 225 | 225 | 225 |
| AI-feedback | 213 | 213 | 213 |
| review threads | 7,782 | 7,782 | 7,782 |
| comments | 14,141 | 14,141 | 14,141 |
| judgments | 13,921 | 13,921 | 13,921 |
| addressed | 4,684 | 4,684 | 4,684 |
| suggestion (mechanical) | 7,320 | 7,320 | 7,320 |
| classify | 1,917 | 1,917 | 1,917 |
| judged labels (threads / PRs) | 7,703 / 1,200 | 7,703 / 1,200 | 7,703 / 1,200 |
| gold sets | 987 | 987 | 1,226 (+239 benchmark) |
| gold issues: Important / Nit / Pre-existing | 204 / 1,871 / 13 (2,088) | 204 / 1,871 / 13 | corpus unchanged; +1,592 benchmark (851 / 741 / 0) |
| context packs / round packs | 1,425 / none | 1,425 / 0 | 1,664 / 203 |
| cached judge answers (`llm_cache`, all `claude-fable-5-1`) | 7,485 | 7,485 | 7,485 |
| usage ledger rows | 7,602 | 7,602 | 7,602 |

The labeling run's cost: 7,485 live Fable calls, $385 list-price shadow cost (addressed 4,683 calls $244, classify 1,917 $76, gold 761 $59, audit 124 $6).

## 2. Splits

`honed splits --assign` (new; `learn/splits.py` `assign`, `cli/splits.py` `cmd_splits`) stored every PR's split. Per language group, the human-review PRs with a gold set are ranked by creation time and cut 60/20/20; validation holds at least `[gate] min_prs_per_language` (30) of them per language, taken from train, never from test; approval-only PRs follow the same cut dates; each language's test PRs (human and clean apart) are halved into test-public and test-private by consecutive pairs in time order, one of each pair held out by a fixed hash of its key (`_held_out`); AI-feedback PRs are `train`. Running `--assign` again stored nothing ("0 PRs assigned now; 1425 kept"). Benchmark PRs are `test` (`source = "benchmark"`).

Human-review PRs (with a gold set, all 987 have one); "w/ issue" and "w/ Important" are PRs with at least one such gold issue (round 1, the stored gold sets); clean is approval-only PRs.

| Split | Language | PRs with gold | w/ issue | w/ Important | Gold issues | Important issues | Clean | AI-feedback | Dates (human PRs) |
|---|---|---|---|---|---|---|---|---|---|
| train | TypeScript | 262 | 208 | 46 | 531 | 55 | 66 | 100 | 01-03 to 04-24 |
| train | C/C++ | 150 | 93 | 20 | 300 | 28 | 39 | 73 | 01-19 to 04-29 |
| train | Python | 120 | 103 | 21 | 271 | 25 | 26 | 0 | 01-03 to 04-27 |
| train | other | 50 | 35 | 9 | 111 | 11 | 9 | 40 | 01-16 to 03-31 |
| **train** | **all** | **582** | **439** | **96** | **1,213** | **119** | **140** | **213** | |
| validation | TypeScript | 88 | 72 | 17 | 189 | 18 | 16 | | 04-24 to 05-29 |
| validation | C/C++ | 50 | 31 | 5 | 87 | 6 | 6 | | 04-30 to 05-29 |
| validation | Python | 40 | 37 | 7 | 100 | 8 | 9 | | 04-28 to 05-29 |
| validation | other | 30 (floor; 20 by fraction) | 22 | 8 | 74 | 11 | 7 | | 03-31 to 05-29 |
| **validation** | **all** | **208** | **162** | **37** | **450** | **43** | **38** | | |
| test-public | TypeScript | 44 | 38 | 7 | 114 | 9 | 15 | | 05-29 to 06-30 |
| test-public | C/C++ | 25 | 19 | 4 | 64 | 5 | 5 | | |
| test-public | Python | 20 | 19 | 2 | 31 | 3 | 3 | | |
| test-public | other | 10 | 8 | 2 | 11 | 2 | 2 | | |
| **test-public** | **all** | **99** | **84** | **15** | **220** | **19** | **25** | | |
| test-private | TypeScript | 43 | 35 | 4 | 64 | 5 | 15 | | 05-29 to 06-30 |
| test-private | C/C++ | 25 | 15 | 6 | 35 | 6 | 4 | | |
| test-private | Python | 20 | 17 | 2 | 52 | 3 | 2 | | |
| test-private | other | 10 | 9 | 3 | 54 | 9 | 1 | | |
| **test-private** | **all** | **98** | **76** | **15** | **205** | **23** | **22** | | |
| test (benchmarks) | | 239 | 235 | | 1,592 | 851 | | | Martian 50 PRs (7 repos), AACR-Bench 189 (50 repos) |

Every language reaches 30 validation PRs with a gold set; only "other" needed the floor (train for "other" went from 60 to 50). Stored PR totals: train 935 (582 + 140 + 213), validation 246, test-public 124, test-private 120, test 239.

The private split is left out of `bundle export` and `export-gold` unless `--include-private`. Checked on this store: a default export held 1,544 PRs (1,664 less the 120 private), 6,838 judge answers (7,485 less the 647 the usage ledger ties to private PRs), no private PR or private-tied answer anywhere, and manifest `test_private = "excluded"`, `test_private_prs = 120`. The check bundle was deleted.

## 3. Round packs

`honed pack --rounds 2 --split validation --split test-public --split test-private` (`--split` is new), from the kept clones in `data/clones/`. Round 1's pack existed for every corpus PR; the later rounds were built from the GitHub compare API.

| Split | PRs | PR-rounds at `--rounds 2` | Packed | Built now | Failed |
|---|---|---|---|---|---|
| validation | 246 | 350 | 350 | 104 | 0 |
| test-public | 124 | 167 | 167 | 43 | 0 |
| test-private | 120 | 176 | 176 | 56 | 0 |
| train (not packed, as planned) | 722 | 986 | 722 (round 1 only) | 0 | |

203 PR-rounds packed, no failures, in about 10 minutes (pack median 36 files, 517 KB; p90 60 files, 1.8 MB). At `--rounds 3` validation would have 392 PR-rounds: the 42 third rounds have no pack.

## 4. Benchmarks

`honed import-benchmark martian` and `aacr` from the files already downloaded to `data-dev/benchmarks/` (Martian commit `e616e849`, AACR-Bench `68a56975`): Martian 50 of 50 PRs, 173 gold issues (127 Important); AACR-Bench 189 of 200 PRs, 1,419 gold issues (724 Important), 604 rejected comments kept, 4 PRs without a golden comment. The 11 AACR-Bench PRs that failed are the same 11 as on data-dev: their reviewed commits were force-pushed away (ClickHouse 2, keycloak 5, ComfyUI 2, astral-sh/uv 1, nodejs/node 1).

`honed rehydrate --repo R`, one repo at a time (a driver script, `data/phase4b-packs.log`, per-repo table `data/phase4b-rehydrate.tsv`): **56 of 56 repos, 239 of 239 benchmark PRs packed, no repo or PR failed**, in 24 minutes. Clones are bare, blobless and depth 1, so they stayed small: the largest is mrdoob/three.js at 378 MB, then calcom/cal.com 316 MB and langflow 185 MB; 56 benchmark clones total 2.9 GB, all kept.

Disk: 47.9 GB free at the start, lowest 42.4 GB (sampled after each repo, not continuously), 43.3 GB at the end. The 20 GB floor was never approached, so no clone was deleted. `data/clones` went from 2.3 GB to 5.3 GB, `data/blobs` from 487 MB to 597 MB on disk.

## 5. Cost of one full validation evaluation (estimate; nothing was run)

Measured on the phase 4b-1 baseline (data-dev, dev split, `--rounds 2`, 46 PR-rounds; run `20261001T063720Z-51a718`, and `20260930T220541Z-660d08` for first-time intent and gold costs), at list price (the `claude_code` backend's shadow cost):

| Stage | Model | Calls per PR-round | List cost per call |
|---|---|---|---|
| intent | Sonnet 5.5 | 1 | $0.040 |
| finder a | Sonnet 5.5 | 1 | $0.060 |
| finder b | Opus 5.5 | 1 | $0.144 |
| verifier | Opus 5.5 | 0.93 (skipped with no findings) | $0.170 |
| reviewer total | | 3.93 | $0.416 (the run's mean cost per review) |
| judge: match | Fable 5.1 | 1 per PR-round with gold issues (24 of 29 human PR-rounds on dev) | $0.123 |
| judge: validity | Fable 5.1 | 0.85 (rounds with an unmatched posted finding) | $0.077 |
| judge: later-round gold | Fable 5.1 | 0.64 per later round, first evaluation only (stored afterwards) | $0.14 |

Validation at `--rounds 2`: 350 PR-rounds (312 human, 38 approval-only; 104 later rounds). Validation PRs are about the size of the dev PRs the costs come from (584 vs 630 changed lines on average, packs 538 vs 461 KB), but dev held TypeScript and Python only.

| Part | Calls | List price |
|---|---|---|
| reviewer (intent, panel, verifier) | about 1,380 | about $146 |
| judge match | about 260 | about $32 |
| judge validity | about 300 | about $23 |
| later-round gold (first evaluation only) | about 66 | about $9 |
| **one full validation evaluation** | **about 2,000** | **about $210 (about $105 as Message Batches)** |

Give it ±25%. Related figures from the same rates: a 20% screen is about $42; the 3-sample sensitivity check (three samples of the incumbent plus the weakened policy) is about four evaluations, roughly $700 to $800; as the improve loop is built, it also evaluates the incumbent on the feed split, train (section 6, first item), about $430 for the 722 packed train PR-rounds or $590 for all 986. On the subscription backend, one validation evaluation is a little over half the labeling run's shadow cost ($385), which needed waits through the weekly window.

## 6. What I think is wrong or weak

1. **The improve loop replays train.** `learn/improve.py` evaluates the incumbent on `[improve] feed_split` ("train") as well as on the gate split (`feed_run = ... s.evaluate(incumbent, o.feed_split)`), and the proposer mines that run's failure cases (`propose.failure_cases`). So train does need replay as built: 986 PR-rounds at `--rounds 2`, of which the 264 later rounds have no pack and would be skipped. Either pack train's later rounds and budget the run, feed the proposer a fixed sample of train, or change it to mine from labels and threads.
2. **AI-feedback PRs in train break the time order, and nothing reads them yet.** 95 of the 213 were created after their language's validation cut (45%), so a lesson mined from them can carry information from the validation and test weeks. Today they're bookkeeping only: `learn/splits.py` `eligible` leaves them out of every split's PR list, the proposer reads only the feed split's evaluation, and `learn/lessons.py` rejects evidence citing PRs outside the feed split. When the lesson miner starts reading them, it should take only those before the validation cut.
3. **The private split is private only by obscurity.** Its PRs are public GitHub PRs inside the published harvest window (`[harvest] window`) and repos, so anyone who re-harvests has their review threads. It guards against accidental overfitting of validation, not against a determined effort. Only refreshing it with PRs newer than any published bundle keeps it honest.
4. **Validation is thin on Important issues, and two gate tolerances are below one item.** Validation has 43 Important gold issues in 37 PRs (C/C++: 6 issues in 5 PRs). Rule 3's `important_recall_tolerance` of 0.02 is less than one issue (1/43 = 0.023): a candidate that misses one more Important issue than the incumbent fails. The clean-PR alarm (rule 4, 2 percentage points) counts roughly 90 clean PR-rounds (38 approval-only plus gold-less human rounds), so 2 points is about two PR-rounds. Both are `METRICS.md` settings, which only a human changes; they should be revisited against the noise floor the sensitivity check measures.
5. **"PRs with gold" is read as "with a gold set".** By that reading, validation "other" has exactly 30, but only 22 of them have a gold issue, and 8 have an Important one. If the floor should count PRs with at least one gold issue, "other" needs about 41 validation PRs (train for "other" would drop to about 39).
6. **Test-private is small.** 98 PRs with gold (15 with an Important issue, 23 Important issues; "other" has 10 PRs). Per-language test numbers mean little, and the overfitting alarm's "test S fell" signal will be noisy. Both test halves cover the same weeks (2026-05-29 to 06-30): the test split is essentially June, and validation is essentially May (April and May for "other"), so a release freeze or a seasonal change in one month moves a whole split.
7. **Rounds: packs are at 2, documentation says 3.** `[eval] max_rounds` is 3 and `README.md` and `CONTRIBUTING.md` say `eval --rounds 3`, but the phase 3 and 4 baselines used 2 and the packs are built for 2. `eval` without `--rounds 2` would skip validation's 42 third rounds ("no context pack"). Pick one round count for the re-baseline and make the documents agree.
8. **The judge audit missed its thresholds at the end of the corpus run.** Accuracy 0.725 and Cohen's kappa 0.30 on 40 items (13 negatives; consistency 1.0), reported as not enforced at that size. `METRICS.md` section 5 drops judge-only labels and valid-unlabeled credit while a judge audit fails. Someone should decide whether it counts before the re-baseline, or grow the audit (the human labels in `yardstick/human_labels/` are the cheapest way).
9. **The dev split now leaves stored test PRs out.** `select` drops PRs stored in the test family from `[eval] dev_split`, so `--split dev` on this store is train plus validation (1,181 PRs). Use `validation` and `train` here; dev is for small copies like data-dev.

## 7. Code changed

- `learn/splits.py`: the validation floor (`_bounds`), clean PRs by the human cut dates, the test halves (`_held_out`), `TEST_FAMILY` (`--split test` selects all of it), the dev split without stored test PRs, `ai_feedback`, `table`.
- `cli/splits.py` (new): `honed splits [--assign]` and `split_keys`, which `eval`, `improve`, `pack --split` and `export-gold` now share.
- `Store.set_splits` (`ports/store.py`, `adapters/sqlite_store.py`): one transaction.
- `learn/bundle.py` `_Export`: test-private left out unless `include_private`, with its escaped defects, split records and ledger-tied judge answers; `Manifest.test_private` and `test_private_prs`; `ExportReport.private_prs` and `private_answers` (counted, never named). `bundle export --include-private` and `export-gold --include-private`.
- `honed.toml` `[eval] test_private_share = 0.5`; `config.py` `EvalSettings.test_private_share`.
- Tests: the floor, clean-PR cuts, the private halves (stratified, paired in time, deterministic), the test family and dev, `splits --assign` storing once without reshuffling, and the export leaving test-private out by default. `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run lint-imports` and the fixture replay pass.
- Documents: `CLAUDE.md`, `ROADMAP.md`, `ARCHITECTURE.md` sections 6 and 8, `DATASET.md`, `README.md`; `.gitignore` ignores everything under `reports/` except Markdown.
