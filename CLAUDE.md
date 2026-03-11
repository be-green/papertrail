# Papertrail

MCP server for managing an academic paper library.

## Architecture
- MCP server at src/papertrail/server.py using FastMCP
- JSON files per paper are the source of truth (synced via rclone to ~/.papertrail)
- SQLite FTS5 index is ephemeral, rebuilt on startup from JSON (database.py)
- PaperStore handles JSON file I/O on local filesystem (paper_store.py)
- rclone sync pulls remote on startup, pushes after writes (sync.py)
- Semantic Scholar + arXiv + SSRN for paper discovery (metadata.py)
- pymupdf4llm for PDF-to-markdown conversion (converter.py)

## Data Layout
```
~/.papertrail/                  # local dir, synced to/from rclone remote
  tags.json                     # Global tag vocabulary
  papers/{bibtex_key}/
    metadata.json               # Source of truth for all paper data
    paper.pdf
    paper.md
    summary.json                # Convenience copy
    citation.bib                # Publisher BibTeX entry (via DOI content negotiation)

~/.cache/papertrail/
  index.db                      # Ephemeral SQLite FTS5 index
```

## Setup
1. `uv sync` to install dependencies
2. `cp .mcp.json.example .mcp.json` and edit paths for your machine
3. Configure rclone remote and set `PAPERTRAIL_RCLONE_REMOTE` in `.mcp.json`

## Development
- Run tests: `uv run pytest`
- Run server directly: `uv run papertrail`
- Install deps: `uv sync`
- Install dev deps: `uv sync --extra dev`

## Key Conventions
- All async code uses httpx for HTTP requests
- BibTeX keys follow format: lastname_year_firstword (e.g., smith_2024_causal)
- Paper files stored at ~/.papertrail/papers/{bibtex_key}/
- Write pattern: write JSON (source of truth) first, then update SQLite index, then push to remote
- All database methods are async (wrapped via asyncio.to_thread)
- When reading or analyzing multiple papers, use Task subagents to read each paper in parallel (one subagent per paper) rather than reading them sequentially in the main context. This keeps large paper texts out of the main context window and runs reads concurrently.
