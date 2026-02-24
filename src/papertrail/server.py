import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP, Context

from papertrail.config import PapertrailConfig
from papertrail.converter import PdfConverter
from papertrail.database import PaperDatabase
from papertrail.metadata import MetadataFetcher
from papertrail.models import PaperMetadata
from papertrail.storage import StorageSync

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    config = PapertrailConfig.from_env()
    config.ensure_directories()
    storage = StorageSync(config)

    try:
        await storage.sync_db(direction="pull")
    except Exception as exc:
        logger.debug("Could not pull DB from remote: %s", exc)

    db = PaperDatabase(config.db_path)
    await db.initialize()

    fetcher = MetadataFetcher(config)
    converter = PdfConverter()

    yield {
        "db": db,
        "config": config,
        "storage": storage,
        "fetcher": fetcher,
        "converter": converter,
    }

    await fetcher.close()
    await db.close()


mcp = FastMCP("papertrail", lifespan=lifespan)


def _get_context(ctx: Context) -> dict:
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Paper discovery
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_paper(query: str, limit: int = 10, ctx: Context = None) -> str:
    """Search for academic papers by query string.

    Searches Semantic Scholar and arXiv. Returns titles, authors, years,
    citation counts, and identifiers for matching papers.

    Args:
        query: Search terms (e.g., "causal inference machine learning")
        limit: Maximum number of results to return (default 10)
    """
    lc = _get_context(ctx)
    fetcher: MetadataFetcher = lc["fetcher"]
    results = await fetcher.search(query, limit=limit)
    if not results:
        return "No papers found for this query. Try different search terms, or use web search as a fallback."
    lines = []
    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."
        identifiers = []
        if r.doi:
            identifiers.append(f"DOI: {r.doi}")
        if r.arxiv_id:
            identifiers.append(f"arXiv: {r.arxiv_id}")
        id_str = " | ".join(identifiers) if identifiers else "no identifier"
        lines.append(
            f"{i}. **{r.title}** ({r.year})\n"
            f"   Authors: {authors_str}\n"
            f"   Citations: {r.citation_count or 'N/A'} | {id_str} | Source: {r.source}"
        )
        if r.abstract:
            truncated = r.abstract[:200] + "..." if len(r.abstract) > 200 else r.abstract
            lines.append(f"   Abstract: {truncated}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Paper ingestion
# ---------------------------------------------------------------------------


@mcp.tool()
async def ingest_paper(identifier: str, ctx: Context = None) -> str:
    """Download a paper and start converting it to markdown.

    Accepts a DOI, arXiv ID, SSRN ID/URL, or paper URL. Downloads the PDF,
    fetches metadata, generates a BibTeX key, and starts background conversion.

    Use conversion_status to check progress after calling this.

    Args:
        identifier: DOI (e.g., "10.1257/aer.2024.001"), arXiv ID (e.g., "2301.12345"),
                    SSRN URL/ID, or direct paper URL
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    config: PapertrailConfig = lc["config"]
    fetcher: MetadataFetcher = lc["fetcher"]
    converter: PdfConverter = lc["converter"]
    storage: StorageSync = lc["storage"]

    # 1. Look up metadata
    result = await fetcher.get_by_identifier(identifier)

    # Fallback for SSRN if Semantic Scholar doesn't have it
    if result is None:
        import re
        ssrn_match = re.search(r"(?:abstract=|^)(\d{5,})", identifier)
        if ssrn_match:
            result = await fetcher.get_ssrn_metadata(ssrn_match.group(1))

    if result is None:
        return f"Could not find paper with identifier: {identifier}. Try using find_paper to search by title."

    # 2. Generate unique bibtex key
    bibtex_key = await fetcher.generate_unique_key(result, db)
    paper_dir = config.papers_dir / bibtex_key
    paper_dir.mkdir(parents=True, exist_ok=True)

    # 3. Create DB record
    paper = PaperMetadata(
        bibtex_key=bibtex_key,
        title=result.title,
        authors=result.authors,
        year=result.year,
        abstract=result.abstract,
        doi=result.doi,
        arxiv_id=result.arxiv_id,
        ssrn_id=result.ssrn_id,
        url=result.url,
        topics=result.topics,
        fields_of_study=result.fields_of_study,
        citation_count=result.citation_count,
        added_date=datetime.now(UTC).isoformat(),
        status="downloading",
    )
    await db.upsert_paper(paper)

    # 4. Download PDF
    pdf_path = paper_dir / "paper.pdf"
    try:
        await fetcher.download_pdf(result, pdf_path)
    except Exception as exc:
        await db.update_status(bibtex_key, "error")
        return f"Ingested metadata for **{bibtex_key}** but PDF download failed: {exc}"

    # 5. Save metadata.json
    metadata_path = paper_dir / "metadata.json"
    metadata_path.write_text(json.dumps(result.model_dump(), indent=2, default=str))

    # 6. Start background conversion
    await db.update_status(bibtex_key, "converting")
    md_path = paper_dir / "paper.md"

    async def background_convert():
        try:
            content = await converter.convert(pdf_path, md_path)
            await db.index_fulltext(bibtex_key, content)
            await db.update_status(bibtex_key, "summarizing")
            # Push files to remote
            try:
                await storage.push_file(pdf_path, f"papers/{bibtex_key}/paper.pdf")
                await storage.push_file(md_path, f"papers/{bibtex_key}/paper.md")
                await storage.push_file(metadata_path, f"papers/{bibtex_key}/metadata.json")
                await storage.sync_db(direction="push")
            except Exception:
                logger.debug("Remote sync failed during background convert", exc_info=True)
        except Exception:
            logger.error("Background conversion failed for %s", bibtex_key, exc_info=True)
            await db.update_status(bibtex_key, "error")

    asyncio.create_task(background_convert())

    return (
        f"Paper ingested as **{bibtex_key}**\n\n"
        f"- Title: {result.title}\n"
        f"- Authors: {', '.join(result.authors)}\n"
        f"- Year: {result.year}\n\n"
        f"PDF is being converted to markdown in the background. "
        f"Use `conversion_status(\"{bibtex_key}\")` to check progress."
    )


@mcp.tool()
async def conversion_status(bibtex_key: str, ctx: Context = None) -> str:
    """Check the conversion and processing status of a paper.

    Args:
        bibtex_key: The paper's BibTeX key (e.g., "smith_2024_causal")
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"
    status_desc = {
        "downloading": "PDF is being downloaded",
        "converting": "PDF is being converted to markdown (this may take a minute)",
        "summarizing": "Conversion complete. Ready for summary generation.",
        "ready": "Fully processed with summary",
        "error": "Something went wrong during processing",
    }
    desc = status_desc.get(paper.status, paper.status)
    return f"**{paper.title}**\nStatus: **{paper.status}** - {desc}"


# ---------------------------------------------------------------------------
# Reading papers
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_paper(
    bibtex_key: str,
    start_line: int | None = None,
    end_line: int | None = None,
    ctx: Context = None,
) -> str:
    """Read the markdown content of a paper, optionally a specific line range.

    Args:
        bibtex_key: The paper's BibTeX key
        start_line: Optional start line number (1-indexed)
        end_line: Optional end line number (1-indexed, inclusive)
    """
    lc = _get_context(ctx)
    config: PapertrailConfig = lc["config"]
    md_path = config.papers_dir / bibtex_key / "paper.md"
    if not md_path.exists():
        return f"No markdown file found for {bibtex_key}. Check conversion_status."
    content = md_path.read_text(encoding="utf-8")
    if start_line is not None or end_line is not None:
        lines = content.splitlines()
        start_idx = (start_line or 1) - 1
        end_idx = end_line or len(lines)
        selected = lines[start_idx:end_idx]
        return (
            f"Lines {start_idx + 1}-{min(end_idx, len(lines))} of {len(lines)} total:\n\n"
            + "\n".join(selected)
        )
    return content


@mcp.tool()
async def get_paper_metadata(bibtex_key: str, ctx: Context = None) -> str:
    """Get structured metadata and summary for a specific paper.

    Args:
        bibtex_key: The paper's BibTeX key
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    paper_tags = await db.get_paper_tags(bibtex_key)

    lines = [
        f"# {paper.title}",
        f"**Key**: {paper.bibtex_key}",
        f"**Authors**: {', '.join(paper.authors)}",
        f"**Year**: {paper.year}",
        f"**Status**: {paper.status}",
    ]
    if paper.journal:
        lines.append(f"**Journal**: {paper.journal}")
    if paper.doi:
        lines.append(f"**DOI**: {paper.doi}")
    if paper.arxiv_id:
        lines.append(f"**arXiv**: {paper.arxiv_id}")
    if paper.ssrn_id:
        lines.append(f"**SSRN**: {paper.ssrn_id}")
    if paper.citation_count is not None:
        lines.append(f"**Citations**: {paper.citation_count}")
    if paper_tags:
        lines.append(f"**Tags**: {', '.join(paper_tags)}")
    if paper.topics:
        lines.append(f"**Topics**: {', '.join(paper.topics)}")
    if paper.keywords:
        lines.append(f"**Keywords**: {', '.join(paper.keywords)}")
    if paper.fields_of_study:
        lines.append(f"**Fields**: {', '.join(paper.fields_of_study)}")
    if paper.abstract:
        lines.append(f"\n**Abstract**: {paper.abstract}")
    if paper.summary:
        lines.append(f"\n**Summary**: ```json\n{json.dumps(paper.summary, indent=2)}\n```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary storage
# ---------------------------------------------------------------------------


@mcp.tool()
async def store_summary(
    bibtex_key: str,
    summary: str,
    keywords: list[str] | None = None,
    ctx: Context = None,
) -> str:
    """Store a summary for a paper and set its status to 'ready'.

    Call this after reading a paper's markdown and generating a structured summary.

    Args:
        bibtex_key: The paper's BibTeX key
        summary: JSON string with the summary. Should include keys like
                 "main_contribution", "methodology", "findings", "limitations",
                 "section_summaries" (object mapping section names to summaries)
        keywords: Optional list of descriptive keywords for the paper
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    storage: StorageSync = lc["storage"]
    config: PapertrailConfig = lc["config"]

    paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    try:
        summary_data = json.loads(summary) if isinstance(summary, str) else summary
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in summary: {exc}"

    await db.store_summary(bibtex_key, summary_data)
    if keywords:
        await db.update_keywords(bibtex_key, keywords)
    await db.update_status(bibtex_key, "ready")

    # Save summary.json to paper directory
    summary_path = config.papers_dir / bibtex_key / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary_data, indent=2))

    try:
        await storage.push_file(summary_path, f"papers/{bibtex_key}/summary.json")
        await storage.sync_db(direction="push")
    except Exception:
        logger.debug("Remote sync failed after storing summary", exc_info=True)

    return f"Summary stored for **{bibtex_key}**. Status set to 'ready'."


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_library(query: str, limit: int = 20, ctx: Context = None) -> str:
    """Search the paper library by metadata, topics, keywords, and summaries.

    Uses full-text search over paper metadata and summaries. Good for finding
    papers by topic, method, author, or content of summaries.

    Args:
        query: Search query (e.g., "climate risk", "difference-in-differences", "Smith")
        limit: Maximum results to return (default 20)
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    results = await db.search_metadata(query, limit=limit)
    if not results:
        return f"No papers found matching '{query}' in library metadata/summaries."
    lines = []
    for paper in results:
        paper_tags = await db.get_paper_tags(paper.bibtex_key)
        tags_str = f" [{', '.join(paper_tags)}]" if paper_tags else ""
        authors_str = ", ".join(paper.authors[:3])
        if len(paper.authors) > 3:
            authors_str += " et al."
        entry = f"- **{paper.bibtex_key}**: {paper.title} ({paper.year}) by {authors_str}{tags_str}"
        if paper.abstract:
            truncated = paper.abstract[:150] + "..." if len(paper.abstract) > 150 else paper.abstract
            entry += f"\n  {truncated}"
        lines.append(entry)
    return "\n\n".join(lines)


@mcp.tool()
async def search_paper_text(query: str, limit: int = 10, ctx: Context = None) -> str:
    """Search over the full text content of papers in the library.

    Uses full-text search over the markdown content of all papers.
    Returns matching snippets with paper keys. Use this when search_library
    doesn't find what you need.

    Args:
        query: Search query to match against paper content
        limit: Maximum results to return (default 10)
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    results = await db.search_fulltext(query, limit=limit)
    if not results:
        return f"No matches for '{query}' in paper full text."
    lines = []
    for r in results:
        lines.append(f"**{r['bibtex_key']}**: ...{r['snippet']}...")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Paper listing
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_papers(
    status: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """List papers in the library, optionally filtered by status or tag.

    Args:
        status: Filter by status (downloading, converting, summarizing, ready, error)
        tag: Filter by tag name
        limit: Maximum number of papers to return (default 50)
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    papers = await db.list_papers(status=status, tag=tag, limit=limit)
    if not papers:
        return "No papers found in the library."
    lines = []
    for paper in papers:
        paper_tags = await db.get_paper_tags(paper.bibtex_key)
        tags_str = f" [{', '.join(paper_tags)}]" if paper_tags else ""
        lines.append(
            f"- **{paper.bibtex_key}**: {paper.title} ({paper.year}){tags_str} [{paper.status}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tag management
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_tags(prefix: str | None = None, ctx: Context = None) -> str:
    """Return all tags in the vocabulary with paper counts.

    Args:
        prefix: Optional prefix to filter tags (e.g., "climate" returns "climate-risk", "climate-finance", etc.)
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    tags = await db.list_tags(prefix=prefix)
    if not tags:
        return "No tags in the vocabulary yet."
    lines = []
    for tag in tags:
        desc = f" - {tag.description}" if tag.description else ""
        lines.append(f"- **{tag.tag}** ({tag.paper_count} papers){desc}")
    return "\n".join(lines)


@mcp.tool()
async def add_tags(tags: str, ctx: Context = None) -> str:
    """Add new tags to the vocabulary.

    Args:
        tags: JSON array of objects with 'tag' (required) and 'description' (optional).
              Example: [{"tag": "causal-inference", "description": "Papers using causal methods"}]
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    storage: StorageSync = lc["storage"]

    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(tag_list, list):
        return "Expected a JSON array of tag objects."

    await db.add_tags(tag_list)

    try:
        await storage.sync_db(direction="push")
    except Exception:
        pass

    tag_names = [t["tag"] for t in tag_list]
    return f"Added tags: {', '.join(tag_names)}"


@mcp.tool()
async def tag_paper(bibtex_key: str, tags: str, ctx: Context = None) -> str:
    """Associate tags with a paper. Tags must exist in the vocabulary first (use add_tags).

    Args:
        bibtex_key: The paper's BibTeX key
        tags: JSON array of tag names, e.g. ["causal-inference", "macro"]
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    storage: StorageSync = lc["storage"]

    paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(tag_list, list):
        return "Expected a JSON array of tag names."

    # Check tags exist
    existing_tags = {t.tag for t in await db.list_tags()}
    missing = [t for t in tag_list if t not in existing_tags]
    if missing:
        return f"Tags not in vocabulary: {', '.join(missing)}. Use add_tags first."

    await db.tag_paper(bibtex_key, tag_list)

    try:
        await storage.sync_db(direction="push")
    except Exception:
        pass

    return f"Tagged **{bibtex_key}** with: {', '.join(tag_list)}"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@mcp.tool()
async def sync(direction: str = "push", ctx: Context = None) -> str:
    """Force rclone sync between local storage and remote.

    Args:
        direction: "push" (local to remote) or "pull" (remote to local)
    """
    lc = _get_context(ctx)
    storage: StorageSync = lc["storage"]
    if not await storage.is_available():
        return "Sync not available. rclone is not configured or PAPERTRAIL_RCLONE_REMOTE is not set."
    try:
        if direction == "pull":
            result = await storage.pull()
        else:
            result = await storage.push()
        return f"Sync complete ({direction}).\n{result}" if result else f"Sync complete ({direction})."
    except Exception as exc:
        return f"Sync failed: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
