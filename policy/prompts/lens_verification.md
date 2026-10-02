<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/interrogate/references/rubric.md (Verification) and skills/principle-test-behavior-not-implementation/SKILL.md. MIT License; see THIRD_PARTY_NOTICES.md. -->
## Verification

Would the tests catch a regression?

- Tests for the new behavior that check behavior, not implementation details. A bug fix comes with a test that fails without it.
- A test that would pass if every import returned `undefined` can't fail: no or only weak assertions (`toBeDefined`, `toBeTruthy`, `not.toThrow`, `assert x is not None`), only mock-call assertions, an expected value computed by the code under test, an assertion restating a constant, a fixture asserting itself.
- A changed integration boundary with only its halves tested.
- Code checking a proxy (a modification time, a cached flag, a self-reported status) instead of the real thing.

Report a missing or ineffective test only when the change adds or alters behavior nothing checks.
