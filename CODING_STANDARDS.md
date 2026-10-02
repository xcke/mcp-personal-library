# Coding standards

Short on purpose. Where a rule and common sense disagree, say why in the PR.

## Design
- **Match the module map** in the spec (#1): config, db, extractors, indexer, watcher, query, render, server, cli. Put code where a reader would look for it; one module changes for one reason.
- **Extractors are pure**: a file in, units and outline out. No database, no logging side effects beyond warnings.
- **Domain concepts get types.** Use a dataclass, enum or `Literal` instead of a dict with string keys or a magic string (document kind, index outcome, status).
- **No hidden global state.** Nothing mutated at import time; process-wide side effects (logging config) happen once, at startup.
- **Don't build for hypothetical needs.** No unused parameters, hooks or abstractions. Three similar lines beat a premature helper.

## Code
- **Names say what a thing is.** No single-letter names except short loop indices and comprehension variables. Avoid `data`, `info`, `tmp`, `helper`.
- **Functions do one job** and fit on a screen (~40 lines). Split when you need a comment to separate phases.
- **Type-hint** every function signature. Prefer explicit return types over `dict`/`tuple` grab-bags.
- **Don't repeat logic.** If the same check or computation appears twice, name it once.
- **Comments explain why**, never what. No commented-out code.

## Errors and input
- **Validate at boundaries only** (CLI args, environment, MCP tool inputs, file contents). Trust internal callers.
- **Tool and CLI errors are actionable**: say what was wrong and what to try next.
- **Catch narrowly.** A broad `except Exception` is allowed only where one bad input must not stop a batch (per-file indexing), and it must record the reason.
- **Bad input data never crashes indexing**: malformed files degrade gracefully (skip the broken part, keep the rest).

## Security
- The token is never logged, printed (other than the startup URL) or put in an error message.
- SQL is always parameterized; only constants are interpolated into SQL text.
- Compare secrets in constant time.

## Tests
- Test behaviour at the agreed seams only (HTTP MCP endpoint, CLI subprocess). Don't test private helpers or query the database directly.
- Test-first for new behaviour. Expected values come from the spec or a literal, never recomputed the way the code does.
- Fixtures are generated at test setup; no committed binaries.
- Run the single test file while working and the full suite before committing.

## Process
- One logical change per commit, referencing the issue (`(#N)`).
- New dependencies need a reason in the commit message.
