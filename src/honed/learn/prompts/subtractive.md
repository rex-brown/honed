## Your task: remove something

Propose one deletion: remove a lesson (`lesson_remove`), or trim a prompt passage (`prompt_replace` with a shorter `new`, or an empty one). A longer policy dilutes the rules that matter, and removing a lesson passes the gate if it loses no score beyond the noise floor and the lesson fired often enough for that to mean something (on at least {{removal_min_exposure}} evaluated reviews). A trimmed prompt passage has to raise the score.

Good targets: a lesson that fires often without precision, a passage that repeats another, an instruction the diagnostics show has no effect, text the model already follows without being told. Don't remove the untrusted-input instructions, the safety rules, or a rule a failure case shows the reviewer needs.
