<!-- Adapted from pstack by Lauren Tan (https://github.com/cursor/plugins/tree/main/pstack, commit 69cf06fa253b): skills/typescript-best-practices/SKILL.md, skills/typescript-best-practices/references/patterns.md and skills/principle-type-system-discipline/SKILL.md. MIT License; see THIRD_PARTY_NOTICES.md. -->
## TypeScript and JavaScript

- External data (HTTP and RPC payloads, `JSON.parse`, `postMessage`, IPC, files, environment, database rows) is `unknown`, not `any`: parse it before use.
- No unvalidated `as` on data that crossed a boundary; `as unknown as T` is always a lie. A type guard must verify what its name claims.
- Variants as discriminated unions with a literal `kind`, not optional-field bags where contradictory states compile.
- A `switch` over a union needs a `never` check in the default arm.
- `satisfies` over `as` for literals; derived types (`z.infer`, `Pick`) over hand-written duplicates.
- Parse once at the boundary into a named domain type; don't pass `Record<string, unknown>` past it.
- A non-null `!`, `arr[0] as T` or a "should never happen" throw marks a type too weak at that spot.
- Async: a promise neither awaited nor returned, an `async` callback where a sync one is expected (`forEach`, event handlers), a missing `await` in `try`, state read after an `await` without a re-check.
- React: a conditional hook, a stale closure from missing dependencies, state copied from props without a key, an effect missing cleanup.
- `console.log` in shipped code where the project has a logger.
