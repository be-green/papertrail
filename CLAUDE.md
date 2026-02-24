# Papertrail

MCP server for managing an academic paper library.

## Architecture
- MCP server at src/papertrail/server.py using FastMCP
- SQLite with FTS5 for search (database.py)
- rclone for syncing to Wasabi (storage.py)
- Semantic Scholar + arXiv + SSRN for paper discovery (metadata.py)
- pymupdf4llm for PDF-to-markdown conversion (converter.py)

## Development
- Run tests: `uv run pytest`
- Run server directly: `uv run papertrail`
- Install deps: `uv sync`
- Install dev deps: `uv sync --extra dev`

## Key Conventions
- All async code uses httpx for HTTP requests
- BibTeX keys follow format: lastname_year_firstword (e.g., smith_2024_causal)
- Paper files stored at ~/.papertrail/papers/{bibtex_key}/
- SQLite database at ~/.papertrail/db/papers.db
- All database methods are async (wrapped via asyncio.to_thread)
- rclone sync is best-effort; server works fully offline
