# Papertrail

> **Warning**: This project was entirely vibecoded in a single Claude Code session. It has not been battle-tested in production. There will be bugs. PDF conversion quality varies. API rate limits may bite you. The rclone sync strategy is naive. Use at your own risk, and expect to fix things as you go.

An MCP server for managing an academic paper library with Claude Code. Search for papers, download and convert them to markdown, generate structured summaries, and search across your library.

## What it does

- **Find papers** via Semantic Scholar, arXiv, and SSRN
- **Download and convert** PDFs to searchable markdown (pymupdf4llm)
- **Generate summaries** with section-level detail, key results, tables, and figures
- **Manage tags** with a growing vocabulary for consistent categorization
- **Full-text search** over metadata, summaries, and paper content (SQLite FTS5)
- **Sync across machines** via rclone (Wasabi, S3, or any rclone-supported backend)

## Installation

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)
- [rclone](https://rclone.org/install/) (optional, for cross-machine sync)

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

Clone the repo and run the install script:

```bash
git clone https://github.com/YOURUSER/papertrail.git
./papertrail/scripts/install-skills.sh
```

This symlinks the skill files into `~/.claude/skills/` so they're available globally.

Alternatively, install skills manually without cloning:

```bash
for skill in add-paper search-papers read-paper; do
  mkdir -p ~/.claude/skills/$skill
  curl -sL "https://raw.githubusercontent.com/YOURUSER/papertrail/main/skills/$skill/SKILL.md" \
    -o ~/.claude/skills/$skill/SKILL.md
done
```

### 3. Configure rclone (optional)

To sync your library across machines, configure an rclone remote and set `PAPERTRAIL_RCLONE_REMOTE` in your MCP config:

```bash
# Set up a Wasabi remote (or S3, GCS, etc.)
rclone config

# Then update PAPERTRAIL_RCLONE_REMOTE in ~/.claude.json:
# "PAPERTRAIL_RCLONE_REMOTE": "wasabi:your-bucket/papertrail"
```

Without rclone configured, everything works locally in `~/.papertrail/`.

## Usage

### Skills (slash commands)

**`/add-paper <identifier>`** -- Add a paper to the library. Accepts:
- arXiv ID: `/add-paper 2301.12345`
- DOI: `/add-paper 10.1257/aer.2024.001`
- SSRN URL: `/add-paper https://ssrn.com/abstract=1234567`
- Title search: `/add-paper "Causal Inference in Economics"`

This runs the full pipeline: find, download, convert to markdown, generate a structured summary, assign tags, and store.

**`/search-papers <query>`** -- Search your library by topic, author, method, or keyword.

**`/read-paper <key or description>`** -- Read a paper's summary and bring relevant sections into context.

### MCP tools (available to Claude directly)

| Tool | Purpose |
|------|---------|
| `find_paper` | Search Semantic Scholar/arXiv for papers |
| `ingest_paper` | Download and start converting a paper |
| `conversion_status` | Check PDF-to-markdown conversion progress |
| `read_paper` | Read paper markdown (full or line range) |
| `store_summary` | Store a generated summary |
| `search_library` | Search metadata, topics, keywords, summaries |
| `search_paper_text` | Full-text search over paper content |
| `get_paper_metadata` | Get metadata + summary for a paper |
| `list_papers` | Browse the library |
| `list_tags` | View the tag vocabulary |
| `add_tags` | Add new tags to the vocabulary |
| `tag_paper` | Tag a paper |
| `sync` | Force rclone sync |

## Configuration

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `PAPERTRAIL_DATA_DIR` | `~/.papertrail` | Local data directory |
| `PAPERTRAIL_RCLONE_REMOTE` | (empty) | rclone remote path (e.g., `wasabi:bucket/papertrail`) |
| `PAPERTRAIL_SEMANTIC_SCHOLAR_API_KEY` | (none) | Optional API key for higher rate limits |
| `PAPERTRAIL_HTTP_PROXY` | (none) | HTTP proxy for PDF downloads (e.g., institutional proxy for off-VPN access) |
| `PAPERTRAIL_UNPAYWALL_EMAIL` | (none) | Email for Unpaywall API (finds legal open access PDFs, no key needed) |

### Accessing paywalled papers

If you have institutional access (e.g., MIT, Stanford, etc.):

- **On VPN**: PDF downloads through DOI links work automatically -- publishers see your institutional IP.
- **Off VPN**: Set `PAPERTRAIL_HTTP_PROXY` to your institution's proxy URL to route PDF downloads through it.
- **Unpaywall**: Set `PAPERTRAIL_UNPAYWALL_EMAIL` to any email address to enable the Unpaywall API, which finds legal open access copies of papers (preprints, author copies, green OA). No API key needed.

## Data storage

Papers are stored locally at `~/.papertrail/`:

```
~/.papertrail/
  db/papers.db                          # SQLite database (metadata + search index)
  papers/smith_2024_causal/
    paper.pdf                           # Original PDF
    paper.md                            # Markdown conversion
    metadata.json                       # Structured metadata
    summary.json                        # Section-level summary
```

## Development

```bash
git clone https://github.com/YOURUSER/papertrail.git
cd papertrail
uv sync --extra dev
uv run pytest
```
