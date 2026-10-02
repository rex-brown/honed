<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/unslop/SKILL.md. MIT License; see THIRD_PARTY_NOTICES.md. -->
## Writing standard for review comments

- Name the mechanism and the fix: what goes wrong, on which input, at which line, and what to change. Cut sentences that aren't a concrete fact, instruction or number.
- Active voice: "`parse()` drops the last field", not "the last field is dropped".
- No hedging: "`x` is null when the list is empty", not "this could potentially be null". If unsure, say what you checked and what you couldn't confirm.
- No filler ("in order to", "note that", "basically", "simply", "just"), praise or chatbot phrases ("Great work", "I hope this helps").
- Plain words: "use", not "leverage" or "utilize"; no "crucial", "delve", "robust", "seamless", "pivotal", "comprehensive". No em dashes.
- Short sentences, identifiers in backticks. A title is one sentence with no trailing period; a body is at most five sentences.
