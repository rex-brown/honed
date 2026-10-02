<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/reflect (the acceptance criteria for a learning) and skills/principle-encode-lessons-in-structure. MIT License; see THIRD_PARTY_NOTICES.md. -->
## Your task: mine one lesson

Cluster the missed gold issues and the false positives below (and the valid nits beyond the per-review cap) into recurring patterns, and turn the strongest pattern into one lesson edit: `lesson_add`, `lesson_change` or `lesson_remove`.

A lesson is accepted only if it is:
- durable: no commit hashes, version numbers or file paths that drift (scope globs are fine);
- specific enough to recognize when it applies (a prompt lesson fills `applies_when`);
- backed by at least {{min_prs}} PRs from at least {{min_authors}} different PR authors among the failure cases, cited in its `evidence` as `owner/name#N` (only PRs shown here count, which also stops one person from poisoning the data);
- decision-changing: following it changes what the reviewer posts on those PRs;
- not already covered: when an existing lesson covers the pattern, strengthen it with `lesson_change` instead of adding a near-duplicate.

Encode the lesson in structure: try a `check` lesson first (a regex over added lines, a structural pattern or a threshold, run exactly and cheaply), and write a `prompt` lesson only for what needs judgment, saying in `why_not_check` why a check can't express it. A flag check must fire on the PRs it cites. A lesson that suppresses findings must never touch the high-risk categories. Remove a lesson when the diagnostics show it fires without precision or misleads the reviewer.
