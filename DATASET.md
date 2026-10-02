# The Honed dataset

What a Honed dataset bundle contains, where its text comes from and under which terms, how personal data and secrets are handled, how to have something removed, and how to rebuild what the bundle leaves out. The mechanics are in `ARCHITECTURE.md`, section 8; the scoring that uses the data is in `METRICS.md`.

## What a bundle is

One compressed JSON Lines file (`honed bundle export FILE`: `.jsonl.gz`, or `.jsonl.zst` on Python 3.14 and later). The first line is the manifest; every later line is one record, `{"kind": ..., <fields>}`. Records by kind:

| Kind | What it holds |
|---|---|
| `pr` | A pull request (PR): metadata (repo, number, title, description, author login, dates, commits by id, date and headline, reviews by author and state), its review threads with every comment's text, author login, reactions and anchor lines, the mechanical outcome of each thread, and which files had a patch. Plus the attribution of every review comment and of the description (below). |
| `judgment` | One verdict of the judge on one thread (was the comment addressed; the reply's stance and the comment's category), with the judge's reason, or a mechanical check (was a suggestion applied). |
| `judged_labels` | Each thread's label after judging: outcome, polarity, strength. |
| `gold`, `round_gold` | The gold issues of a PR (and of each replayed review round): path, lines, severity, category, provenance, confidence, the judge's description, and the threads it came from. |
| `escaped_defect` | A bug a later fix PR repaired, blamed back to the corpus PR that introduced it. |
| `split` | Each PR's split (`train`, `validation`, `test-public`, or `test` for a benchmark PR), fixed as the bundle's maker had it, so every contributor shares one validation split. The maintainers' `test-private` split is not in a published bundle (below). |
| `round_diff` | A later review round's diff: its file list and merge base, without patches. |
| `decision`, `policy_version` | The improve loop's decision log, and every promoted policy with its full content. |
| `benchmark` | A public benchmark's own record of an imported PR: its golden comments, and the comments its annotators rejected. |
| `cached_answer` | The judge model's cached answers: the cache key's hashes (never the prompt text), the answer and its usage, so re-scoring reproduces the judge's verdicts without asking it again. |

### What it leaves out

- **Code.** No patches, no diff hunks, no context packs. `honed rehydrate` rebuilds them from git (below), so no repository's code is redistributed.
- **Model weights**, and the reviewer models' cached answers (only the judge's are shipped).
- The removed items (below), and in the stripped variant the comment text.
- **The test-private split** (below): its PRs, with everything about them and the judge's answers on them.
- Not yet: the incumbent policy's evaluation runs, which the first release is to carry so the published baseline is re-scorable exactly (`ROADMAP.md`, 5b item 2).

## The manifest

