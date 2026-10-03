# Keep the trigram index for grep

Status: accepted (2026-10-03)

## Context

SPEC.md asked for a scale check before committing to the trigram FTS index behind `grep`. Real `~1000-book` access was not available, so we measured a 200-document sample of the owner's networking library (Cisco Press books, Cisco Live decks, CCDE/CCIE material, Routing Bits, Fortinet/F5): 1.4 GB of PDFs, 76,461 pages, 137 MB of extracted text. Machine: 12-core Apple silicon Mac, local SSD, server queried over the HTTP MCP endpoint (median of 5 runs).

## Measurements

| Item | Result |
|---|---|
| Index size (total) | 603 MB |
| Trigram FTS (`units_tri`) | 349 MB (2.5x text, 58% of the index) |
| Stored text (`units`) | 198 MB |
| Porter FTS (`units_fts`) | 50 MB |
| First index (11 workers) | 79 s |
| Restart, nothing changed | 0.5 s |
| `search`, whole library | 5-12 ms |
| `search`, filtered by `path_glob` | 3 ms |
| `grep` literal, whole library / filtered | 3-9 ms |
| `grep` regex with a literal part | 50-185 ms |
| `grep` regex with no literal (IP/CIDR pattern) | 1.2 s |
| Full streaming scan of all text (the alternative), literal | 0.7-1.1 s |

Extrapolating linearly to ~1000 books (about 5x this sample): index around 3 GB (trigram about 1.7 GB), first index around 7 minutes, literal `grep` via trigram still in the tens of milliseconds, streaming scan about 4-5 s per query.

## Decision

Keep the trigram index. It costs disk (cheap) and buys literal `grep` roughly 100x faster than scanning (6 ms vs ~1 s here, ~5 s at 1000 books), which matters because agents call `grep` repeatedly. Pure-regex queries with no literal part (1.2 s) do not benefit from it and remain the slow case.

## Consequences

- Revisit only if a real library pushes the index past available disk, or if first-index time becomes a problem; the fallback in SPEC.md (scan porter-FTS candidates, or a `path_glob`-restricted streaming scan) is still available.
- Follow-up tickets were filed for relevance issues found during the check (see GitHub issues).
