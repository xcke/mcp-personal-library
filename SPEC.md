# plib — v1 spec

A folder-based personal library. Run it in a folder of PDFs and Markdown files and it serves the content over MCP (Streamable HTTP) so agents can search it the way they'd use `rg`, `sed -n`, `ls`, and a page screenshot.

## Decisions

| Topic | Decision |
|---|---|
| Formats | PDF, Markdown (`.md`, `.markdown`) |
| OCR | None. Pages with little text are flagged so the agent can render them. |
| Scale | 50–1000 books (roughly 20k–500k pages) |
| PDF library | PyMuPDF (AGPL accepted) |
| Storage | SQLite (WAL) + FTS5, in `<root>/.plib/` |
| Transport | MCP Streamable HTTP only |
| Auth | Single secret in the URI path, from the `PLIB_TOKEN` env var |
| Freshness | Live file watching + reconciliation at startup |
| Stack | Python 3.12+, `mcp` SDK (FastMCP), Starlette/uvicorn, PyMuPDF, watchdog, `uv` |

## CLI

```
PLIB_TOKEN=... plib serve [ROOT=.] [--host 127.0.0.1] [--port 8765] [--workers N]
plib index [ROOT=.]          # one-shot index without serving (CI / first run)
plib status [ROOT=.]         # counts, pending jobs, failed files
```

`serve` starts answering immediately and indexes in the background. Results from documents that aren't indexed yet are simply missing; `index_status` reports progress.

## Auth

- Endpoint: `http://{host}:{port}/{PLIB_TOKEN}/mcp`. Rendered images and any other routes also live under `/{PLIB_TOKEN}/`.
- Refuse to start if `PLIB_TOKEN` is unset or shorter than 32 characters. Print a suggested value (`secrets.token_urlsafe(32)`).
- Token comparison uses `hmac.compare_digest`. Any path that doesn't match returns `404` with an empty body.
- The uvicorn access log is disabled. Our own request logging redacts the token segment.
- Bind to `127.0.0.1` by default. The README states that remote use requires TLS in front (Caddy or Tailscale).

## Data model

```sql
documents(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE,            -- relative to root, POSIX separators
  kind TEXT,                   -- 'pdf' | 'md'
  size INTEGER, mtime REAL, sha256 TEXT,
  title TEXT, author TEXT,
  unit_count INTEGER,
  meta_json TEXT,              -- PDF metadata / markdown front-matter
  status TEXT,                 -- 'ok' | 'error' | 'encrypted'
  error TEXT,
  indexed_at REAL
)

units(                         -- PDF page or Markdown section
  id INTEGER PRIMARY KEY,
  doc_id INTEGER REFERENCES documents ON DELETE CASCADE,
  unit_no INTEGER,             -- 1-based physical page / section ordinal
  label TEXT,                  -- PDF page label ("xii", "12"); md: heading text
  heading_path TEXT,           -- md: "Install > Linux"; pdf: nearest outline entry
  line_start INTEGER, line_end INTEGER,   -- md only
  text TEXT,
  char_count INTEGER,
  image_count INTEGER,         -- pdf only
  low_text INTEGER,            -- 1 if char_count is small and the page has images/drawings
  UNIQUE(doc_id, unit_no)
)

outline(doc_id, ord, level, title, unit_no)   -- PDF bookmarks / md heading tree

units_fts  USING fts5(text, content='units', content_rowid='id', tokenize='porter unicode61')
units_tri  USING fts5(text, content='units', content_rowid='id', tokenize='trigram')
```

Size estimate at the top end (500k pages, ~1.5 GB text): porter FTS is roughly the size of the text, trigram roughly 3x. **Milestone check:** measure on a real 1000-book sample. If the trigram index is too big, drop it and have `grep` regex-scan the candidates from the porter FTS, or do a streaming scan restricted by `path_glob`.

## Units

**PDF:** one unit per physical page. Text comes from `page.get_text("text", sort=True)`. Page labels come from `doc.get_page_labels()`. Each page's `heading_path` is the deepest outline entry starting at or before it.

