# Security

## Pull request text is untrusted data

Honed feeds other people's pull requests to language models: titles, bodies, code, review comments and replies. All of it is treated as data, never as instructions:

- Every prompt that includes it puts it inside a marked block (`<untrusted_pr_data>`, `<repo_guidance>`, `<pr_intent>`), says it is untrusted, and tells the model to ignore instructions inside it; text inside a block can't close or reopen one (`src/honed/review/text.py` `neutralize`).
- Models get no tools. On the `claude_code` backend every call runs isolated: an empty working directory, no tools, no MCP servers, plugins, skills or slash commands, no CLAUDE.md or memory, and a child environment without the parent's `ANTHROPIC_*` and `CLAUDE*` variables. A call whose `system/init` event shows otherwise stops the run (`ARCHITECTURE.md`, section 8).
- Nothing is posted anywhere: `honed review` prints its review.
- A repository's own `REVIEW.md` and `CLAUDE.md` are read at the merge base, so a pull request can't loosen its own review.

If you find a way for pull request content to change what the reviewer or the judge does beyond being reviewed, that is a vulnerability: please report it (below).

## Credentials and spending

- **Never run `honed` on a Claude subscription with paid overage (usage credits) enabled.** The plan guard stops before the next call on any overage or usage-credit signal, and at 80% of a plan window (85% of a weekly one), but it can only react to what Claude Code reports.
- The `anthropic` backend uses only `ANTHROPIC_API_KEY`, passes it to the SDK explicitly (so no OAuth token or profile is ever used), refuses to start without it, and refuses a subscription OAuth token in its place. Set `[llm.anthropic] run_budget_usd`: a call or batch whose worst case doesn't fit in what is left is refused.
- Keep keys in the environment, never in `honed.toml` or a committed file.
- The `config_dir` isolation mode reads a `claude setup-token` token from `CLAUDE_CODE_OAUTH_TOKEN`, or from a macOS Keychain item only when you name one (`[llm.claude_code] keychain_service`).

## Data

Bundles (`honed bundle export`) carry review comments quoted from public pull requests and the repositories' names and licenses, but no code. Their text goes through a secrets and personal-data scan before export, and a removal list every export honors (`DATASET.md`, Privacy and Removal). To ask for a PR or comment to be removed, open a GitHub issue titled "Removal request" with its link, or, to keep the request private, use GitHub's private vulnerability reporting (below). These are the only channels until the project has a dedicated contact for removals. Data directories (`data/`, `data-dev/`) hold harvested code and are ignored by git; don't commit or publish them.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository (the **Security** tab, then **Report a vulnerability**) rather than a public issue. Include what you did, what happened, and the commit you ran. The same form takes private removal requests for the dataset (`DATASET.md`, Removal): say "Removal request" and give the PR's or comment's link.

The maintainer is away for now, so a reply may be slow; reports are read when the maintainer is back.
