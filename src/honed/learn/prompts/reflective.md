## Your task: one prompt or settings edit

Read the failure traces below and the judge's reasons for them, and find a mechanism in the reviewer's prompts or settings that produces them. Propose one `prompt_replace` or one `config_set` that changes that mechanism.

What you can change:
- a prompt passage: a lens that produces false positives, a missing instruction behind missed issues, the lead reviewer's filters, buckets or evidence rules;
- the model and effort of a stage, within the allowed models listed below (the judge and the proposer are fixed);
- the finder panel: its members' models, efforts and lenses, the composition, the language lens;
- the context budget (files, lines, callers, earlier discussion);
- thresholds: confidence threshold, nit cap, the evidence level Important needs, the categories capped at Nit (never a high-risk one), findings per member.

Mind the gate's cost and latency limits: a stronger model or more effort must pay for itself in score.
