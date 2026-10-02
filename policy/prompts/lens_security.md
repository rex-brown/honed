<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/interrogate/references/rubric.md (Security) and skills/poteto-mode/references/bugbot-triage.md (ask by default). MIT License; see THIRD_PARTY_NOTICES.md. -->
## Security and data safety

Trace every finding here through the code: where the value enters, each step, and the sink it reaches.

- User input reaching a dangerous sink (SQL, shell, dynamic code, HTML, file paths, URL fetches, deserialization) unvalidated or unescaped.
- Authentication or authorization gaps in new endpoints, handlers or tools; a check that runs after the side effect it guards.
- Secrets or personal data in code, logs, errors or telemetry.
- Time-of-check to time-of-use gaps on security-relevant state.
- Data kept longer, sent further or shown to more people than before; billing, quotas and permission boundaries.
- Migrations and schema changes that lose data, lock tables, or break readers on the old code.

Report one whenever you can trace a plausible path.
