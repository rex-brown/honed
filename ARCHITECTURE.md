# Honed — Architecture

A pull request (PR) reviewer that improves itself automatically. It reviews diffs, learns from what happened to its findings and to human reviews, and hill-climbs its own policy against a fixed evaluation. Training runs locally and can run fully offline.

**Status (2026-10-01):** phases 1 to 3, phase 4a and the first half of phase 4b built (judged-severity VU credit, removal exposure, the two well-formed rates, screening, the policy-change self-review lens, the single offline judge, the cross-family alarm's human-accuracy condition, judge fingerprints on stored runs). Implemented so far: `research/repo-selection/` (repo-rigor measurement); `honed.toml` and `config.py`; `core` (types, filters, mechanical outcomes, METRICS.md scoring, suggestions, line mapping, judged labels, agreement statistics, review rounds, policy validation and hashing, the improve loop's records); the ports; the GitHub, SQLite, git, pack and policy-directory adapters; harvesting and context packs, round packs included (`honed harvest`, `pack`, `stats`); the `claude_code`, `local` and `replay` LLM backends with the call cache, usage ledger, plan-usage guard and offline routing; the job runner; labeling, gold sets, the judge audit with human labels from several labelers, and the cross-family audit (`honed label`, `audit-judge [--cross-family]`, `export-audit-sample`, `human-labels serve|check`, `usage`); the seed policy (`policy/`); the review pipeline (`honed review`); the replay evaluation with splits, per-round gold, judge matching, diagnostics, the sensitivity check and the decision log (`honed eval`, `decisions`); escaped-defect mining behind a flag (`honed mine-defects`); the improve loop: proposer, self-review, gate, promote and the driver (`honed improve`). Phase 5a (making it runnable by others) built: the `anthropic` backend (API key, Message Batches, prompt caching, refusal fallbacks, a dollar budget per run), the portable dataset (`honed bundle export|import`, `rehydrate`), human labels in the yardstick, the Martian and AACR-Bench adapters (`honed import-benchmark`), the fixture-PR replay in CI, `--wait-for-reset`'s maximum wait and a clean interrupt of `improve`; the store schema is 6. Everything below marked *(planned)* is not built yet.

## 1. Goals and non-goals

| Goals | Non-goals (v1) |
|---|---|
| Fully automatic improve loop: no human approval gate | The reviewer rewriting its own source code (only `policy/` is mutable) |
| Training portable and runnable offline | Blocking merges (findings never block, like Anthropic Code Review) |
| Learns from every signal: acted-on, reactions/replies, missed human findings, replay eval, LLM judge | Style/format review that linters already enforce |
| Changes to the reviewer are reviewed by the reviewer before they ship | |
| Cheapest model per stage that holds quality | |

Target languages, in priority order: TypeScript, C/C++, Python.

## 2. Big picture

```
 ONLINE ONLY                        OFFLINE-CAPABLE
 ┌────────┐  harvest   ┌──────────────────────┐  label   ┌──────────┐
 │ GitHub │──────────► │ dataset (SQLite +    │────────► │ gold set │
 └────────┘            │ blobs), bundles      │          └────┬─────┘
      ▲                └──────────────────────┘               │
      │ post review                                           ▼
 ┌────┴────────────┐    ┌─────────────────┐  findings  ┌──────────┐
 │ incumbent policy│───►│ review pipeline │──────────► │ evaluate │──► scores
 └────▲────────────┘    └─────────────────┘            └────┬─────┘
      │ promote                                             │ failures
 ┌────┴─────────────────────────────┐   candidate    ┌──────▼──────┐
 │ gate: self-review by incumbent + │◄───────────────│   propose   │
 │ eval on validation split         │  policy diff   └─────────────┘
 └──────────────────────────────────┘
```

Two loops share one pipeline:
- **Production loop:** review real PRs, post findings, harvest the feedback.
- **Improve loop:** propose a policy change, self-review it, evaluate it, promote it only if it wins.

## 3. The policy: what gets hill-climbed

