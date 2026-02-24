# Papertrail

An MCP server for managing an academic paper library with Claude Code. Search for papers, download and convert them to markdown, generate structured summaries, and search across your library.

## What it does

- **Find papers** via Semantic Scholar, arXiv, and SSRN
- **Download and convert** PDFs to searchable markdown (pymupdf4llm)
- **Generate summaries** with section-level detail, key results, tables, and figures
- **Manage tags** with a growing vocabulary for consistent categorization
- **Full-text search** over metadata, summaries, and paper content (SQLite FTS5)
- **Sync across machines** via rclone mount (Wasabi, S3, or any rclone-supported backend)
- **Literature reviews** across multiple papers using parallel subagents

## Installation

### Prerequisites

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** -- Python package manager used to install and run the server.

- **[rclone](https://rclone.org/install/)** (optional) -- A command-line tool for syncing files to cloud storage. Papertrail uses `rclone mount` to make a remote storage bucket (S3, Wasabi, GCS, Backblaze, Dropbox, etc.) appear as a local directory. This means your paper library lives in the cloud and stays in sync across machines automatically. Without rclone, everything works fine locally in `~/.papertrail/`.

- **[FUSE-T](https://www.fuse-t.org/) or [macFUSE](https://osxfuse.github.io/)** (macOS only, required if using rclone) -- `rclone mount` needs a FUSE (Filesystem in Userspace) provider to mount remote storage as a local directory. FUSE-T is recommended: it runs in userspace, doesn't require a reboot, and can be installed with `brew install --cask fuse-t`. Linux has FUSE built in, so no extra install is needed there.

### 1. Install the MCP server

Add this to your `~/.claude.json` (global Claude Code config):

```json
{
  "mcpServers": {
    "papertrail": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/YOURUSER/papertrail", "papertrail"],
      "env": {
        "PAPERTRAIL_DATA_DIR": "${HOME}/.papertrail",
        "PAPERTRAIL_RCLONE_REMOTE": ""
      }
    }
  }
}
```

This installs and runs the server automatically via `uvx` -- no cloning required.

For local development, clone the repo and use the directory-based config instead:

```json
{
  "mcpServers": {
    "papertrail": {
      "command": "uv",
      "args": ["--directory", "/path/to/papertrail", "run", "papertrail"],
      "env": {
        "PAPERTRAIL_DATA_DIR": "${HOME}/.papertrail",
        "PAPERTRAIL_RCLONE_REMOTE": ""
      }
    }
  }
}
```

### 2. Install the skills

Clone the repo and copy the skills into your Claude Code config:

```bash
git clone https://github.com/YOURUSER/papertrail.git
cp -r papertrail/skills/* ~/.claude/skills/
```

This makes the skills available globally as slash commands.

### 3. Configure rclone (optional)

To sync your library across machines using an rclone mount:

```bash
# 1. Install a FUSE provider (macOS only)
brew install --cask fuse-t    # lightweight, no reboot needed
# OR: brew install --cask macfuse

# 2. Configure an rclone remote (S3, Wasabi, GCS, Backblaze, Dropbox, etc.)
rclone config
# Follow the interactive prompts to set up your chosen storage provider.
# This creates a named remote (e.g., "myremote") you can reference later.
# See https://rclone.org/overview/ for the full list of supported backends.

# 3. Create your bucket/directory on the remote
rclone mkdir myremote:my-bucket

# 4. Set PAPERTRAIL_RCLONE_REMOTE in your MCP config:
# "PAPERTRAIL_RCLONE_REMOTE": "myremote:my-bucket"
```

When `PAPERTRAIL_RCLONE_REMOTE` is set, the server automatically mounts the remote at `~/.papertrail` on startup and unmounts on shutdown. All file writes go directly to the mount, so changes are synced automatically.

Without rclone configured, everything works locally in `~/.papertrail/`.

## Usage

### Skills (slash commands)

**`/add-paper <identifier>`** -- Add a paper to the library. Accepts:
- arXiv ID: `/add-paper 2301.12345`
- DOI: `/add-paper 10.1257/aer.2024.001`
- SSRN URL: `/add-paper https://ssrn.com/abstract=1234567`
- Title search: `/add-paper "Causal Inference in Economics"`
- Author + title: `/add-paper Cunningham and Shah decriminalization`
- Local PDF: `/add-paper ~/Downloads/paper.pdf`

Runs the full pipeline: find, download, convert to markdown, generate a structured summary, assign tags, and store. Uses a subagent to read and summarize the paper in parallel with tag fetching.

**`/search-papers <query>`** -- Search your library by topic, author, method, or keyword.

**`/read-paper <key or description>`** -- Read a paper's summary and bring relevant sections into context.

**`/lit-review <research question>`** -- Conduct a literature review across papers in the library. Finds relevant papers, reads them all in parallel via subagents, and synthesizes findings with cross-paper comparisons, thematic groupings, and citations.

### MCP tools (available to Claude directly)

| Tool | Purpose |
|------|---------|
| `find_paper` | Search Semantic Scholar/arXiv for papers |
| `ingest_paper` | Download and start converting a paper |
| `ingest_paper_manual` | Provide a PDF for a paper that failed automatic download |
| `conversion_status` | Check PDF-to-markdown conversion progress |
| `read_paper` | Read paper markdown (full or line range) |
| `store_summary` | Store a generated summary |
| `search_library` | Search metadata, topics, keywords, summaries |
| `search_paper_text` | Full-text search over paper content |
| `get_paper_metadata` | Get metadata + summary for a paper |
| `list_papers` | Browse the library, filter by status or tag |
| `list_tags` | View the tag vocabulary |
| `add_tags` | Add new tags to the vocabulary |
| `tag_paper` | Tag a paper |
| `delete_paper` | Remove a paper from the library |
| `rebuild_index` | Force a full rescan and index rebuild |

## Configuration

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `PAPERTRAIL_DATA_DIR` | `~/.papertrail` | Data directory (rclone mount point if remote is set) |
| `PAPERTRAIL_RCLONE_REMOTE` | (empty) | rclone remote path (e.g., `myremote:my-bucket`) |
| `PAPERTRAIL_INDEX_DIR` | `~/.cache/papertrail` | Local directory for the ephemeral search index |
| `PAPERTRAIL_SEMANTIC_SCHOLAR_API_KEY` | (none) | Optional API key for higher rate limits |
| `PAPERTRAIL_HTTP_PROXY` | (none) | HTTP proxy for PDF downloads |
| `PAPERTRAIL_UNPAYWALL_EMAIL` | (none) | Email for Unpaywall API (finds legal open access PDFs) |

### Accessing paywalled papers

- **On VPN**: PDF downloads through DOI links work automatically -- publishers see your institutional IP.
- **Off VPN**: Set `PAPERTRAIL_HTTP_PROXY` to your institution's proxy URL to route PDF downloads through it.
- **Unpaywall**: Set `PAPERTRAIL_UNPAYWALL_EMAIL` to any email address to enable the Unpaywall API, which finds legal open access copies of papers. No API key needed.

## Architecture

```
~/.papertrail/                     # rclone mount (or local dir)
  tags.json                        # Global tag vocabulary
  papers/{bibtex_key}/
    metadata.json                  # Source of truth for all paper data
    paper.pdf                      # Original PDF
    paper.md                       # Markdown conversion
    summary.json                   # Convenience copy of summary

~/.cache/papertrail/
  index.db                         # Ephemeral SQLite FTS5 index
```

JSON files on the mount are the source of truth. The SQLite index is ephemeral and rebuilt on every server startup (fast metadata scan, then background fulltext indexing). The write pattern is: write JSON first, then update the index.

## Development

```bash
git clone https://github.com/YOURUSER/papertrail.git
cd papertrail
uv sync --extra dev
uv run pytest
```
