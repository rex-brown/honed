# Honed: Metrics

These are the exact numbers the improve loop optimizes and the gates it must pass. The design is in `ARCHITECTURE.md`. The code in `src/honed/core/scoring.py` must match this file.

Metric settings live in `honed.toml` under `[metrics]`, `[gate]`, `[prod]` and `[judge]`. They sit outside `policy/`, so **the improve loop cannot change them**. Only a human edits this file or those settings.

## Overview

| Tier | Question | Metric | Used for |
|---|---|---|---|
| Headline | Is the reviewer better? | Score **S**: language-weighted, severity-weighted F0.5 | Choosing between policies |
| Gate | Is it better without breaking anything? | ΔS confidence interval, per-language and Important recall floors, cost, latency, policy size, self-review | Accepting or rejecting a candidate |
| Production | Is it better on real PRs? | Acted-on rate, 👎 rate, dismissal rate, missed-issue rate | Rollback; promoting lessons |
| Yardstick | Can we trust the scores? | Agreement between the judge and human outcomes, and between the offline matcher and the judge | Deciding whether judge labels count |
| Diagnostics | Where is it failing? | Per category, finder, lesson and severity | The proposer's input; reports |

## 1. Scoring one PR

For each evaluated PR, the reviewer produces findings F, and the gold set G holds the real issues. The judge matches findings to gold issues one-to-one, requiring the same location (overlapping lines or the same symbol) and the same underlying problem. Each finding then falls into one class:

| Class | Meaning | Counts toward |
|---|---|---|
| TP | Matched a gold issue | Precision and recall |
| VU (valid, unlabeled) | No gold match, but the judge rules it a real issue | Credit `c_vu` only when the **judge independently rates it Important** and the verifier's evidence level is ≥ 3. A finding claimed Important that the judge rates a Nit is scored as an FP at Nit weight (severity inflation). A valid unlabeled Nit is neutral, up to `max_neutral_vu_per_round` per PR-round; beyond that it counts as an FP |
| FP | No gold match, and the judge rules it wrong or not worth reporting | Precision (as a cost) |
| DUP | Repeats an already-matched issue or another finding | Precision (as a cost, same as FP) |

**Weights.**
- Severity: `w(Important) = 3`, `w(Pre-existing) = 1`, `w(Nit) = 0.5`.
- Each gold issue also has a provenance confidence `conf`, set when labeling (section 8).
- A true positive earns its **gold** issue's weight. A false positive costs the **claimed** severity's weight, so claiming Important falsely costs the most.

```
TPw = Σ_{f∈TP} w(gold(f))·conf(gold(f))
VUw = c_vu · Σ_{f∈VU, claimed Important, judged Important, evidence ≥ 3} w(Important)
FPw = Σ_{f∈FP∪DUP} w(claimed(f)) + Σ_{f∈VU Nits beyond the per-round cap} w(claimed(f))
      + Σ_{f∈VU, claimed Important, judged Nit} w(Nit)
Gw  = Σ_{g∈G} w(g)·conf(g)
```

"Judged" is the judge's own severity: the validity question (`yardstick/prompts/validity_set.md`) asks it to rate each valid unmatched finding Important or Nit, and it never sees the severity the reviewer claimed. The evidence level is the verifier's (ARCHITECTURE.md section 4), and its threshold is `metrics.vu_min_evidence`.

**Why VU credit is limited to Important** (phase 3 baseline): 66 of 77 posted findings were valid-but-unlabeled and only 2 were false positives. Any credit for such findings raises precision, so a loop optimizing S would learn to post more plausible nits, which is exactly the noise humans dislike. A real bug the humans missed still earns credit; extra valid nits are tolerated up to the cap and penalized past it. The claimed severity is the reviewer's own choice, so credit also needs the judge's independent Important rating and a traced path: otherwise the loop could earn credit by calling valid nits Important.

## 2. The headline score, S

Sums are pooled (micro-averaged) over all PRs in one language group L, where L is TypeScript, C/C++, Python or other:

```
P_L  = (ΣTPw + ΣVUw) / (ΣTPw + ΣVUw + ΣFPw)      precision
R_L  = ΣTPw / ΣGw                                 recall
F_L  = (1+β²)·P_L·R_L / (β²·P_L + R_L)            β = 0.5, which weights precision more than recall
S    = Σ_L π_L · F_L                              π = TS 0.45, C/C++ 0.25, Python 0.20, other 0.10
```

