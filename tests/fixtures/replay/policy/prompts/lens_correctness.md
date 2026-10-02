<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/interrogate/references/rubric.md (Correctness). MIT License; see THIRD_PARTY_NOTICES.md. -->
## Correctness

Does the code do what the intent says, on every path?

- Edge cases: empty inputs, null or undefined, boundaries, zero and negatives.
- Errors caught, propagated or swallowed; a failure path that leaves state inconsistent.
- Off-by-one, type coercion, integer overflow, string encoding, units.
- Stale values and closures, dangling references, a cache never invalidated.
- Idempotency: what if it runs twice, or a previous run crashed halfway? If that depends on what was left behind, a reconciliation step is missing.
- Concurrency: is shared mutable state (files, rows, memory) serialized by structure (locks, phases, exclusive ownership) or only by a convention that won't hold?
- Contracts: a changed signature, return value, default or thrown error that callers rely on.

Trace a suspected bug: show the call chain that produces the bad value, and check the callers shown before claiming one passes it.
