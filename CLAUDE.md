# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Papertrail is an MCP server (FastMCP) for managing an academic paper library. It is consumed primarily by Claude Code through slash-command skills in `skills/` (installed to `~/.claude/skills/`).

## Development
- Install deps: `uv sync` (runtime) / `uv sync --extra dev` (adds pytest, pytest-asyncio, respx)
- Run tests: `uv run pytest`
- Run a single test: `uv run pytest tests/test_metadata.py::test_name`
- Run server directly: `uv run papertrail` (entry point `papertrail.server:main`)
- Python >=3.12 required

## Architecture

**Layered storage.** JSON files under `~/.papertrail/papers/{bibtex_key}/metadata.json` are the source of truth. The SQLite FTS5 index at `~/.cache/papertrail/index.db` is ephemeral and rebuilt on startup from the JSON. An optional rclone remote (`PAPERTRAIL_RCLONE_REMOTE`, e.g. `wasabi:bucket`) mirrors the JSON across machines — empty means local-only.

**Write pattern.** Always: write JSON via `PaperStore` → update `PaperDatabase` index → `sync_push` the changed subpath to remote. Never write to the index without first writing the JSON. `sync_push` uses `rclone copy` (additive); explicit deletion goes through `sync_delete` (`rclone purge`).

**2-phase startup** (see `lifespan` in `server.py`):
1. Blocking: `sync_pull` from remote, then rebuild metadata index from all `metadata.json` files.
2. Background task: rebuild FTS5 fulltext index from `paper.md` files, gated by `fulltext_ready` event. Tools that need fulltext must await this event.

**Sync staleness.** `_ensure_synced` in `server.py` re-pulls from remote if >5 min since last pull, so long-lived sessions pick up changes made on other machines.

**Ingestion pipeline.** Metadata and PDF acquisition are separate tools so skills can parallelize them:
1. `ingest_paper` (or `ingest_paper_manual` for unindexed drafts) — fetches metadata, saves the paper record, optionally kicks off auto-download in the background.
2. `download_paper` — explicit PDF acquisition (`pdf_url=` or `pdf_source_path=`); used when `auto_download=False` or automated discovery failed.
3. `conversion_status` — polled until status reaches `summarizing`; pymupdf4llm conversion runs in a background task.
4. `store_summary` — persists the model-generated summary once conversion is done.

## Key modules (src/papertrail/)
- `server.py` — MCP tool definitions + lifespan
- `paper_store.py` — synchronous JSON I/O; call via `asyncio.to_thread` for bulk ops
- `database.py` — ephemeral SQLite FTS5 (`rebuild_from_papers`, `rebuild_fulltext`); all methods async via `to_thread`
- `sync.py` — `sync_pull`, `sync_pull_if_stale`, `sync_push`, `sync_delete` (rclone wrappers)
- `metadata.py` — Semantic Scholar + arXiv + SSRN + Unpaywall discovery; uses httpx
- `converter.py` — pymupdf4llm PDF→markdown
- `config.py` — env-driven paths (`data_dir`, `index_dir`, `rclone_remote`)
- `models.py` — `PaperMetadata` with `tags: list[str]`

## Conventions
- BibTeX keys: `lastname_year_firstword` (e.g. `smith_2024_causal`)
- All HTTP via httpx (async). PDF downloads may also use `curl_cffi` for bot-detection bypass.
- Tags live both in each paper's `metadata.json` and in global `~/.papertrail/tags.json`
- When reading or analyzing multiple papers, dispatch Task subagents in parallel (one per paper) instead of sequential reads in the main context — keeps paper text out of the main window.

## Skills
`skills/` contains the user-facing slash commands (`/add-paper`, `/search-papers`, `/read-paper`, `/lit-review`, `/find-pdfs`, `/verify-summary`). They orchestrate MCP tools; changes to tool signatures or output shape must be reflected in the matching `skills/*/SKILL.md`.