The language weights π are **personal**: each user sets their own in `honed.toml` (the defaults above follow the original owner's mix). S is for reporting and for ranking candidates in a user's own runs. **The canonical gate (section 3) never uses π**, so the shared reviewer encodes no one's priorities between languages (owner decision, 2026-10-07).

**Edge cases** (decided in phase 1; `core/scoring.py` implements them):
- Precision with no findings is 1, and recall with no gold issues is 1. F is 0 when both P and R are 0.
- S renormalizes π over the language groups present in the evaluated PRs.
- A second true positive on an already-matched gold issue counts as a duplicate.
- Recall on Important issues (`R_imp`) is pooled over all PRs.
- A valid unlabeled Pre-existing finding is neutral and doesn't count toward the Nit cap (the formulas in section 1 leave it out of both VUw and FPw).
- A valid unlabeled finding claimed Important that the judge rates Important but whose evidence level is below `vu_min_evidence` is neutral and outside the Nit cap: real, but nothing traced it. So is one from a run judged before the judge rated severity (`Match.judged_severity` empty); such runs are re-judged rather than compared (ARCHITECTURE.md section 6, storage).
- A severity-inflated finding (claimed Important, judged Nit) costs w(Nit) whatever its evidence level, and doesn't use up the round's neutral Nits.
- The Nit cap takes a PR-round's valid unlabeled Nits in the order the review posted them; the ones after the first `max_neutral_vu_per_round` are the ones beyond it. They all weigh the same, so the order only decides which findings a diagnostic subset holds.

**Worked example.** One PR has two gold issues: one Important (weight 3) and one Nit (0.5). The reviewer posts three findings:
- one matches the Important issue: TP, 3;
- one is a valid Nit the humans didn't flag: VU, neutral (the first of the round, within the cap);
- one is a false Important: FP, 3.

That gives P = 3 / 6 = 0.50, R = 3 / 3.5 = 0.86, and F0.5 ≈ 0.55. The false alarm erased as much precision as the catch earned, and the valid nit neither helped nor hurt. That is the intended behavior.

## 3. The gate: all must hold on the validation split

**Screening (a cost lever, not a rule):** a candidate is first replayed on a fixed, stratified `screen_fraction` of the validation split. Only candidates whose screen ΔS is above 0 go on to the full split and the rules below; the rest are recorded as `screened_out`. The screen subset is the same for every candidate in a round, and the full-split result is the only one the gate uses.

**Precondition:** the eval has passed its sensitivity check for the current models and dataset. It must separate the incumbent from a deliberately weakened policy by more than the noise floor (`ARCHITECTURE.md` section 6).

1. **Real gain (weight-free):** at least one language L has ΔF_L = F_L(candidate) − F_L(incumbent) ≥ `min_gain_L` **and** a 95% paired-bootstrap confidence interval for ΔF_L whose lower bound is above 0. `min_gain_L` comes from that language's noise floor (section 6); until a per-language floor has been measured, the pooled `min_gain` applies. Combined with rule 2, this accepts only changes that help some language and hurt none, whatever weights a user prefers. A run restricted to one language (`--language L`) is judged on that language alone, plus rule 2 for the others when the change touches anything shared. Section 6 gives the protocol.
   - **Exception for pure removals:** a candidate that only deletes policy content passes this check if, for every language, the confidence interval lower bound of ΔF_L is ≥ −`min_gain_L` **and the removed content was exercised** on the gate split: a removed lesson or check fired, or a removed prompt passage was cited, on at least `removal_min_exposure` PR-rounds. Otherwise the removal is `unmeasured`, not harmless, and is not promoted. (The phase 4a trial removed a check that had never fired on its tiny split.)
2. **No language regresses:** for each L with at least 30 PRs, F_L(candidate) ≥ F_L(incumbent) − 0.02.
3. **Bug-catching holds:** recall on Important gold issues (`R_imp`) ≥ incumbent − 0.02.
4. **Clean PRs stay quiet:** the share of clean PRs (no gold issues) that receive at least one Important finding doesn't rise by more than 2 percentage points.
5. **Cost** (list-price token cost on every backend, including the flat-rate subscription):
   - mean cost per review ≤ `cost_cap_usd`;
   - and ≤ 1.10 × the incumbent's, unless ΔS ≥ 2 × `min_gain`.
6. **Latency:** p90 review time ≤ `latency_p90_max_s`.
7. **Policy size:**
   - at most `policy_max_lessons` active and probation lessons;
   - prompt tokens ≤ `policy_max_prompt_tokens`.

   A longer policy dilutes the rules that matter.
8. **Well-formed output:** at least 99% of findings parse, and at least 99% of Important and Nit findings anchor to a changed line. Pre-existing findings sit outside the diff by design and are excluded from the anchor check.
9. **Self-review:** the incumbent reviewing the candidate's `policy/` diff reports zero Important findings.

The tolerances above stay as they are (owner's decision, 2026-10-02), even though nearly every candidate is expected to be rejected on noise at the current split sizes: a rejection on noise is the gate working, not a reason to loosen it.

Mechanical conventions (`learn/gate.py`):
- Both runs must score the same PR-rounds: a candidate that lacks a PR-round the incumbent scored fails rule 1.
- Rule 2 counts distinct PRs, not PR-rounds, against `min_prs_per_language`.
- Rule 8 is two rates, each at least `well_formed_min`. The parse rate is proposals that parsed over proposals that parsed plus malformed ones (`ReviewResult.proposed`: the panel's parsed proposals before dedup, plus the checks' findings). The anchor rate is posted Important and Nit findings whose lines come within 3 lines of a hunk of a changed file, over all posted Important and Nit findings. Each is 1.0 with nothing to count.
- A pure removal (rule 1's exception): no file added, every changed prompt or lessons file only loses characters, and `config.toml` only loses whole lines (a smaller value is a change, not a removal).
- Exposure (rule 1's exception): every lesson the removal deletes or shortens must have fired on at least `removal_min_exposure` of the compared PR-rounds in the incumbent's run. A lesson fires on a PR-round when any finding of that review, posted or not, cites it, or its check raised the finding. Prompt passages and `config.toml` lines carry no citation, so a removal that touches them is always `unmeasured` (it can still pass by real gain). A candidate whose only failed rule is an unmeasured rule 1 is recorded `unmeasured`, not `rejected`, and may be proposed again once the split grows.
- Screening: the subset is `screen_fraction` of each language's PRs in the gate split (at least one, rounded), in an order fixed by the incumbent's hash and the round number, so it is the same for every candidate in a round and drawn afresh the next. The candidate's screen run is stored under the split's name plus `/screen`; the incumbent's numbers come from its stored full run on the same PR-rounds. A screen with a PR-round the incumbent scored but the candidate didn't is `screened_out` too. Self-review runs before the screen (it costs a few calls; the screen costs a fifth of an evaluation).
- The precondition and `min_gain` come from the newest sensitivity rows of the decision log; `min_gain` is never below `min_gain_floor`.

## 4. Production metrics (live reviews, per policy version)

Every posted finding carries its policy hash, so each version's production record is separate.

| Metric | Definition | Offline analogue |
|---|---|---|
| Acted-on rate | Severity-weighted share of posted findings whose outcome is `fixed` **and** the judge confirms the change addressed the finding (phase 2 found 38% of `fixed` threads weren't) | Precision |
| 👎 rate | Share of posted findings with a 👎 reaction | FP rate |
| Dismissal rate | Share of findings resolved without a code change | FP rate |
| Missed-issue rate | Human review threads with a strong outcome that no finding overlaps, per PR | 1 − recall |
| Escaped defects *(diagnostic)* | Reviewed PRs whose changed lines are rewritten by a bug-fix PR within 30 days | Recall |
| Cost and latency per PR | Measured from API usage | Same |

**Rollback.** Compare the new policy's first 100 posted findings (or 21 days, whichever comes first) with the previous policy's last 100. Roll back automatically when either of these holds, with a one-sided two-proportion test at p < 0.05:
- the acted-on rate drops by ≥ 10 percentage points;
- the 👎 rate rises by ≥ 5 percentage points.

**Lesson lifecycle.** Confidence levels follow pstack's Bugbot-triage format. Each finding records which lessons it cited, so every lesson has its own production record.
- **Candidate → recurring:** at least 5 production firings, with an acted-on rate ≥ the policy's average and a 👎 rate ≤ the policy's average.
- **Recurring → strong:** at least 20 firings across at least 2 repos, meeting the same bar.
- **Retired:** either of these holds:
  - at least 10 firings with an acted-on rate below half the policy's average;
  - no firings in 90 days (a stale rule).
- **Suppression limits:** a lesson that suppresses findings may never apply to `[safety] high_risk_categories`. Human dismissals in those categories never count against a lesson.

## 5. Trusting the yardstick

Judge labels count only while these audits pass. Each is re-run whenever the dataset version changes.

| Audit | Definition | Threshold |
|---|---|---|
| Judge vs humans | Judge validity verdicts against judge-independent human answers. Valid: applied ```suggestion blocks. Invalid: a human 👎 on any reviewer finding (human or AI-bot threads), and AI-bot findings resolved without a change whose reply disagrees. Both classes are required; without negatives κ is 0 by construction, as phase 2 showed. A 50-item human-labeled set sharpens this: contributors label a fixed blind sample (`yardstick/human_labels/`, one `labels-<username>.json` each), and an item's human answer is the majority of at least 2 labelers' valid or not-valid verdicts ("unsure" excluded, ties dropped). Items with a single labeler are judged but reported separately, flagged, and not counted. Reported next to it: inter-annotator agreement (Fleiss' κ with 3 or more labelers, else Cohen's κ) and the judge's accuracy and Cohen's κ against the majority. | Accuracy ≥ 0.85 and Cohen's κ ≥ 0.6 |
| Judge consistency | The same item judged 3 times | ≥ 90% agreement |
| Offline matcher vs judge | Match decisions of the local matcher against Fable's cached matches | F1 ≥ 0.90 |
| Cross-family agreement | A local judge from a different model family re-scores a sample of Fable's verdicts, as a check on self-preference | Report κ each round. The alarm (κ below 0.5) counts only once the local judge itself agrees with the human labels at accuracy ≥ 0.75; until then low κ says the local judge is weak, not that Fable is biased (phase 4a: the local judge ruled 58 of 60 findings valid, κ 0.07) |
| Blinding | The judge never sees which policy or model produced a finding; replayed PRs carry no eval cues | Must hold; checked by a test |

If a judge audit fails, judge-only labels are dropped: VU findings get zero credit and judge-only gold issues are removed until the audit is fixed. If the matcher audit fails, offline promotions are *provisional* until an online run re-scores them with Fable.

### Human audits (tracked; proposed 2026-10-07)

Every dataset version gets a set of human audits. Each audit is a blind, stratified sample (by language, repository and comment provenance), labeled by at least 2 people who did not author or review the PRs in it. Each is recorded in the audit ledger (`AUDITS.md` plus `yardstick/audits/<audit-id>/`: the sample, one label file per labeler, and the computed results), so coverage and results can be tracked over time.

| Audit | Question asked of the humans | Sample per dataset version | Threshold | If it fails |
|---|---|---|---|---|
| Judge validity | Is this review comment a valid issue? (the table above) | 50 | Accuracy ≥ 0.85, κ ≥ 0.6 | Judge-only labels dropped (above) |
| Gold precision | Is this gold issue a real issue in the code shown? | 60 | ≥ 0.90 real | Lower `gold_conf` for the failing provenance; block the release if below 0.80 |
| Gold severity | Is it Important, a Nit, or Pre-existing? | the same 60 | Agreement with the judge's severity ≥ 0.80; Important precision ≥ 0.85 | The Important-only metrics are reported as provisional |
| Addressed check | Did the later change address the comment? | 50 `fixed` threads | Accuracy ≥ 0.85 | `fixed` positives fall back to `changed_unaddressed` for the failing class |
| Provenance | Does this comment read as written by a person, assisted by AI, or fully automated? | 50, stratified by predicted class | Reported with the classifier's error rates on known-provenance comments (bot accounts; pre-2023 comments) | Comments of uncertain provenance are excluded from human-provenance gold |

Labelers report their agreement with each other too (Fleiss' or Cohen's κ). An audit with κ below 0.4 between humans is inconclusive: the question or the guidance needs work before the result counts. A dataset version is released only when every audit has reached its sample size and either passed or had its failure handled as the table says.

## 6. Statistical protocol

- **Paired bootstrap:** resample validation PRs with replacement, stratified by language; 1,000 resamples; S is recomputed for both policies on each resample.
- **Noise floor:** evaluate the incumbent twice (or K times) with fresh model samples and measure the spread σ of ΔS between two identical policies: the bootstrap standard deviation of ΔS for each pair of samples, pooled as the root mean square over the pairs. Set `min_gain = max(0.01, 2σ)`, and re-measure whenever the models or the dataset change.
- **Overfitting alarm:** the test split is scored every round but never used for decisions. The loop pauses when either of these holds:
  - validation S rose while test S fell across 3 consecutive promotions;
  - cumulative test ΔS is below −0.01 while cumulative validation ΔS is above +0.03.

  The fix is to refresh validation with newly harvested PRs; the old validation PRs move to train.
- **External check:** each round also reports the Martian Code Review Bench score, using its own "Core" profile, and the AACR-Bench score. These are comparable to published results for other tools.

## 7. Diagnostics (reported every round, never gated)

Each metric is broken down by:
- category (bug, security, memory/undefined behavior, types/contracts, concurrency, error handling, tests, performance);
- severity;
- language.

Also reported:
- the severity confusion matrix (claimed vs gold);
- location accuracy (exact line vs same hunk);
- findings and nits per PR;
- verifier rejection rate;
- **verifier false-dismissal rate:** dismissed candidates that match a gold issue;
- consensus rate, and precision for consensus findings vs single-member findings;
- the evidence-level distribution, and precision at each level;
- "act on" findings per PR (more than 5 flags weak filtering);
- comment-lint violations (target 0);
- context-pack hit rate: the share of file reads during review that the PR's saved pack answered;
- precision per finder;
- precision and firing rate per lesson;
- cost per stage.

The proposer reads these to decide what to change.

## 8. Defaults (`honed.toml`)

| Key | Default |
|---|---|
| `metrics.beta` | 0.5 |
| `metrics.severity_weights` | Important 3, Pre-existing 1, Nit 0.5 |
| `metrics.valid_unlabeled_credit` (`c_vu`) | 0.5, findings claimed and judged Important at evidence level `vu_min_evidence` or higher only |
| `metrics.vu_min_evidence` | 3 |
| `metrics.max_neutral_vu_per_round` | 3 |
| `gate.removal_min_exposure` | 5 PR-rounds |
| `gate.screen_fraction` | 0.2 of the validation split (section 3, screening) |
| `judge.local_min_human_accuracy` | 0.75 |
| `metrics.language_weights` | TS 0.45, C/C++ 0.25, Python 0.20, other 0.10 (personal: reporting and ranking only, never the canonical gate) |
| `metrics.gold_conf` | applied suggestion 1.0, human `fixed` (addressed) 1.0, human `fixed` (partially) 0.8, human `open_at_merge` 0.8, escaped defect 0.8, judge-only 0.6, benchmark golden comment 1.0 (test split only) |
| `gate.min_gain` | max(0.01, 2σ noise floor); `min_gain_L` per language once measured |
| `gate.language_tolerance`, `gate.important_recall_tolerance` | 0.02, 0.02 |
| `gate.min_prs_per_language` | 30 |
| `gate.clean_pr_alarm_rise_pp` | 2 |
| `gate.cost_cap_usd` | 0.80 per review (the phase 3 baseline measured $0.41 at list price) |
| `gate.cost_growth_max` | 1.10 |
| `gate.latency_p90_max_s` | 600 |
| `gate.policy_max_lessons`, `gate.policy_max_prompt_tokens` | 60, 6000 |
| `gate.bootstrap_resamples` | 1000 |
| `gate.well_formed_min` | 0.99 |
| `prod.rollback_window` | 100 findings or 21 days |
| `prod.rollback_acted_drop_pp`, `prod.rollback_thumbsdown_rise_pp` | 10, 5 |
| `judge.min_accuracy`, `judge.min_kappa`, `judge.min_consistency`, `judge.matcher_min_f1` | 0.85, 0.6, 0.90, 0.90 |
| `judge.cross_family_alarm_kappa` | 0.5 |
| `lessons.min_prs`, `lessons.min_authors` | 2, 2 |
| `lessons.recurring_min_fires`, `lessons.strong_min_fires`, `lessons.strong_min_repos` | 5, 20, 2 |
| `review.act_on_flag` | 5 |
| `safety.high_risk_categories` | security, privacy, auth, billing, data retention, migrations/schema, idempotency, concurrency |