| Field | Meaning |
|---|---|
| `dataset_version` | The release (`[dataset] version`). A removal is followed by a patch release. |
| `bundle_schema`, `store_schema` | The record format (2: attribution, stripped text and these release fields) and the store it came from. A bundle with a newer schema is refused; schema 1 bundles still import. |
| `judge` | The fixed judge: `model` (`claude-fable-5-1`), `effort` (`medium`), `prompts` (each `yardstick/prompts/*.md` file's SHA-256) and `fingerprint` (`<model>:<hash>` of the model, effort and the evaluation prompts; stored evaluation runs name their judge the same way). |
| `licenses` | The license split: `annotations` (`CC-BY-4.0`), `comment_text`, `benchmarks`, `software` (below). |
| `comment_text` | `full`, or `stripped` (text left out, hashes kept). |
| `redaction_rules`, `redactions` | The version of the redaction rule set, and how many hits each kind had. |
| `removals` | The removal list's size (`listed_prs`, `listed_comments`) and what it took out of this export (`prs`, `comments`, `threads`, `gold_issues`). |
| `repos`, `benchmarks` | The source repos with their license as GitHub detects it (SPDX id) and PR counts; the benchmarks with their licenses. |
| `counts` | Records by kind. |
| `test_private`, `test_private_prs` | `excluded` (the default: the maintainers' holdout is not in the bundle) and how many PRs were left out, or `included` (`--include-private`: a maintainers' bundle, never published) and how many are in it. Empty in bundles made before the split existed. |

Every export also writes a report beside the bundle (`<name>.report.json`): each redaction by location (the record and field, the hit's kind and its character offset; never the value), and every removed PR, comment and thread by id. It is for the maintainer who publishes the bundle and is not published with it.

## Provenance and licensing of the comment text

- **Where it comes from.** The corpus PRs were harvested through GitHub's API from the public repositories listed in `honed.toml` `[corpus]` (PRs created in `[harvest] window`, chosen as `ARCHITECTURE.md` section 11 describes). The benchmark PRs' metadata and threads were fetched from GitHub the same way. Comment text is as GitHub returned it at harvest time, apart from redaction.
- **Attribution.** Every review comment and every PR description in a `pr` record carries an attribution: `id` (the comment's GitHub node id; empty for the description), `author` (the GitHub login), `url` (the comment's permalink, or the PR's URL), `text_sha256` (below), `redacted` (the kinds of the hits replaced in it) and `stripped`.
- **Text hash.** `text_sha256` is the hex SHA-256 of the UTF-8 text *as exported*, after redaction. Hashing the redacted text means the hash can't be used to confirm a guess at a redacted value.
- **Licensing status.** The project does not license this text and claims no rights in it: its authors keep theirs. A repository's code license doesn't necessarily cover the discussion on its PRs. The text is quoted from public pull requests, with attribution, so the outcomes, labels and gold issues can be checked against what was actually said. If you need the dataset without it, use the stripped variant and refetch the text yourself under GitHub's terms.

The parts and their terms:

| Part | Terms |
|---|---|
| Our annotations: outcomes, judgments and reasons, judged labels, gold issues and descriptions, splits, decision log, policy versions, the judge's cached answers, the manifest | CC BY 4.0 (`DATA_LICENSE`) |
| Review comments, PR titles and descriptions, commit headlines | Not licensed by this project; each remains its author's (attribution above) |
| Benchmark answer keys | Their own licenses: Martian Code Review Bench, MIT; AACR-Bench, Apache-2.0 (`THIRD_PARTY_NOTICES.md`) |
| Honed's code (not in the bundle) | Apache-2.0 (`LICENSE`) |

## The private test split

The newest fifth of the corpus (by PR creation date, per language) is the test split. `honed splits --assign` stores every PR's split and halves the test split, per language, into `test-public` and `test-private` (`[eval] test_private_share`, 0.5): the PRs in time order are cut into consecutive pairs, and one PR of each pair, chosen by a fixed hash of its key, is held out, so both halves cover the same weeks. `test-private` is the maintainers' holdout (`ROADMAP.md`, governance): it is scored on every promotion so that many contributors tuning against the public validation split can't overfit it unnoticed.

- `honed bundle export` leaves it out by default: its PRs, their threads, judgments, labels, gold and round gold, their escaped defects, their split records, and every cached judge answer the usage ledger ties to one of them. The manifest says so (`test_private`), and the export report counts them without naming them. `honed export-gold` leaves its gold out the same way.
- Maintainers keep it with `--include-private` on either command. Such a bundle is for moving the holdout between maintainers, and is never published.
- What it does not protect against: the private PRs are public GitHub pull requests, inside the published harvest window and repos, so anyone who harvests the same window has their review threads. What stays private is which PRs are held out, and the judge's labels and gold on them. It guards against accidental overfitting, not a determined one; the remedy for the latter is refreshing it with PRs newer than anything published.
- A PR's split never moves once stored: re-running `splits --assign` assigns only PRs without one.

## Imported benchmarks

Martian Code Review Bench (MIT, https://github.com/withmartian/code-review-benchmark) and AACR-Bench (Apache-2.0, https://github.com/alibaba/aacr-bench) are test-split gold only (`honed import-benchmark`). A bundle carries each benchmark's record of its PRs and the gold issues made from its golden comments **verbatim**, under the benchmark's license, never redacted: they are the benchmark's answer key. The benchmark PRs' own GitHub text (title, description, review threads) is scanned and redacted like any other.

## Privacy

What personal data a bundle holds: GitHub logins (PR, comment and review authors, reaction users, who resolved a thread), timestamps, and whatever people wrote in public PR text. No email addresses from commit metadata (commits are carried by id, date and headline only).

Before export, every string a person wrote, and every annotation that might quote one, goes through a secrets and personal-data scan (`src/honed/core/redaction.py`): PR titles, descriptions and commit headlines, review comments, judgment reasons, gold and escaped-defect descriptions, and the judge's cached answers. Each hit is replaced by `[redacted:<kind>]`:

| Kind | What |
|---|---|
| `private_key` | A `-----BEGIN ... PRIVATE KEY-----` block, to its end |
| `aws_access_key`, `aws_secret_key` | `AKIA...`-style key ids; `aws_secret_access_key = <40 characters>` (AWS's documentation `...EXAMPLE` keys are left) |
| `github_token` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` tokens and `github_pat_` fine-grained tokens |
| `slack_token` | `xox[abposr]-` tokens and Slack webhook URLs |
| `stripe_key` | `sk_live_`, `sk_test_`, `rk_live_`, `rk_test_` keys |
| `google_key` | `AIza...` API keys, `GOCSPX-` client secrets, `ya29.` access tokens |
| `anthropic_key`, `openai_key` | `sk-ant-...`; `sk-...`, `sk-proj-...` |
| `jwt` | JSON Web Tokens (`eyJ...eyJ...signature`) |
| `secret_assignment` | The value in `api_key = ...`, `secret: ...`, `client_secret`, `access_token`, `auth_token`, `password` and similar, when it is a literal of 12 or more characters mixing letters and digits that isn't a placeholder (`your-...`, `example`, `xxxx`) |
| `email` | Email addresses, except noreply ones (`...@users.noreply.github.com`, `noreply@`, `no-reply@`), `git@` remotes and the reserved example domains (`example.com`, `.example`, `.test`, `.invalid`, `.localhost`) |
| `phone` | Phone numbers written with separators: `+<country> ...`, `(415) 555-0132`, `415-555-0132` |
| `ipv4` | IPv4 addresses that are globally routable: not the documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), not private, loopback, link-local, shared or reserved ones, not a network address (`.0`), a well-known public resolver or a version number |

The scan is pattern-based: it catches the shapes above and will miss others (names, postal addresses, secrets in unusual formats). If you find personal data or a secret in a published bundle, ask for its removal (below). Redaction changes the text the judge originally saw, so a replayed judge question about a redacted comment misses the cache and is asked again; on the development data 2 of 298 PRs had hits.

## Removal

Anyone may ask for a PR or a review comment to be taken out of future releases: open a GitHub issue titled "Removal request" with the PR's or comment's link, or, to keep the request private, use GitHub's private vulnerability reporting on the repository (the **Security** tab, then **Report a vulnerability**; `SECURITY.md`). You don't need to say why. These two are the only channels until the project has a dedicated contact for removals; while the maintainer is away, a request is acted on when the maintainer is back.

A maintainer adds it to `yardstick/removals.json` (committed; empty until a request comes), with the date and nothing else:

```json
{"prs": [{"pr": "owner/name#123", "added": "2026-10-01"}],
 "comments": [{"id": "PRRC_kwDO...", "added": "2026-10-01"}]}
```

Every later export (`honed bundle export`, `honed export-gold`) honors the list:

- A listed **PR** goes entirely: its record, judgments, labels, gold, round data, split, escaped defects and benchmark record.
- A listed **comment** takes its whole review thread with it, because the replies answer it and the thread's labels, judgments and gold issues were made from it: the thread, its outcome, its judgments and judged labels, and every gold issue built from it.
- The judge's cached answers that the usage ledger ties to an affected PR are dropped too (answers imported from another bundle have no ledger rows and can't be tied).
- Other PRs keep their splits.
- A last check refuses to write any record that still carries a listed PR or comment, and the manifest's `removals` counts what was applied.

The maintainers then publish a patch release (bump `[dataset] version`) and replace the earlier release's asset. Copies already downloaded can't be recalled.

## The stripped variant

`honed bundle export FILE --strip-comments` leaves the text of every review comment and PR description out and keeps its attribution and `text_sha256`. Titles and commit headlines stay. Importing it marks that text as missing, and those PRs are not reviewed or replayed until it is back:

```sh
uv run honed bundle import honed-dataset-stripped.jsonl.gz
uv run honed rehydrate --comments        # GitHub's API: per repo, a query per 100 comments and per 50 descriptions
```

`rehydrate --comments` fetches each comment by its node id (and each description by PR), applies the same redaction the export applied, and keeps the text only when its hash matches, so the text is the one the labels and gold were made on. A comment edited or deleted since keeps its mark and is reported; `--accept-changed` stores the current text anyway (empty when deleted) and releases the PR. It works repo by repo, so a stop (GitHub's rate limit, the run's query budget) keeps what was fetched and a re-run resumes. On the development data (298 PRs in 60 repos, 814 texts) every text came back with a matching hash in about 35 seconds, and the restored store was identical to one imported from the full bundle.

## Rebuilding the code

```sh
uv run honed bundle import honed-dataset-v0.1.jsonl.gz
uv run honed rehydrate                   # clones each repo once (bare, blobless) and rebuilds patches and context packs
```

`rehydrate` restores each patch with `git diff` between the same commits the harvester compared, then builds the context pack and the later rounds' packs. About 3% of rehydrated patches differ from the original in hunk boundaries only, so a replayed review can miss the bundle maker's cache; the gold doesn't depend on patches.

## Gold in Martian's format

`honed export-gold --format martian [--split S] [--repo R] [--out DIR]` writes our gold issues as Martian Code Review Bench golden comments, one file per source repo (`<owner>__<name>.json`, default under `data/exports/martian/`), so a tool's reviews can be scored on our PRs with Martian's pipeline and compared with its leaderboard. Each file is a list of `{"pr_title", "url", "comments": [{"comment", "severity", "category"}]}`, the shape of Martian's `offline/golden_comments/*.json`; it loads with Martian's own `load_golden_comments`.

What goes out: corpus PRs with at least one gold issue (Martian's scorer skips a PR without golden comments; benchmark PRs' answer keys are their benchmarks'), with the removal list and the redaction applied. The comment is our gold description as it is, without file or line, as Martian's own golden comments carry none, so matching ours is as hard as matching theirs.

Martian's scorer matches on a golden comment's text and keeps or drops it by category (its strict, core and all profiles); severity is descriptive only. The mapping (`src/honed/core/benchmarks.py` `MARTIAN_TAG`, `MARTIAN_SEVERITY_OUT`) inverts the import mapping where one exists:

| Our severity | Martian severity |
|---|---|
| Important | High |
| Nit | Low |
| Pre-existing (outside the diff; Martian has no word for it) | Low |

| Our category | Martian tag | Martian profiles that count it |
|---|---|---|
| correctness, error handling, memory/undefined behavior, billing, idempotency | `bug` | strict, core, all |
| types/contracts, compatibility | `api` | strict, core, all |
| security, privacy, auth | `security` | strict, core, all |
| concurrency | `concurrency` | strict, core, all |
| data retention, migrations/schema | `data` | strict, core, all |
| performance | `perf` | core, all |
| tests | `test_gap` | core, all |
| documentation, comments | `doc_defect` | core, all |
| design, style | `style` | all |
| other | `speculative` | all |

Every category in `[safety] high_risk_categories` counts under Martian's default (core) profile.

## The judge

The labels, gold issues and cached answers are the work of one fixed judge: `claude-fable-5-1` at effort `medium`, with the prompts in `yardstick/prompts/` (`addressed`, `classify`, `gold`, `match`, `validity`, `validity_set`). The manifest's `judge` field records the model, the effort, each prompt file's SHA-256 and the evaluation fingerprint, so you can check a bundle against the yardstick you have: a prompt whose hash differs is a different judge, and evaluations under different fingerprints are not compared.
