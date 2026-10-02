<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): agents/comment-sicko.md and skills/no-comments/SKILL.md. MIT License; see THIRD_PARTY_NOTICES.md. -->
## Comments and suppressions

- A new lint or type-checker suppression (`@ts-ignore`, `@ts-expect-error`, `eslint-disable`, `NOLINT`, `# type: ignore`, `# noqa`): if the rule catches real bugs, the suppression hides one; name the change that makes the rule pass.
- A comment justifying a workaround ("hack", "temporary", "do not remove"): name the rename, extraction or type that makes the behavior obvious without it.
- Commented-out code; comments narrating the next line; a comment that now contradicts its code.

Leave alone license headers, doc comments defining a public API, comments on behavior forced by an external dependency or protocol, and issue links explaining a constraint. These findings are `nit`s unless the suppression hides a real bug.
