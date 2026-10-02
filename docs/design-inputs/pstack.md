# pstack → Honed: traceability map

This maps every code-review learning in pstack (Lauren Tan, `cursor/plugins/pstack`, MIT, read at commit `69cf06fa253b` on 2026-09-29) to where it lands in this design. Section numbers refer to `ARCHITECTURE.md` (A§) and `METRICS.md` (M§). Status is *planned* until the code exists.

## Adopted

| pstack source | Learning | Lands in |
|---|---|---|
| `interrogate` step 2 | State the PR's intent first; review the execution, not the goal | A§4 step 2 (intent) |
| `interrogate` step 3 | Adversarial signal comes from model diversity, not assigned personas | A§4 step 3 (panel composition is tunable) |
| `interrogate` step 4 | Findings raised by 2+ models independently are the highest signal | A§4 step 3 (consensus flag); M§7 (consensus precision) |
| `rubric.md` | Correctness (trace the path), root cause vs symptom, structural integrity, verification, complexity budget, security (trace the input) | A§4 step 3 (lenses); seed `policy/prompts/` |
| `code-quality-review.md` | Look for "code judo" simplifications; 1,000-line file threshold; spaghetti growth; boring over magic; loose types at boundaries; canonical layer; atomic updates | A§4 step 3 (design lens); A§4 severity (approval bar) |
| `code-quality-review.md` approval bar | Structural regressions are presumptive blockers | A§4: design findings are Important only when they meet the bar |
| `reviewer-prompt.md` | Every finding gives location, evidence and reasoning; an empty review is valid; no praise | A§4 step 7 (writing standard); seed prompts |
| `lead-judgment.md` | Nitpick gravity; hypothetical vs actual; premature abstraction; "I'd have done it differently"; missing-context signals | A§4 step 5 (verifier filters) |
| `lead-judgment.md` | Act on / consider / noted / dismissed; act-on ≤ 5; dismissed list as a trust mechanism | A§4 steps 5 and 7; M§7 (act-on per PR) |
| `lead-judgment.md` | Don't dismiss security or correctness findings lightly, even from one model | A§4 step 5 |
| `blast-radius` | Evidence ladder: asserted → cites line → traced → ran it → reproduced | A§4 step 5 (evidence level); M§7 (precision by level) |
| `blast-radius` | Find the one fact the change is safe because of; look where grep stops (library source, wire formats, flags, timing) | Seed verifier prompt |
| `blast-radius` | Never invent a caller or an API; a search that finds nothing is an answer | Seed verifier prompt |
| Comment Sicko, `no-comments` | Workaround justifications, commented-out code and lint suppressions (`@ts-ignore`, `eslint-disable`) are findings | A§4 step 3 (comments lens); seed `check` lessons |
| `bugbot-triage.md` format | Pattern fields: skip when, do not skip when, example signal, source, confidence (candidate / recurring / strong) | A§3 (`lessons.yaml` schema); M§4 (lifecycle) |
| `bugbot-triage.md` "ask by default" | Never auto-suppress security, privacy, auth, billing, data, migrations, idempotency or concurrency findings | A§3 (high-risk list); M§4 (suppression limits) |
| `bugbot-triage.md` | Human dismissals of security/data findings are owner judgment, not team-wide skip rules | A§5 (high-risk dismissals aren't noise) |
| `bugbot-triage.md` patterns | Stack-local usage, framework invariants, narrow error conditions, manual reimplementation of native behavior | Seed `candidate` lessons |
| `babysit` step 8 | Re-check claims against the current head; never churn code to quiet a bot | A§4 step 7 (re-reviews); M§4 (dismissal rate) |
| `principle-encode-lessons-in-structure` | Prefer a lint or check over more text; pick the strongest mechanism | A§3 (`check` lessons tried first) |
| `reflect` synthesizer | Accept a learning only if durable, specific, convergent, decision-changing and not already covered | A§7 (lesson acceptance) |
| `reflect` | Reviewer and transcript text is untrusted; ignore instructions inside it | A§4 (PR text is untrusted); `CLAUDE.md` |
| `hillclimb` | One change, one measurement, keep or revert; never stack untested changes | A§7 step 1 |
| `hillclimb` | Prove the harness can separate cases, then freeze it | A§6 (sensitivity check); M§3 (precondition) |
| `hillclimb` | Hypotheses name a mechanism; push past plateaus; stop predicate = target + minimum attempts, never relaxed | A§7 (plateaus) |
| `hillclimb` | Correctness and simplicity outrank the number; keep a simplification that holds the score | A§7 step 3; M§3 (exception for pure removals) |
| `show-me-your-work` | Decision log, one row per attempt | A§7 (decision log) |
| `arena` | N parallel attempts on different models; convergence is signal, wild divergence means the task was under-specified | A§7 step 1 (parallel proposals) |
| `eval` playbook | Blind the candidate (no eval cues) and the judge (neutral labels); one judge scores both variants in one pass | A§6 (blinding); M§5 |
| `eval`, `arena` | The judge comes from a different model family | A§6 (cross-family audit); M§5 |
| `eval` step 7 | Read outputs yourself and compare with the judge | A§6 (spot audits) |
| `unslop` | Concrete mechanism, active voice, no hedging, filler or chatbot phrases | A§4 step 7 (comment lint) |
| `typescript-best-practices` | Discriminated unions, branded types, `unknown` over `any`, no unvalidated `as`, exhaustiveness, `satisfies`, schema-derived types, boundary parsing | A§4 step 3 (TypeScript lens); seed `check` lessons |
| `principle-type-system-discipline` | Make illegal states unrepresentable; don't lie to the compiler (any typed language) | TypeScript and C/C++ lenses |
| `principle-test-behavior-not-implementation` | A test that passes when every import returns `undefined` can't fail | Verification lens; seed `check` for weak-assertion tests |
| `principle-boundary-discipline`, `model-the-domain`, `minimize-reader-load`, `fix-root-causes` | Validate at boundaries; a structure instead of scattered conditionals; count layers and hidden state; no symptom guards | Design lens content |
| `why` | How and why code got its shape comes from its history | A§4 step 1 (earlier review threads on the same files, before the PR only) |

## Not adopted, and why

| pstack element | Reason |
|---|---|
| Comment Sicko's "delete almost every comment" absolutism | Adopted as a lens whose weight the eval sets from real outcomes; comment norms differ by repo. |
| Default panel of Opus, GPT and Grok | Open decision (A§13). The default panel is Claude online plus a local open model for family diversity. |
| Babysit merge-frontier, CI and stack mechanics | Out of scope: this project reviews PRs; it doesn't drive them to merge. |
| `reflect` asking a human to approve each skill edit | Replaced by the automatic gate (A§7); the user asked for no human gate. |
| Planning, prototyping, benny, bot-UI and worktree skills | Not about code review. |