All reviewer behavior that can change lives in `policy/`, as plain, diffable, versioned files. A policy version is identified by the content hash of that directory (`core/policy.py`: sha256 over every file's path and text). `adapters/policy_dir.py` reads the directory and parses `config.toml` and `lessons.yaml`; `core/policy.py` validates the result against rules that sit outside `policy/` (`[safety] high_risk_categories`, `[label] categories`, the `allowed` models of each tunable stage in `[models]`) and can derive a changed policy with regenerated files and its own hash (the weakened policy of the sensitivity check).

| File | Contents |
|---|---|
| `policy/prompts/*.md` | Base instructions for each review stage |
| `policy/lessons.yaml` | Learned rules, in the shape of pstack's Bugbot-triage patterns: `id`, `kind` (`prompt` or `check`), `scope` (language/path globs, repo), `text`, `applies_when`, `skip_when` and `do_not_skip_when` (risk boundaries for rules that suppress findings), `example_signal`, `evidence` (PR refs + outcome ids), `confidence` (candidate → recurring → strong, or retired), `stats` (fires, precision) |
| `policy/config.toml` | Finder panel composition (`specialist`, `shared_rubric` or `mixed`) and its members' models and efforts, model and effort per stage, the verifier switch, context budget, confidence threshold, nit cap, the act-on flag, severity rules (the evidence level Important needs; categories capped at Nit, never a high-risk one) |

Rules for lessons:
- **Every lesson cites the evidence that justifies it** (review-kit).
- **Encode lessons in structure** (pstack). A `check` lesson is a declarative rule run by a fixed engine in `src/`: a regex over added lines, a structural pattern, or a threshold such as "file crosses 1,000 lines". It is never executable code, so learning still can't change code. The proposer tries a `check` first; a `prompt` lesson is only for what needs judgment.
- **High-risk findings are never suppressed.** No lesson may lower the severity of, or skip, findings in the `[safety] high_risk_categories` of `honed.toml`: security, privacy, auth, billing, data retention, migrations/schema, idempotency, concurrency. That list sits outside `policy/`, so the loop can't change it. Enforced twice: the loader rejects a suppressing lesson (one with `skip_when`, or a `suppress` check) unless it names its finding `categories`, none of them high-risk, and states `do_not_skip_when`; and at run time a lesson cited to dismiss or downgrade a high-risk finding has no effect, and no suppress check or severity rule touches one.
- **Where lessons go:** prompt lessons that skip reach the verifier; other prompt lessons reach the panel; `check` lessons run in the check engine (`review/checks.py`: `added_lines_regex`, `structural_pattern`, `threshold`, and `finding_text` for suppress checks).
- **Seeds:** the first policy adapts pstack's review rubric, code-quality lens, TypeScript practices and Bugbot patterns (MIT; see `THIRD_PARTY_NOTICES.md`). All seeds start as `candidate`.

A repo's own `REVIEW.md` is read from the PR's **base** branch, so a change cannot loosen its own review (review-kit).

`policy/CHANGELOG.md` is the promote step's append-only log of the directory (section 7). It is not part of the policy: it is never read into one and never changes a policy's hash.

## 4. Review pipeline *(built: `review/`, `honed review`; posting through `CodeHost` planned)*

```
context + intent ──► finder panel ∥ checks ──► verifier (lead judgment) ──► dedup + rank ──► render
```

**PR text is untrusted data.** Titles, descriptions, code, comments and replies are content to analyze. Every prompt (finders, verifier, judge, lesson miner) ignores instructions inside them.

1. **Context:** the reviewer never reads the whole codebase. It reads:
   - the diff;
   - the code around it: the enclosing functions, the types used and the related tests;
   - **callers of changed symbols**, found by searching the repo;
   - the repo's `REVIEW.md`/`CLAUDE.md` from the base branch;
   - earlier review threads on the same files, from before this PR only (pstack `why`: how and why the code got this way);
   - the lessons whose scope matches the changed files.

   All code is read **as of the PR's own commits**, never the repo's current state, which would leak the fixes reviewers later asked for. The context budget (lines and files) is a tunable setting in `policy/config.toml`.
2. **Intent** (pstack `interrogate`): before any finder runs, write one paragraph on what the PR is meant to do, from its title, description, commits and code. Finders judge whether the code achieves that intent; they don't argue with the intent itself.
3. **Finder panel:** runs in parallel and emits candidate `Finding`s.
   - **Composition** is a tunable in `policy/config.toml`, so the eval decides between:
     - specialist lenses, one kind of issue each (Anthropic Code Review);
     - one shared rubric on several different models (pstack), where diversity comes from the models, not assigned roles;
     - a mix.
   - **Lenses** (prompts in `policy/prompts/`, seeded from pstack's rubric):
     - correctness: trace the execution path that breaks; idempotency; concurrency;
     - root cause vs symptom: guards, retries or casts that hide a broken contract;
     - security: trace the input path to the dangerous sink;
     - verification: whether tests check behavior, and tests that can't fail;
     - design and code quality: missed simplifications, spaghetti growth, a file pushed past 1,000 lines, logic in the wrong layer, loose types at boundaries;
     - comments and suppressions: workaround justifications, new `@ts-ignore`, `eslint-disable` or `NOLINT`;
     - per language: TypeScript (type-system discipline: discriminated unions, no unvalidated `as`, `unknown` over `any`, exhaustive matching), C/C++ (memory, undefined behavior, ownership), Python.
   - **Consensus:** a finding raised independently by 2 or more panel members is flagged, which raises its confidence.
4. **Checks:** `check` lessons (section 3) run as code, not prompts. They are cheap, exact and work offline.
5. **Verifier (lead judgment):** checks every candidate against the code, as a pragmatic senior engineer rather than an aggregator (pstack `lead-judgment`, `blast-radius`). Before it, candidates at the same place with enough shared words are merged (`review/rank.py`), so the verifier sees how many members raised each; it can also mark duplicates itself. A policy can switch the verifier off, which posts every candidate at its proposed severity (the weakened policy of section 6).
   - **Evidence level** for each finding:
     1. asserted;
     2. cites the line;
     3. traces the failing path step by step;
     4. ran code that shows it;
     5. reproduced it in the running program.

     Important needs level 3 or higher. Levels 4–5 need a sandbox that can run the code, which is a later phase. **The verifier assigns the level from what it checked itself**; a finder's own claim about its evidence is ignored (the phase 3 baseline showed self-reported levels carry no precision signal). Built (`review/verifier.py`): any level a proposed finding carries (a check's, a finder's) is dropped before verification, the finder's trace reaches the verifier as "a claim to check", and the verifier answers `checked` (what it confirmed in the code shown, with the lines it read) beside the level; a level above 1 counts only with a non-empty `checked`. Without a verdict, or with the verifier off, a finding stays at level 1.
   - **Filters:** dismiss hypotheticals that callers can't actually reach (trace the call site), "I'd have done it differently" preferences, premature-abstraction suggestions, and findings that reveal missing context. Never dismiss a security or correctness finding without tracing it, even when only one panel member raised it.
   - **Buckets:** act on, consider, noted, dismissed, each with a one-line reason. More than 5 "act on" findings on one PR means the verifier isn't filtering hard enough.
6. **Dedup and rank:** merge duplicates, recording which panel members raised each; apply the severity rules, confidence threshold and nit cap.
7. **Render:** inline comments plus a summary through the `CodeHost` port, or JSON for offline and eval runs.
   - "Act on" becomes an inline Important or Pre-existing comment. "Consider" becomes an inline Nit or a summary line. "Noted" is a count in the summary.
   - "Dismissed" is a collapsed list in the summary, with reasons, so humans can override it (pstack: the dismissed list is a trust mechanism). Dismissed candidates are also logged. One that later matches a real issue counts against the verifier.
   - Comment text meets a writing standard enforced by a deterministic lint (pstack `unslop`): name the mechanism and the fix, in active voice, with no hedging, filler, praise or chatbot phrases. An empty review is valid.
   - **Re-reviews** (Anthropic Code Review, pstack `babysit`): on later pushes, post only new Important findings; check each one against the current head, not the head it first saw; and never repeat a finding a human dismissed.
   - Every posted comment carries a hidden marker `<!-- honed finding=<id> policy=<hash> -->`, so feedback can be traced back to the exact policy version.
   - 👍/👎 reactions are pre-attached to each comment.

Severity (same as Anthropic Code Review):
- **Important:** would break behavior, security or the build.
- **Nit:** worth fixing, not blocking.
- **Pre-existing:** a real bug the PR didn't introduce.

Each finding also has a **category** (correctness, security, design, tests, comments, performance, and so on). A design finding is Important only when it meets pstack's approval bar: for example, a file pushed past 1,000 lines, ad-hoc branching tangled into a shared flow, or logic in the wrong layer when a clear home exists. Otherwise it is a Nit.

## 5. Signals → labels *(built: mechanical outcomes, applied suggestions, the addressed check, reply stance, judged labels and gold sets)*

Outcomes are derived mechanically, first match wins, from harvested thread data. `core/outcomes.py` implements this. `honed label` (`learn/label.py`) then adds judgments and turns both into judged labels (`core/labels.py`) and gold sets.

| Signal | Label | Strength |
|---|---|---|
| 👎 reaction on the finding | `thumbs_down` | strong negative |
| The flagged lines changed in a later commit before merge (compare patches; fallback: GitHub `isOutdated`) | `fixed` | strong positive |
| An applied ` ```suggestion ` block (the suggested text is in the change to the flagged lines, or a suggestion commit followed; `core/suggestions.py`) | `fixed`, provenance `applied_suggestion` | strong positive |
| Resolved, lines unchanged | `resolved_no_change` | weak negative, calibrated per repo |
| A reply to the finding | stance (agree / disagree / fixed elsewhere / question / other), classified by the judge | medium (agree and fixed elsewhere positive, disagree negative) |
| Unresolved, no reply | `ignored` | weak negative |
| A human caught an issue the reviewer missed | `missed` gold issue | strong (drives recall) |
| LLM judge verdict (Fable) | valid / invalid / severity | medium; never overrides a strong human outcome |

**Per-repo calibration:** a signal's weight depends on how informative it is in that repo. Example: dotnet/runtime resolves about 100% of AI-bot threads, so "resolved" carries no information there.

**`fixed` means the lines changed, not that the comment was addressed.** In the phase 1 sample, 87% of human threads came out `fixed`: authors often rewrite a region for other reasons, and base-branch merges and adjacent insertions also count. So before a `fixed` thread becomes a strong positive or a gold issue, the judge confirms the change actually addressed the comment, from the comment plus the before and after code (`addressed`, `partially` or `not_addressed`). Threads the judge rejects become `changed_unaddressed` and count as neither positive nor negative. Applied suggestions skip the check. In phase 2's 36 PRs, the judge confirmed 69% of human `fixed` threads (applied suggestions included) and rejected 38% of the ones it checked.

**High-risk dismissals aren't noise.** When a human dismisses a finding in a high-risk category (section 3), that is the owner's judgment call, not evidence the finding was wrong (pstack Bugbot triage). It never counts as a negative label for a lesson. The judge categorizes every dismissed finding (👎, resolved without change, ignored, or a disagreeing reply) so the check can run; a high-risk one is stored as neutral with `high_risk_dismissal` set.

**Gold set per PR:** the real issues a reviewer should report. They come from human threads with strong outcomes, applied suggestions, and findings validated by the judge with agreement. Each `GoldIssue` records its provenance and a weight. A `partially` addressed thread gets a lower confidence than a fully addressed one.

**Escaped defects as gold** *(built behind a flag: `honed mine-defects`, `[eval] include_escaped_defects`)*: the phase 2 sample was 95% Nits, with 2 Important issues in 16 PRs, because reviewers in well-run repos rarely leave bugs in merged PRs. Bugs the reviewers missed are still gold, and they are findable: mine later bug-fix PRs, blame the lines they fix back to the corpus PR that introduced them, and record an Important gold issue at those lines, described by the fix. Provenance `escaped_defect`. This is the classic SZZ method, so its known noise (refactors and formatting changes blamed as bug-introducing) is filtered by requiring the fix PR to be labeled or titled as a bug fix and to touch the lines, not just the file. `learn/defects.py` lists PRs merged in the `[defects] after_days` after the corpus window, keeps bug fixes by `fix_title` or `fix_labels`, blames the old lines each fix hunk rewrites (pure insertions and hunks rewriting more than `max_fix_lines` are skipped) at the landing commit's parent in a deepened clone, maps a blamed commit to a corpus PR by its commits or a landing message naming it, and locates the lines at that PR's reviewed commit. The eval adds them to round 1's gold of human-review PRs only when `[eval] include_escaped_defects` is set; an approval-only PR with an escaped defect still counts as clean (a known gap).
- Built so far from: addressed (or partially addressed) `fixed` human threads, applied suggestions, and `open_at_merge` human threads with an agreeing reply, with `conf` from `[metrics] gold_conf`. Judge-only issues are not built yet.
- The judge groups threads that raise one issue, and assigns each issue a severity (Important, Nit, Pre-existing) and a category from `[label] categories`, in one call per PR.
- Only issues whose flagged code exists at the reviewed commit are kept (section 6); locations are in that commit's lines. Later-round threads are counted per PR (`GoldSet.excluded_later_round`).

## 6. Evaluation *(built: `learn/evaluate.py`, `honed eval`, the cross-family audit, the benchmark imports; test-split tracking planned)*

- **Replay, per review round:** a PR is replayed once per review round in which humans left inline threads, at that round's commit, up to `[eval] max_rounds` rounds per PR (the first, then the ones with the most threads). It is 2: the round packs (`honed pack --rounds 2`) and every baseline use 2, so `eval` without `--rounds` covers every packed round. Phase 2 showed why: replaying only the first round discarded 53% of gold issues, because iterative PRs get most of their review later. For round k the reviewer sees the PR at that commit plus the threads from earlier rounds, as a real re-review would, and its gold set is the round's new threads whose flagged code exists at that commit. A PR-round is the unit of scoring; the bootstrap resamples whole PRs.
  - Rounds (`core/rounds.py`): round 1 is the PR's reviewed commit; every other anchor commit of a human thread is a later round. The replay reads the round pack saved at the round's commit (`honed pack --rounds N`), sees the commits up to it, and sees the review threads opened before the round began, with only the replies made by then. A replay is a full review with that discussion as context; re-review mode is for the reviewer's own earlier findings.
  - Gold per round: each gold issue counts once, in the latest replayed round at or before the round it was raised in whose commit has the flagged code. With one round replayed this is the stored gold set; issues on code the reviewed commit didn't have are grouped by the judge per round, like the stored set, and saved per round (`learn/replay.py`).
- **Match:** the judge matches findings to gold issues (`yardstick/prompts/match.md`): one-to-one, same location and same underlying problem; a finding may instead repeat another finding (DUP). Unmatched findings get a blind validity verdict (`validity.md` plus `validity_set.md`, one call per PR-round), so a real issue the humans missed is not scored as a false positive; for a valid one the judge also rates the severity itself, Important or Nit, without seeing the claimed severity (`Match.judged_severity`; METRICS.md section 1 credits only a judged Important). Dismissed and noted findings are matched too, without the one-to-one rule, for the verifier's false-dismissal rate.
- **Objective and gates:** fully defined in **`METRICS.md`**.
  - The headline score S is F0.5 (weighted toward precision), severity-weighted and averaged across languages with weights TS 0.45, C/C++ 0.25, Python 0.20, other 0.10.
  - The gate also checks per-language floors, Important-issue recall, the false-alarm rate on clean PRs, cost, latency, policy size and self-review.
  - Production metrics (acted-on rate, 👎 rate) drive rollback and lesson promotion.

| Split | Contents | Used for |
|---|---|---|
| train | Harvested PRs, oldest period (about 60%), and every AI-feedback PR | Proposer mines failures (on a sample, section 7) and lessons (AI-feedback PRs only from before the validation cut) |
| validation | Harvested PRs, middle period (about 20%, at least `[gate] min_prs_per_language` PRs with gold per language) | Gate accept/reject |
| test-public | Half of the newest period (about 10%) | Scored every round and reported; never used for decisions; in the published bundle |
| test-private | The other half of the newest period | The maintainers' holdout: scored on every promotion; never used for decisions; left out of bundle exports |
| test (benchmarks) | Martian Code Review Bench and AACR-Bench (their repos are excluded from train and validation) | Scored every round and reported; never used for decisions |

Splits are by PR, stratified by language, and time-ordered so the future never leaks into training. `--split test` selects the whole test family (benchmarks, public and private).

Trusting the eval (pstack `hillclimb` and `eval`):
- **Prove it can tell good from bad.** Before trusting the eval, and whenever the models or data change, confirm it separates the incumbent from a deliberately weakened policy by more than the noise floor. Then freeze it. `honed eval --sensitivity`: the incumbent twice (model samples 0 and 1), then the weakened policy (verifier off, every lesson removed, every stage at the lowest effort). The noise floor sigma is the standard deviation of delta-S between two identical runs over the paired bootstrap's resamples; with `--samples K` (K >= 2 samples of the policy) it pools every pair (the root mean square of their standard deviations), so more samples estimate it better. `min_gain = max(0.01, 2 sigma)` goes into the report and the decision log, never into `honed.toml`. The noise-floor and separation rows are what the gate's precondition reads (section 7).
- **Splits** (`learn/splits.py`, `honed splits [--assign]`): time-ordered by PR creation within each language group; eligible PRs are human-review PRs with a gold set and approval-only PRs (the clean set).
  - The human-review PRs with a gold set are ranked by creation time per language and cut by `[eval] split_fractions`. Validation holds at least `[gate] min_prs_per_language` of them per language, so every language's floor (METRICS.md section 3, rule 2) applies; the shortfall comes out of train, never out of test. Approval-only PRs follow the same cut dates: each goes to the split whose period its creation date falls in (before 2026-10-01 they were ranked together with the human PRs).
  - `honed splits --assign` stores every PR's split (`prs.split`), and halves each language's test PRs, human and clean apart, into `test-public` and `test-private` (`[eval] test_private_share`): consecutive pairs in time order, one PR of each pair chosen by a fixed hash of its key, so the halves cover the same weeks and the assignment is deterministic. AI-feedback PRs are stored as `train` (no gold). `honed splits` alone prints the table per split and language (PRs with a gold set, with an issue, with an Important issue, clean, AI-feedback, benchmark) and what `--assign` would store.
  - A PR with a stored split keeps it: an imported benchmark PR is always `test`, an imported bundle fixes every PR's split as its maker had it (contributors share one validation split), and `splits --assign` fixes the rest, so re-running it never reshuffles. PRs without a stored split are assigned among themselves by the same rule, their test PRs as `test` (the private half exists only once stored).
  - Test-private is the maintainers' holdout (ROADMAP.md, governance): `bundle export` and `export-gold` leave it out unless `--include-private` (section 8, DATASET.md). Its PRs are public pull requests inside the published harvest window, so it protects against accidental overfitting of validation, not against someone harvesting the same window.
  - While the dataset is small, `[eval] dev_split` names one split holding every eligible PR except the benchmark ones (the dev split can feed the proposer). It is for stores without stored splits: on a store with them it would silently mean train plus validation, so `--split dev` refuses there with a message pointing to `train` and `validation` (`learn/splits.py` `SplitError`).
  - `splits.validation_cuts`: per language, the creation time of its earliest validation PR, where its validation period begins. The feed selector's time cut reads it (section 7).
  - `honed pack --split S` (repeatable) narrows packing to a split's PRs; 4b-2 packed later rounds (`--rounds 2`) for train, validation and both test halves.
- **Storage:** runs are stored keyed by policy hash, split, backend, round count and sample, with every review and judge verdict; one run per split, backend and round count is the incumbent that `eval` compares against with the paired bootstrap. Each run records its judge (`EvalRun.judge`: the answering model and `LLMJudge.fingerprint`, a hash of the judge model, effort and the match and validity prompts). A stored run is reused only under the same judge; otherwise the policy is re-evaluated, its reviews served from the call cache, so only the judge's calls are live. `eval` warns when its incumbent was judged by another judge.
- **Blinding:**
  - Replayed PRs look exactly like production input, with no eval cues in prompts or paths.
  - The judge sees findings under neutral labels, never which policy or model produced them.
  - When comparing two variants, one judge scores both in one pass on one scale. **Offline, that judge is the local model for both sides**; cached Fable verdicts are not mixed in, because mixing a strict and a lenient judge across the two sides would bias the comparison. Offline promotions stay provisional until Fable re-scores both sides online.
- **Cross-family audit** *(built: `learn/crossfamily.py`, `honed audit-judge --cross-family`)*: Claude judging findings from Claude reviewers risks self-preference. The local model (section 9, another family) re-asks a sample of Fable's questions from stored runs: the validity question of PR-rounds' unmatched posted findings, rebuilt with the same evidence and labels (`learn/evaluate.py` `round_questions`, so the prompt is byte-identical), gives agreement and Cohen's kappa against `[judge] cross_family_alarm_kappa`; the match question of PR-rounds with gold issues gives the offline matcher's precision, recall and F1 on (finding, gold issue) pairs against `[judge] matcher_min_f1`. PR-rounds with an invalid verdict are sampled first (they are rare), and rounds where Fable matched something first for the matcher, each in a fixed pseudo-random order. No Claude calls. First run (data-dev, 2026-10-01, Qwen3.8-27B against Fable over the four stored dev runs; all 111 validity and 68 match questions rebuilt from them hit Fable's cache keys): validity agreement 0.53 and kappa 0.07 on 60 findings (half of them Fable-invalid, by the sampling), the alarm; the local judge ruled 58 of 60 valid, so the disagreement runs opposite to self-preference: Fable is the stricter judge, and the local one barely discriminates. Matcher F1 0.944 (precision 1.0, recall 0.895 on 19 of Fable's pairs in 20 PR-rounds): it passes `matcher_min_f1`. No answer failed to parse. The alarm therefore counts only once the local judge's own validity verdicts on the imported human labels reach `[judge] local_min_human_accuracy` (the audit asks it each human-labeled comment through the judge audit); until then it is printed as report-only, with the reason ("no human labels yet").
- **Human labels** *(built: `honed export-audit-sample`, `human-labels serve|check`; `tools/human-labels/`)*: the blind sample is committed in the yardstick, `yardstick/human_labels/sample.json` (`[paths] human_labels`): per item the thread id, repo, PR, path, language, the first comment's text (redacted by `core/redaction.py`, and without the status lines bots add after the fact, "✅ Resolved in ...", `core/blinding.py`), its author's login and permalink (attribution), the commit it was made on and the flagged lines. No code and no diff hunks: the project never redistributes code. `export-audit-sample --ids-from OLD` rebuilt the first sample (drawn 2026-09-30 with code excerpts, kept in the gitignored `data/human_labels/`) from the store in this shape; a fresh draw skips old-side (LEFT) comments, whose lines belong to a base file the item can't name, and the command refuses to overwrite a sample without `--force`, since labels are keyed by its ids. Anyone labels it on a self-contained local page, `tools/human-labels/index.html`, served by `honed human-labels serve` (stdlib `http.server` on 127.0.0.1, `adapters/label_server.py`; it serves only the page, the sample and `/meta.json`, because browsers may block `fetch` from `file://`). The page fetches each file from `raw.githubusercontent.com` at the item's commit and shows it with line numbers, the flagged lines highlighted and `[label] region_context_lines` of context (the judge's window); when GitHub no longer serves the commit (force-pushed branches; 6 of the first 50), it shows the comment's diff hunk from GitHub's REST API instead, and when neither is reachable, a link to the file at the commit. It never shows the thread link, the author, or anything about what happened next, and it shows the judge's own definitions (`yardstick/prompts/validity.md`; a test keeps them identical). Verdicts (1/2/3: valid, not valid, unsure) and notes autosave in `localStorage`; "Save labels" downloads `labels-<username>.json` (`{labeler, labeled_at, honed_version, items: [{id, verdict, note}]}`), which the labeler adds by pull request (`CONTRIBUTING.md`). `honed human-labels check` and a test validate every committed file against the sample (known ids, verdicts, one file per person, the file named after its labeler; `adapters/human_labels.py`). `audit-judge` and the cross-family audit read every `labels-*.json` (`core/consensus.py`): an item's human answer is the majority of at least 2 labelers' valid or not-valid verdicts ("unsure" excluded, ties dropped), source `human_majority`, replacing a human-strong outcome on the same thread; an item with one usable verdict is judged but reported separately and flagged (source `human_single`), never counted. Reported beside the judge's accuracy and Cohen's kappa against the majority: inter-annotator agreement, Fleiss' kappa with 3 or more labelers (each item with its own number of usable verdicts; exactly Fleiss' when every labeler answered every item), Cohen's kappa with 2. Known gap: the judge's own validity question still sees those bot status lines (about 157 of 7,782 stored first comments carry one); changing that changes the judge's inputs, so it is a yardstick decision.
- **Spot audits:** each round, a sample of reviews is read end to end and compared with the judge's verdicts.

## 7. Improve loop *(built: `learn/propose.py`, `selfreview.py`, `gate.py`, `promote.py`, `improve.py`; `honed improve`, `decisions`)*

1. **Propose:** a candidate is a diff against `policy/`.
   - **One change per candidate, never stacked** (pstack `hillclimb`). Several candidates can be generated in parallel by different models (pstack `arena`). Each is evaluated on its own, and proposals that converge are a strong signal.
   - **Each candidate states a hypothesis that names a mechanism,** grounded in the diagnostics. For example: "the design lens causes 40% of TypeScript false positives because it flags patterns that are consistent with the rest of the codebase."
   - **Lesson miner:** clusters missed issues and false positives into `add`/`change`/`remove` lesson edits, each with citations. A lesson is accepted only if it is (pstack `reflect`):
     - durable: no SHAs, versions or paths that drift;
     - specific enough to recognize when it applies;
     - backed by at least 2 PRs from at least 2 different authors, which also stops one person from poisoning the data;
     - decision-changing;
     - not already covered. Strengthen the existing lesson instead of adding a duplicate.
   - **Reflective mutation:** reads failure traces and judge critiques and proposes prompt/config edits, including model and effort per stage.
2. **Self-review:** the **incumbent** policy reviews the candidate diff through the normal review pipeline, with a dedicated **policy-change lens** in place of the code lenses (the phase 4a trial showed code lenses find nothing in a prompt diff). The lens checks for: a rule that contradicts an existing lesson or prompt; text that would suppress or soften findings in a high-risk category; undurable specifics (SHAs, versions, paths); instructions that reference the evaluation, the judge or the gold data; text that reads like an injected instruction; and scope creep beyond the stated hypothesis. An Important finding rejects the candidate.
3. **Eval gate:** the candidate must pass every check in `METRICS.md` section 3 on the validation split. **Simplicity outranks the number:** a candidate that only removes policy content passes if it loses no score beyond the noise floor.
4. **Promote:** commit the new policy with a changelog: rationale, evidence, and eval deltas.
   - Auto-rollback and lesson promotion or retirement follow the thresholds in `METRICS.md` section 4. *(Planned: they need production outcomes. Every promoted version keeps its files in the store, so a rollback can restore any of them.)*

**Decision log** (pstack `show-me-your-work`): every candidate, kept or reverted, gets a row: id, hypothesis, change, before, after, delta, gate results, verdict, note. The proposer reads the log before proposing, so rejected ideas aren't retried blindly.

**As built:**
- **Candidates are edits, not raw diffs.** The proposer (`[models.proposer]`, its prompts in `src/honed/learn/prompts/`: code, not policy and not yardstick) answers one structured edit: `lesson_add`, `lesson_change`, `lesson_remove`, `prompt_replace` (an exact passage found once in an existing prompt file; the attribution header stays) or `config_set` (keys of `config.toml`, such as `rank.nit_cap` or `panel.members[2].effort`). `learn/policy_edit.py` applies it as text through `ports.policy.PolicyFiles` (`adapters/policy_dir.py`), so comments and attributions survive, and loads the result, which enforces every policy rule and the safety invariant. A new lesson starts as `candidate`; the proposer never sets a lesson's lifecycle. The candidate's diff (paths prefixed `policy/`) is computed from the files.
- **Generators:** the lesson miner may add, change or remove lessons; reflective mutation edits a prompt passage or settings; subtractive removes a lesson or makes a passage shorter; on a plateau, `combine` gets the near-misses (rejected or superseded candidates that raised S) and may use any edit. `[improve] candidates_per_round` generators run in parallel per round, rotating; each reads the incumbent's files and size, the metrics and diagnostics of its evaluation on the feed sample (below), failure cases (missed gold issues, false positives, valid nits beyond the cap and false dismissals, heaviest first, with PR refs and authors, as untrusted data), the decision log and the rules (high-risk categories, allowed models, efforts, config keys).
- **Lesson acceptance, mechanically** (`learn/lessons.py`): durable (no commit hash, version number or file path in its text), specific (a prompt lesson says when it applies), evidence from `[lessons] min_prs` PRs of the feed (the feed split's PRs and the AI-feedback PRs the time cut admits, below) by `min_authors` PR authors (citing any other PR is an error), not already covered (word overlap with an active lesson), a new prompt lesson says why a check can't express it, and a flag check fires on a line its cited PRs added. Decision-changing is otherwise left to the evaluation.
- **Order of checks per candidate:** apply and load; refuse paths the promote step may not write; lesson acceptance; skip a policy identical to one already rejected or screened out; self-review (the diff as a synthetic PR of repo `honed/policy`, the incumbent's files at the base commit and the candidate's at the head, reviewed by the incumbent pipeline with its `prompts/policy_change.md` lens in place of the code lenses; any posted Important finding rejects, and its title goes into the decision-log note the proposer reads); the screen (METRICS.md section 3: a fixed, language-stratified `[gate] screen_fraction` of the gate split, compared with the incumbent's stored full run on the same PR-rounds; a delta-S not above 0 is `screened_out`, and the full split never runs); evaluation on the gate split (sample 0, paired with the incumbent's run); the gate (`learn/gate.py`, every METRICS.md section 3 rule with its numbers; a pure removal whose deleted lessons fired on fewer than `removal_min_exposure` PR-rounds, or that touches prompt text or settings, is `unmeasured` rather than kept for simplicity).
- **The self-review lens** (`ReviewOptions.focus`): every panel member applies `policy_change.md` alone (no code lenses, no language lens, no lessons, no checks: they are about code), and the incumbent's verifier reads it too, including what evidence levels mean for policy text. It is part of the policy, so it is versioned with the incumbent, but the improve loop may not edit it (`learn/policy_edit.py`), the proposer never sees its text, and it doesn't count toward the policy's prompt tokens (a code review never sends it).
- **One promotion per round:** the passing candidate with the largest delta-S is promoted; others that passed are `superseded`. Promotion (`learn/promote.py`) refuses a diff touching any path `[promote]` doesn't permit, records the version in the store (hash, parent, diff, rationale, the gate's numbers, the provisional flag and every file), writes the directory, appends to `policy/CHANGELOG.md`, and makes the candidate's run the incumbent.
- **Precondition:** the latest sensitivity rows of the decision log must say the eval separates; otherwise the loop refuses. `--ignore-sensitivity` overrides it with a loud warning, and its promotions are provisional, with the warning in their decision-log rows and changelog entries. Offline runs are provisional too.
- **Splits:** the proposer mines `[improve] feed_split` (train), the gate decides on `--split` (validation). With the one dev split both are dev (the gate run serves the proposer too), and the round report says so.
- **The feed sample** (`learn/feed.py` `FeedSelector`, `draw`): the incumbent is evaluated for the proposer on `[improve] feed_sample_rounds` (150) PR-rounds of the feed split, not all of it (train is 986 PR-rounds at 2 rounds, about $590 a run at list price).
  - Stratified by language: each language gets its share of the pool's PR-rounds (`core/sampling.py` `largest_remainder`).
  - Whole PRs are drawn, each with every replayed round that has a context pack (a PR-round without a pack is left out of the pool, so it never becomes a skipped unit), and a PR whose rounds don't fit the language's remaining quota is passed over for one that does.
  - PRs with at least one gold issue (in the stored gold set) come first. Among them, PRs with an Important gold issue are drawn at the share that language's human-review PRs in the pool have, so preferring PRs with gold neither floods the feed with Important issues nor starves it. PRs without a gold issue, clean PRs included, fill only what the others can't.
  - The order is a hash of the salt and the PR key; the salt is the incumbent's hash. Every round with the same incumbent draws the same sample, and its stored run (split label `<feed split>/feed`) is reused; a promotion redraws it.
  - The round report (`improve-<run>.json`, `feed`) records the sample's PR-rounds (`owner/name#N@round`), its composition per language beside the pool's (PR-rounds, PRs, later rounds, PRs with a gold issue and with an Important one, the target Important share) and the run it was evaluated in.
  - The later rounds it can draw were packed with `honed pack --split train --rounds 2` (git and the compare API, no model calls: 264 round packs, none failed), so the sample draws from all of train's 986 PR-rounds, later rounds included.
  - On the live store (2026-10-01, the seed incumbent `c4c3ca013ea8`): 150 PR-rounds of 98 PRs (TypeScript 68, C/C++ 37, Python 32, other 13; 52 later rounds), every PR with a gold issue (242 issues), 16 with an Important one (16.3%, against 16.5% of train's human-review PRs). Estimated at about 900 calls and $95 at list price (reviewer $62, judge match $18, validity $10, first-time later-round gold $5; ±25%), paid when a sample is drawn (a run's first round and the round after a promotion) and reused otherwise; the whole of train was about $590.
- **The time cut** (`learn/feed.py` `admitted`, applied only in `FeedSelector.ai_feedback`): an AI-feedback PR may be read by the proposer or the lesson miner only if it was created before its language's validation cut (`splits.validation_cuts`; a language without validation PRs takes the earliest cut, and with no validation split none is admitted). AI-feedback PRs are stored as `train`, but 45% of them were created in the validation and test weeks, so without the cut a lesson could carry information from them. Today they reach the loop only as lesson evidence (`FeedSelector.evidence`: the feed split's PRs and the admitted AI-feedback PRs); any later reader of them goes through the same selector. Feed-split PRs need no cut: train is older than validation by construction.
- **Plateaus and stopping:** after `[improve] plateau_rejects` candidates in a row without a promotion, the next round starts at the least-tried generator and gives one slot to `combine` when there are two near-misses. The loop stops after `--rounds`, or once the incumbent reaches `[improve] target_s` after `--min-attempts` candidates; the target is never relaxed. A plan limit or the call cap stops it cleanly, the candidate in flight recorded `incomplete`. Each round writes a report (`improve-<run>.json`).

**Plateaus:** after several rejects in a row, pivot to another category, combine near-misses, or try something more radical. A run's stop condition pairs a target with a minimum number of attempts, and it is never relaxed to declare success.

**Safeguards against gaming the score:**
- The proposer can only write to `policy/`. The promote step rejects diffs that touch anything else, and never accepts one that touches `[promote] forbidden_paths` (`yardstick/`, `src/`, `tests/`, `honed.toml`, `METRICS.md`, `ARCHITECTURE.md`), even if `allowed_paths` is widened.
- The judge model, its prompts (`yardstick/prompts/`, outside `policy/`) and the gold data are fixed, so the yardstick doesn't move.
- The judge comes from a different model tier than the reviewer, and a cross-family audit checks for self-preference.
- Human outcomes outweigh LLM verdicts.
- Lessons need evidence from at least 2 authors, PR text is untrusted data, and high-risk findings are never suppressed.
- Policy length and cost are penalized, because a long rules file dilutes the rules that matter.

## 8. Offline and portability

- **LLM port backends:**
  - `claude_code` (online, the default): headless Claude Code (`claude -p`) on the user's Claude Max subscription. This is Anthropic's documented way to use a subscription from scripts. Calling the Messages API directly with a subscription token is not permitted.
    - **Isolation:** every call runs isolated, so the user's CLAUDE.md, memory, hooks and MCP servers never reach reviewer or judge prompts. It uses (`adapters/claude_code_llm.py`):
      - an empty temporary working directory, with the user content on stdin;
      - `--safe-mode` (`[llm.claude_code] isolation = "safe_mode"`, the default), or `config_dir`: `--safe-mode` plus a fresh `CLAUDE_CONFIG_DIR` with `CLAUDE_CODE_OAUTH_TOKEN` from the environment or a Keychain item holding a `claude setup-token` token;
      - no tools (`--tools ""`, `--strict-mcp-config` with an empty `--mcp-config`), `--max-turns 1` (2 with a JSON schema: phase 3 saw about 1.5% `error_max_turns` with one turn, and the only tool is still StructuredOutput), `--no-session-persistence`, `--disable-slash-commands`, `--setting-sources ""`, and `CLAUDE_CODE_DISABLE_AUTO_MEMORY`/`CLAUDE_CODE_DISABLE_CLAUDE_MDS`;
      - a replaced system prompt (`--system-prompt-file`);
      - `--json-schema` for structured output;
      - a child environment without the parent's `ANTHROPIC_*` and `CLAUDE*` variables, so a call can never bill an API key.

      Every call's `system/init` event is checked (no tools but StructuredOutput, no MCP servers, skills or slash commands, no plugins beyond Claude Code's own `@builtin` ones, `apiKeySource: none`, no memory paths); a breach stops the run. A live test (`uv run pytest -m live`) also runs a canary against the user's global CLAUDE.md. **Known residue (2.1.285):** in `safe_mode`, Claude Code still attaches the logged-in account's email address as a context block. It carries no instructions. A fresh config directory that reuses the login leaked the same block plus an Agent SDK prompt prefix, so `safe_mode` stays the default; `config_dir` with a setup token is the likely fix and is untested.
    - **Limits:** the 5-hour and weekly subscription limits are shared with interactive Claude Code. `PlanGuard` (`adapters/plan_guard.py`) reads the `rate_limit_event`s in each call's stream-json output and stops the run before the next call on status `rejected` or a status it doesn't know, any overage or usage-credit indicator (overage in use or available, a usage-credit fallback), a window at its threshold, or a call with no usage signal. Each window has its own threshold: `stop_at_weekly_utilization` (0.85) for every window whose `unifiedWindows` key or `rateLimitType` starts with `seven_day` (the model-specific weekly windows included), `stop_at_utilization` (0.80) for the 5-hour window and any other; the stop reason names the window and its threshold, and the reset is that window's. Status `allowed_warning` is advisory: it is recorded (`warning`) and never stops a run by itself. A stop for a plan window alone (a threshold, `rejected`, a limit error with a reset time) can be waited out with `--wait-for-reset`; every other stop is permanent. Usage credits are never enabled. It also enforces a per-run cap on live calls, and after every call rewrites `data/llm_status.json` (`updated_at`, `calls`, `cap`, `rate_limit_status`, `warning`, `utilization` (the highest window's), `window_utilizations` (window to utilization), `resets_at`, `overage`, `stopped`, `waiting_until`). Network and login failures stop the run without retries; other failures are retried with backoff. The job runner (`learn/jobs.py`) stops cleanly and reports the reset time; jobs are idempotent and every answer is cached, so a re-run resumes. `--wait-for-reset` never waits longer than `[llm.claude_code] max_wait_s` (6 hours): a reset further away ends the run (exit 3), with the reason in the report. Heavy runs are best scheduled for idle hours.
    - **Cost:** the cost metric uses list-price token cost (the reported `total_cost_usd`) whatever the billing, so the gate's cost limit stays meaningful on a flat-rate plan. `honed usage` reports calls, tokens and this shadow cost per stage, PR and run.
  - `anthropic` (API key; `adapters/anthropic_llm.py`): for scaling beyond subscription limits, for Batches API pricing, and for any use on behalf of other people, where subscription credentials may not be used.
    - **Credentials:** API-key only. The key comes from `ANTHROPIC_API_KEY` and is passed to the official SDK explicitly, so the SDK never falls back to an OAuth token or a profile; without a key, or with a subscription OAuth token in its place, the backend refuses to start (`honed` exits 2).
    - **A call:** the system prompt is one block with a `cache_control` breakpoint at its end; when the call names a shared prefix of its user content (`LLMCall.cache_prefix`: the finder panel's change, guidance and intent, `review/finders.py` `shared_prefix`), a second breakpoint ends that prefix, so every panel member after the first reads it from the prompt cache. A JSON schema becomes `output_config.format` (never a prefill; keywords structured outputs reject are dropped, `api_schema`), the effort `output_config.effort`, and thinking stays at the model's default (adaptive). Calls stream (large `max_tokens`), with server-side refusal fallbacks (beta `server-side-fallback-2026-07-01`, `fallbacks: "default"`, `[llm.anthropic] fallbacks`); a refusal every fallback also refused fails the call. The SDK retries rate limits, overloads, server and connection errors; what is left maps to the port's errors: a refused key, a missing model or a billing problem (`BackendUnavailable`) and a rate limit (`UsageLimitReached`) stop the run, the network stops it too, a bad request or a server error fails the call.
    - **Cost:** from each response's usage (per iteration when a fallback model served it, at that model's prices) and `[llm.anthropic.prices]` (list prices per million tokens; an unlisted model is charged the table's highest). The cost metric's shadow cost is the real cost here, and the call cache and usage ledger work as on every backend.
    - **Budget** (`adapters/budget_guard.py`): `[llm.anthropic] run_budget_usd` is a ceiling. Before a live call the guard reserves its worst case (its input, all written to the cache, plus `max_tokens` of output) and refuses with `BudgetExhausted` when the cost so far plus every reservation in flight plus this one would pass the budget; the reservation is settled at the real cost. It also enforces the run's call cap. The heartbeat (`data/llm_status.json`) holds `calls`, `cap`, `spent_usd`, `reserved_usd`, `budget_usd`, `stopped` and the batch in flight. There is no plan window, so `--wait-for-reset` does nothing here.
    - **Message batches** (`[llm.anthropic] use_batches`, used by `label`, `audit-judge` and `eval`): the job runner runs its jobs in passes (`learn/jobs.py` `run_jobs` with a `CallBatch`). In a pass, a call that misses the call cache is queued and its job deferred (`CallDeferred`, never a failure); the queue goes out as one Message Batch at half price, keyed by the call's cache-key digest as `custom_id`, is polled until it has ended, and every answer and per-item failure is kept for the next pass, in which the deferred jobs run again and find their answers. A job that makes calls in turn takes one pass per step (a replayed review: intent, panel, verifier, then the judge). A batch is admitted as the longest prefix of its requests whose worst cases fit in the budget and the call cap; the rest wait for the next batch. The Batches API takes no fallbacks, so a refused item is asked again on its own, with them. An interrupt cancels the batch in flight (its finished items are still billed), and answers already received live only in memory until their jobs run again, so a crash between a batch and the next pass loses them. `improve` keeps its calls synchronous.
  - `local` *(built for MLX-LM on Apple Silicon: `adapters/local_llm.py`; llama.cpp or Ollama elsewhere planned)*: the `[llm.local]` model (section 9) in-process, loaded once, one call at a time; the `local` extra (`uv sync --extra local`, then `honed fetch-local-model` into `[paths] models`). Its reasoning mode is off. Structured output by prompting: the system prompt gets the JSON schema and an example of the answer's shape, the answer is parsed and validated (`adapters/json_answer.py`), and an invalid one is sent back with its errors up to `parse_retries` times; first-try and final parse failures are counted. Usage comes from the tokenizer, shadow cost is 0, sample 0 decodes greedily and a sample k > 0 is seeded by k. Answers are recorded and cached as model `local:<repo id>`, never under a Claude model's name (`adapters/routing_llm.py` `RoutedLLM` rewrites the model before the cache).
  - `replay` (answers only from cache and errors on a miss; used by tests and deterministic re-scoring).
- **Every model call is cached** in SQLite (`adapters/call_store.py`), keyed by (model, system-prompt hash, input hash, schema hash) plus the effort level and a sample index (repeat samples for consistency audits). Offline runs reuse cached Fable gold labels, never Fable's verdicts on findings (section 6, blinding).
  - The local model matches and validates every finding, on both sides of a comparison; the cross-family audit calibrates it against Fable's cached matches.
  - Offline promotions are *provisional* until an online audit re-scores them with Fable.
- **Dataset:** one SQLite file plus a content-addressed `blobs/` directory (store schema 6: a PR's source, corpus or benchmark, and its fixed split; later rounds' diffs; benchmark records). The portable form is a **bundle** (`ports/bundle.py`, `adapters/bundle_file.py`, `learn/bundle.py`; what it contains, its terms and the privacy and removal process for people: `DATASET.md`): one gzip- or Zstandard-compressed JSON-lines file, a manifest first (bundle schema 2, store schema, record counts, the source repos with their licenses from the code host, the benchmarks with theirs, the creation date, and the release fields: `[dataset] version`, the judge (model, effort, each `yardstick/prompts` file's SHA-256, the evaluation fingerprint), the license split (annotations `[dataset] annotations_license`, CC BY 4.0; comment text not ours; benchmarks theirs; the code Apache-2.0), `comment_text` full or stripped, the redaction rules' version and hit counts, the removal counts), then the records: PR metadata, review threads (text, author login, URL), mechanical outcomes, judgments and judged labels, gold sets and round gold, escaped defects, the splits, the decision log, policy versions, benchmark records, and the judge's cached answers (the cache rows of `[models.judge] online`, so re-scoring is reproducible). It holds **no code**: every patch and diff hunk is removed (each PR record lists which files had a patch), no context pack is exported, and later review rounds keep only their diff's file list and merge base.
  - **Export rules** (in order, per PR): a PR stored as `test-private` is left out, with everything about it and the judge's cached answers the usage ledger ties to it, unless `--include-private` (the manifest's `test_private` and `test_private_prs` say which; the export report counts them, never names them). Then the removal list (`[paths] removals_file`, committed `yardstick/removals.json`, `core/removals.py`) drops a listed PR with every record about it, and a listed comment's whole thread with its outcome, judgments, judged labels and the gold issues built from it; judge cache rows the usage ledger ties to an affected PR go too; other PRs keep their splits; a guard on the writer refuses any record still carrying a listed PR or comment. Then `learn/bundle_text.py` runs the secrets and personal-data scan (`core/redaction.py`: API keys and tokens, private-key blocks, JWTs, `api_key = ...` assignments, non-noreply email addresses, phone numbers, globally routable IPv4 addresses outside the documentation ranges) over PR titles, descriptions and commit headlines, review comments, judgment reasons, gold and defect descriptions and the cached answers (benchmark answer keys stay verbatim); each hit becomes `[redacted:<kind>]`. Each review comment and PR description gets an `Attribution` in its PR record: node id, author login, URL (the comment's permalink), `text_sha256` (of the text after redaction, so a hash can't confirm a guessed secret), the redacted kinds and `stripped`. `<name>.report.json` beside the bundle lists every redaction (record, field, kind, offset; never the value) and every removal.
  - `honed bundle export FILE` writes one (`.jsonl.gz`, or `.jsonl.zst` on Python 3.14+); `--strip-comments` leaves the comment and description text out and keeps the attributions. `honed bundle import FILE` merges one and is idempotent: a PR already in the store is kept as it is (it has its code), labels and gold are replaced by key, decision-log rows and cached answers already present are skipped, and every PR's split is fixed as the bundle's maker had it, so contributors share one validation split. Text that doesn't hash to its attribution is reported. A bundle with a newer schema is refused; schema 1 bundles import.
  - A stripped bundle's text is marked missing on its PR (`comment:<id>:<sha256>`, `body:<sha256>`, beside the patch marks; `core/marks.py`), and the PR is not reviewed or replayed until `honed rehydrate --comments` (`learn/rehydrate_text.py`) refetches it through the `TextSource` port (GitHub GraphQL: `nodes(ids:)` 100 comments per query, aliased `pullRequest` fields for descriptions; objects GitHub no longer has come back as missing), redacts it with the same rules and keeps it only when the hash matches. Edited or deleted text keeps its mark and is reported; `--accept-changed` stores it anyway. On data-dev all 814 texts came back matching, and the store equaled one imported from the full bundle. Re-exporting a store whose text is still missing keeps it stripped under its hash.
  - `honed export-gold --format martian` (`learn/gold_export.py`) writes the corpus gold as Martian Code Review Bench golden-comment files (mapping in `core/benchmarks.py`, `DATASET.md`), with the removal list and redaction applied.
  - An imported PR is marked as missing its patches (`Store.stripped`) and is neither reviewed nor replayed until `honed rehydrate [--repo R]` (`learn/rehydrate.py`) restores them from git (`git diff -M` between the same commits, hunks one line apart merged as GitHub does: on data-dev 711 of 729 reviewed-diff patches came back byte-identical, the rest differ only in hunk boundaries; locally diffed ones from the file contents, as the harvester made them), then builds the context pack and a round pack for each later round the bundle carried a diff for. Rehydrated packs can differ from the originals where a patch's hunks differ (symbols are read from patches), so a replayed review can miss the reviewer's cache; the judge's gold labels don't depend on patches.
- **Code snapshots (context packs):** online, the harvester reads code from a bare partial clone of each repo. Clones are kept by default (`[harvest] keep_clones`). Offline runs, and runs on another machine, can't fetch files, so the harvester also saves a context pack for each PR, into `blobs/`:
  - the changed files at base and head;
  - the callers and imports of changed symbols;
  - the tests near the change;
  - all taken at the reviewed commit.

  Offline reviews read only from the pack. A read outside it is logged as a miss and served when back online. The pack hit rate is reported (`METRICS.md` section 7), and packs are widened when it falls.
- **Dependencies:** locked with uv. `uv sync --offline` installs from a local wheel cache. A Docker image is optional.
- **One setting switches modes:** `offline = true` in `honed.toml` swaps every adapter to its local or replay implementation. *(Built for the LLM stack: every stage, the proposer included, goes to the local model; so do the judge's match and validity verdicts, for both sides of every comparison, with the local model's own earlier answers replayed from the cache (`ReplayFirstLLM` under the local model's name) and Fable's cached verdicts never mixed in; the judge's gold labels (per-round gold issues, the dataset both sides share) come from Fable's cache first, then the local model; runs are stored under backend `local`, separate from `claude_code` runs and incumbents; promotions are marked provisional in the decision log and the policy version.)*

## 9. Models per stage

These are defaults. Stages marked tunable can be changed by the improve loop, within an allowed list.

| Stage | Online default | Offline | Tunable |
|---|---|---|---|
| Intent | `claude-sonnet-5-5` | local | yes |
| Finders | `claude-sonnet-5-5` | local | yes |
| Verifier | `claude-opus-5-5` | local | yes |
| Proposer | `claude-opus-5-5` | local | no |
| Judge / labeler | `claude-fable-5-1`, effort medium | local (both sides of a comparison; gold labels from Fable's cache) | no (fixed yardstick) |

"local" is `[llm.local] model`: **Qwen3.8-27B, 4-bit MLX** (`mlx-community/Qwen3.8-27B-4bit`, 16 GB of weights; Alibaba's Qwen family, not Claude's), chosen 2026-09-30 (section 13). The judge is fixed online; offline the local model judges both sides of every comparison, and Fable's cached labels supply only the gold sets.

The Anthropic adapter handles the `refusal` stop reason, using server-side fallbacks. Token usage is recorded for each stage, for the cost metric.

**Cost levers for training:**
- On the `anthropic` backend, eval replays and judge labeling aren't latency-sensitive, so they run through the Message Batches API at half price. The `claude_code` backend has no batch pricing, but Claude Code caches prompts automatically.
- The finder panel shares one cached context prefix across its members.
- Most improve-loop iterations can run offline on the local model, with Fable audits confirming promotions (section 8).

## 10. Code layout and layer rules *(layers built and enforced)*

```
honed.toml               central config and feature flags (the only config file)
policy/                  the mutable, versioned reviewer policy (section 3)
yardstick/prompts/       judge prompts: fixed, outside policy/, forbidden to the promote step (section 7)
yardstick/human_labels/  the blind audit sample (no code) and people's labels-<username>.json (read by `audit-judge`)
yardstick/removals.json  PRs and comments every dataset export leaves out (DATASET.md, Removal)
src/honed/
  core/                  pure domain: types, outcomes, matching, scoring, policy hashing, agreement and human-label consensus. No I/O.
  ports/                 interfaces (typing.Protocol): LLM (+ CallBatch, PlanWindow), CallStore (call cache + usage ledger), CodeHost (+ TextSource), CodeReader (read/grep code at a commit; + Differ), Store (+ LabelStore, EvalStore, ImproveStore), Judge, Reviewer, JobRunner, PolicyFiles and PolicyDirectory, BundleWriter and BundleReader, BenchmarkSource
  review/                review pipeline: context, finders, verifier, rank, render
  learn/                 harvest, label, evaluate, crossfamily, propose (+ prompts/), policy_edit, lessons, feed, selfreview, gate, promote, improve, bundle (+ bundle_text), rehydrate (+ rehydrate_text), gold_export, benchmarks
  adapters/              claude_code_llm (+ plan_guard), anthropic_llm (+ budget_guard), local_llm (+ json_answer), routing_llm, cached_llm, replay_llm, call_store, github, git_reader (online; also diffs commits for rehydrate), pack_reader (offline), sqlite_store, bundle_file, human_labels (the yardstick's sample and labels files), label_server (the labeling page on localhost), removals_file, policy_dir, bench_martian, bench_aacr
  config.py              loads honed.toml into typed settings
  cli/                   composition root: the only place adapters are wired into services (wiring.py builds them;
                         corpus.py, reviews.py, defects.py, audits.py, human_labels.py and improve.py hold the commands)
tests/                   unit tests (core), contract tests (each adapter against its port), replay e2e; fixtures/replay/ is the CI's fixture PR
tools/human-labels/      the labeling page for the blind sample (`honed human-labels serve`); self-contained HTML
scripts/                 replay_fixture.py (CI: the fixture PR must report its planted bug; --record re-records it through the fake claude), full_corpus_run.sh
research/                one-off studies (repo selection)
data/                    gitignored: prreview.sqlite (the store, under its pre-rename file name), blobs/, cache/
```

Import rules are enforced in CI by `import-linter`:
- `core` imports nothing else from `honed`.
- `ports` → `core`.
- `review`, `learn` → `core`, `ports`; never `adapters`, and never each other: the evaluation reaches the review pipeline through the `Reviewer` port, and the panel reaches the job runner through the `JobRunner` port.
- `adapters` → `core`, `ports`.
- Only `cli` and `config` import everything.

**Core types:** `PullRequest`, `Thread`, `Outcome`, `Finding`, `GoldIssue`, `Match`, `EvalResult`, `Lesson`, `PolicyVersion`.

**CLI:**
- Global: `--config PATH`, `--data-dir PATH` (a runtime override: every `[paths]` entry under `data` moves under PATH, for a run on a copy of the data).
- Built: `harvest [--repo R …] [--limit N]`, `pack [--repo R …] [--limit N] [--rebuild] [--rounds N] [--split S …]`, `stats`, `label [--repo R …] [--limit N] [--max-calls N] [--rebuild-gold]`, `audit-judge [--max-calls N] [--cross-family [--split S] [--run ID …] [--n N] [--match-rounds N]]`, `export-audit-sample [--n N] [--out PATH] [--ids-from SAMPLE] [--force]`, `human-labels serve [--port N] [--no-browser]`, `human-labels check [FILE …]`, `fetch-local-model [--model REPO]`, `usage [--run ID | --last]`, `review <owner/name#N | diff-file> [--round K] [--policy DIR] [--json|--markdown] [--prior FILE --dismissed IDS]`, `eval [--policy DIR] [--split S] [--rounds N] [--sample K] [--set-incumbent] [--sensitivity [--samples K]]`, `decisions`, `mine-defects [--repo R …] [--limit N]`, `improve [--rounds N] [--min-attempts M] [--split S] [--review-rounds N] [--policy DIR] [--ignore-sensitivity] [--repo R …] [--limit N]` (an interrupt records the candidate in flight as `incomplete`, finishes the round's bookkeeping and exits 130), `bundle export FILE [--no-licenses] [--strip-comments] [--report PATH]`, `bundle import FILE`, `rehydrate [--repo R …] [--limit N] [--rebuild] [--comments [--accept-changed]]`, `export-gold --format martian [--split S] [--repo R …] [--out DIR]`, `import-benchmark martian|aacr [--dir PATH] [--fetch] [--limit N]`; the model-calling commands take `--max-calls N` and `--wait-for-reset`. Output is line-buffered, so a log file or pipe gets progress as it happens.
- Planned: `review … --post`. (`improve` caps live calls with `--max-calls`; on the `anthropic` backend the run's dollar budget applies too.)

## 11. Training data

Chosen from 138 repos ranked by measured review rigor (`research/repo-selection/`, 2026-09-29). The list moves into `honed.toml` `[corpus]` when that file exists.

**Human-review corpus.** PRs are sampled per language in proportion to the metric weights (`METRICS.md`), spread evenly across the repos in each group. Start with about 1,000 reviewed PRs and grow once the labeling cost is measured.
- **Targeted sampling for bugs** *(built for `CHANGES_REQUESTED` reviews; bug-like wording planned)*: at least `[harvest] bug_targeted_share` (30%) of each repo's quota comes from landed PRs where a human submitted a `CHANGES_REQUESTED` review, read from the search listing (GitHub's `review:changes_requested` qualifier matches only the current review decision, so it finds almost no merged PRs), with a cursor per repo and slice; a slice that runs out hands the shortfall to the general sample, and PRs store `sampled_as` for the evaluation. The rest is sampled as before, so the mix stays realistic.
- **The quota counts only PRs with at least one human inline review thread**, since those are the PRs that produce gold issues.
- **Approval-only PRs** (a human reviewed without an inline thread) are collected separately, up to 25% on top of the quota (`[harvest] approval_only_share`), in the `approval_only` corpus. They are the clean-PR set for the false-alarm check (`METRICS.md` section 3). Phase 1 counted them in the quota; phase 2 split them out, and moved the 17 stored ones without refetching.

| Group (weight) | Repos (rigor rank of 138) |
|---|---|
| TypeScript/JS (0.45) | elastic/kibana (29), WordPress/gutenberg (19), facebook/lexical (17), open-telemetry/opentelemetry-js (24), typescript-eslint/typescript-eslint (36), cloudflare/workers-sdk (37), microsoft/fluentui (40), microsoft/rushstack (28) |
| C/C++ (0.25) | scylladb/scylladb (2), open-telemetry/opentelemetry-cpp (3), ceph/ceph (6), zephyrproject-rtos/zephyr (11), apache/arrow (18), carbon-language/carbon-lang (20), NVIDIA/cccl (27) |
| Python (0.20) | scikit-learn/scikit-learn (9), apache/airflow (13), scipy/scipy (21), django/django (26), zulip/zulip (30) |
| Other (0.10) | grpc/grpc-go (1), kubernetes/kubernetes (8), openjdk/jdk (12), dotnet/runtime (15), rust-lang/rust (25) |

The TypeScript repos rank lower than the others because rigorous TypeScript review is rarer on GitHub. They are still the best available, and the language weights give TypeScript its priority.

**AI-feedback corpus.** Harvest only AI-bot threads and what happened to them. This labels whether an AI finding was acted on. Repos: elastic/kibana, cloudflare/workers-sdk, prisma/orm, backstage/backstage, bitwarden/clients, microsoft/onnxruntime, NVIDIA/cccl, systemd/systemd, scylladb/scylladb, dotnet/runtime, vitessio/vitess.

Several of these resolve about 100% of AI threads. For them, labels come from changed lines and replies, not from resolution.

- **Excluded from training:**
  - The held-out benchmark repos. For the Martian bench: Sentry, Grafana, Cal.com, Discourse, Keycloak, and the `ai-code-review-evaluation/*` copies its replicated PRs live in. For AACR-Bench, the 50 source repos listed in its `dataset/positive_samples.json`. These include bitcoin/bitcoin, valkey-io/valkey, n8n-io/n8n, microsoft/typescript-go, ClickHouse/ClickHouse, electron/electron, nodejs/node, vllm-project/vllm, sveltejs/svelte and astral-sh/uv.
  - redis/redis, because Valkey is a fork of it, so its code nearly duplicates a held-out repo.
  - Repos whose review happens off GitHub: Gerrit or mailing lists, Reviewable (cockroachdb), and Meta/Google-internal review (react-native, rocksdb, protobuf). These are listed in `research/repo-selection/rank_repos.py` `OFF_GITHUB_REVIEW`.
- **Per-repo quirks the harvester must handle.** The filters in `research/repo-selection/measure_repos.py` already encode them; reuse them.
  - OpenJDK lands by closing the PR (`label:integrated`).
  - Kubernetes approvals are `/lgtm` comments.
  - Backports and release-branch copies.
  - Automation accounts that GitHub types as users.
  - AI review posted as `github-actions` (kibana, systemd).
  - Frequent force-pushes (scylladb, ceph, kubernetes), where "fixed" must come from compare patches, not GitHub's `isOutdated`.
- **Once live:** the user's own repos become the highest-weight source.

**Held-out benchmarks** *(built: `honed import-benchmark martian|aacr [--fetch]`; `core/benchmarks.py`, `adapters/bench_martian.py`, `adapters/bench_aacr.py`, `learn/benchmarks.py`)*. Each benchmark PR is fetched from the code host (metadata, commits, reviews, and the diff the benchmark reviewed: AACR-Bench pins it, Martian reviews the PR's head) and stored with `source = "benchmark"`, the fixed split `test`, no review threads of its own, and the benchmark's golden comments as its gold set (provenance `benchmark`, `[metrics.gold_conf] benchmark` 1.0); the benchmark's record (golden and, for AACR-Bench, rejected comments) is kept beside it. Severities: Martian's Critical, High and Medium are Important, Low a Nit; AACR-Bench has none, so a code defect or a security vulnerability is Important and a performance or maintainability remark a Nit. Categories map to `[label] categories`. Martian names no file or line (the judge matches on the description); AACR-Bench names lines on the new side, as we do, and an old-side comment becomes file-level. A benchmark PR is never labeled, never moved to the clean-PR set, never in the dev split, and its repo is on `[corpus.exclude]`, so nothing harvests, packs (except `rehydrate`) or mines it. Imported into data-dev on 2026-10-01: Martian 50 PRs, 173 gold issues; AACR-Bench 189 of 200 PRs, 1,419 gold issues, 604 rejected comments kept, 4 PRs with no golden comment; the other 11 review commits that are no longer on GitHub (force-pushed away) and are skipped.

## 12. Design inputs

- **Anthropic Code Review** (code.claude.com/docs/en/code-review): finder → verifier → rank; the severity scheme; `REVIEW.md`; reactions as feedback.
- **review-kit** by Alex Owen (@aowen14, https://github.com/aowen14/review-kit): the review-thread outcome taxonomy, lessons that cite their evidence, rules read from the base branch, the fixture-PR regression check. No license: ideas only, no code or text copied (`NOTICE`, `THIRD_PARTY_NOTICES.md`).
- **Martian Code Review Bench** (MIT): golden comments, judge matching, F-beta. Its online set labels whether developers acted on bot comments.
- **AACR-Bench** (Apache-2.0): expert-verified multi-language test set.
- **pstack** by Lauren Tan (poteto), `cursor/plugins/pstack` (MIT). Adopted:
  - intent first, and multi-model panels with consensus;
  - the review rubric and code-quality lens, and lead-judgment filtering and buckets;
  - evidence levels, and "find the one fact it's safe because of";
  - Bugbot-triage lesson format, and never suppressing high-risk findings;
  - encoding lessons in structure, and the lesson acceptance criteria;
  - hill-climb discipline, eval blinding, and the writing standard for comments.

  Its content is adapted into the seed policy with attribution. Every learning is mapped to where it lands in `docs/design-inputs/pstack.md`.

## 13. Open decisions

- How much design and code-quality findings should count. Anthropic Code Review puts correctness first and skips style; pstack is demanding about structure. The default is design findings as Nits unless they meet pstack's approval bar, and the eval decides from real outcomes. You may want your own repos to weight design higher.
- Whether to add non-Anthropic model families (for example GPT or Grok, as pstack does) to the finder panel for diversity. By default, the online panel is Claude only, and diversity comes from a local open model of a different family.
- ~~The local model.~~ Decided 2026-09-30: **Qwen3.8-27B at 4-bit** (`mlx-community/Qwen3.8-27B-4bit`, 16 GB of weights, dense, hybrid linear attention so the KV cache stays small; vendor-reported LiveCodeBench v6 90.3, SWE-bench Pro 61.7, IFBench 79.5; Apache-2.0). The alternatives: `mlx-community/Qwen3.6-35B-A3B-4bit` (20.4 GB, mixture of experts with 3B active: much faster, SWE-bench Verified 73.4, the pick if offline loops need speed) and `mlx-community/gemma-4-26B-A4B-it-4bit` (15.3 GB, Google: a third family, LiveCodeBench v6 77.1). Measured on Apple Silicon (M4 Pro, 48 GB unified memory): load 4–7 s, peak memory 18 GB, prefill about 125 tokens/s, decode about 15 tokens/s.
- How live review runs: a GitHub Action (a thin wrapper around `honed review --post`) or a local daemon.
