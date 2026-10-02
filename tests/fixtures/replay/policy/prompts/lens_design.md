<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/interrogate/references/code-quality-review.md and skills/interrogate/references/rubric.md (Structural Integrity, Complexity Budget). MIT License; see THIRD_PARTY_NOTICES.md. -->
## Design and code quality

- Missed simplification: a restructuring that keeps the behavior and deletes whole branches, helpers or layers.
- File size: a file pushed from under 1,000 lines to over; extract helpers or modules.
- Spaghetti growth: ad-hoc conditionals or one-off branches in an unrelated shared flow; move the logic into a helper, state machine or module.
- Boring over magic: brittle or clever behavior, thin pass-through wrappers.
- Types and boundaries: needless optionality, loose `any`/`unknown`/`object` shapes, cast-heavy code, a silent fallback papering over an unclear invariant. Validate data once where it enters.
- The canonical layer: feature logic in shared code, implementation details leaking through an API, a helper duplicating an existing one.
- Non-atomic updates that can leave state half-applied.
- Complexity budget: parameters for cases that don't exist, dead code, compatibility paths kept after a migration.

Premature abstraction is worse than duplication.

Approval bar: a design finding is `important` only when the change pushes a file past 1,000 lines, tangles ad-hoc branching into a shared flow, scatters feature checks across shared code, or puts logic in the wrong layer when a clear home exists. Every other design finding is a `nit`.
