<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/interrogate/references/rubric.md (Root Causes vs. Symptoms). MIT License; see THIRD_PARTY_NOTICES.md. -->
## Root cause or symptom

Does the code fix the actual problem or paper over a symptom? Read the callers and types shown first.

- A guard clause masking a broken invariant ("if (!x) return" where x is missing only because of an upstream bug).
- Retry logic hiding a broken contract; a cast silencing a modeling error.
- A fix in module A that belongs in module B's contract.
- A "don't do X" comment or a convention to remember, where a type, lint rule or runtime check could make the wrong thing impossible.

For a workaround, name why it is needed and what the proper fix is.
