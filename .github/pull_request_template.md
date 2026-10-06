## What kind of change is this?

- [ ] Code (adapters, pipeline, tooling, docs)
- [ ] Policy candidate (a change under `policy/`)
- [ ] Human labels (`yardstick/human_labels/labels-<username>.json`)
- [ ] Yardstick or metrics (maintainer decision; open an issue first)

## Summary

<!-- One or two sentences on what changes and why. -->

## For a policy candidate

- **Hypothesis** (names a mechanism, for example "the design lens flags patterns consistent with the codebase"):
- **The one change** (one change per PR; never stacked):
- **Evidence:** the eval report (`honed eval --split validation --rounds 2`) and its decision-log row, or at least the screen result. Paste the gate verdict below.
- **Backend and judge:** (for example `claude_code` with `claude-fable-5-1`, or `local`; local results are provisional)

```
<gate verdict>
```

## Checklist

- [ ] `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` and `uv run lint-imports` pass
- [ ] No changes to `yardstick/`, `METRICS.md` or thresholds unless a maintainer agreed in an issue
- [ ] No secrets, personal data or repository code from the corpus in the diff
- [ ] Human labels were made blind (no looking up threads or the dataset first)