**Markdown:** split on ATX headings (`#`–`######`) outside fenced code blocks. Sections longer than ~8k chars are split at paragraph boundaries into `Heading (2/3)` and so on. Front-matter goes into `meta_json`. A file without headings becomes one unit (or several if it's long). Title is the front-matter `title`, then the first H1, then the filename.

## MCP tools

Every hit includes a **locator**, `{doc, unit, label}`, that can be passed straight to the next call. Tools take `doc` as a relative path or document id. They take `pages` either as physical numbers (`"10-20,35"`) or as PDF labels (prefix `label:`, e.g. `label:xii-xv`).

| Tool | Purpose | Key params | Returns |
|---|---|---|---|
| `list_documents` | `ls` | `glob?`, `kind?`, `limit`, `offset` | path, kind, title, units, status |
| `doc_info` | `stat` | `doc` | metadata, unit count, label map summary, low-text pages |
| `get_outline` | TOC | `doc`, `max_level?` | tree of `{title, level, unit}` |
| `search` | ranked lexical search | `query` (FTS5 syntax), `path_glob?`, `doc?`, `pages?`, `limit=20` | bm25-ranked hits with `snippet()` |
| `grep` | `rg -C` | `pattern`, `regex=false`, `ignore_case=true`, `context=200` chars, `path_glob?`, `doc?`, `pages?`, `max_hits=50` | match lines with context, plus hit counts per doc |
| `read` | `sed -n` | `doc`, `from`, `to?`, `max_chars=20000`, `cursor?` | unit texts with headers; `next_cursor` when truncated |
| `render_page` | screenshot (PDF only) | `doc`, `page`, `dpi=110`, `clip?` (x0,y0,x1,y1 in points or fractions) | MCP `ImageContent` (PNG) and the page size |
| `index_status` | progress | — | docs total/indexed/pending/failed, current file |

Output rules:
- Every tool has a hard character budget. When it truncates, it says what was cut and how to narrow the query (`"312 more hits in 41 docs; add path_glob= or doc="`).
- Units flagged `low_text` are marked `[low text — consider render_page]` in `read`/`search` output.
- `render_page` caps the pixel area (for example ≤ 2000×2000) and suggests `clip` for detail. PNGs are cached in `.plib/cache/{doc_id}/{page}@{dpi}[_{clip}].png` and invalidated when the document changes.

## Indexing

- **Reconcile at startup:** walk the root (skip `.plib/`, dotfiles and dot-dirs, and whatever `.plibignore` lists, using gitignore syntax). Compare `(size, mtime)`, and on a mismatch compare `sha256` before reindexing. Delete rows for files that no longer exist.
- **Watching:** watchdog observer with a ~2 s debounce per path. Before indexing, wait until size and mtime are stable across two checks, so copies in progress aren't read. Renames update `documents.path` when the sha256 matches.
- **Workers:** extraction runs in a `ProcessPoolExecutor` (default `cpu_count - 1`). A single writer thread commits one document per transaction, replacing all of its units, outline entries and FTS rows atomically. Readers use separate connections (WAL).
- **Failures:** encrypted or corrupt files get `status='error'/'encrypted'` and are retried only when they change.
- **Rough throughput:** PyMuPDF text extraction is about 100–500 pages/s per core, so the first index of 500k pages takes tens of minutes on a laptop. Restarts after that are near-instant.

## Layout

```
pyproject.toml
src/plib/
  cli.py          # serve / index / status
  config.py       # root, token, paths
  db.py           # schema, migrations, connections
  extract/pdf.py  # PyMuPDF → units, outline, labels
  extract/md.py   # markdown → sections
  indexer.py      # reconcile, job queue, writer thread
  watcher.py      # watchdog + debounce
  query.py        # search, grep, read, page-range parsing
  render.py       # PNG rendering + cache
  server.py       # FastMCP tools, Starlette mount, token guard
tests/
  fixtures/       # small PDFs (labels, outline, low-text page), md files
```

## Milestones

1. Schema, PDF/MD extraction, and `plib index` + `status`.
2. Query layer (`search`, `grep`, `read`, outline, page ranges) with tests.
3. MCP server over HTTP with the token guard; `render_page`.
4. Watcher and background indexing while serving.
5. Scale check on a real library: index size, grep latency, then the trigram decision.

## Out of scope for v1

OCR, embeddings/semantic search, formats other than PDF and Markdown, multiple tokens or scopes, table extraction (`find_tables`), and write operations.
