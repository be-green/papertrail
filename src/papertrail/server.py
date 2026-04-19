import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Context

from papertrail.config import PapertrailConfig
from papertrail.converter import PdfConverter
from papertrail.database import PaperDatabase
from papertrail.metadata import MetadataFetcher
from papertrail.models import PaperMetadata, SearchResult
from papertrail.sync import sync_pull, sync_pull_if_stale, sync_push, sync_delete
from papertrail.paper_store import PaperStore
from papertrail.tag_similarity import similar_tags

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    config = PapertrailConfig.from_env()

    # Sync remote data to local directory
    await sync_pull(config.rclone_remote, config.data_dir)
    sync_state = {"last_pull_time": time.monotonic()}

    config.ensure_directories()

    store = PaperStore(config)
    db = PaperDatabase(config.index_db_path)
    await db.initialize()

    # Phase 1 (blocking): rebuild index from JSON files
    papers = await asyncio.to_thread(store.scan_all_papers)
    tags = await asyncio.to_thread(store.read_tags)
    await db.rebuild_from_papers(papers, tags)
    logger.info("Index rebuilt: %d papers, %d tags", len(papers), len(tags))

    # Phase 2 (background): rebuild fulltext index from paper.md files
    fulltext_ready = asyncio.Event()

    async def rebuild_fulltext_index():
        try:
            paper_texts = []
            for paper in papers:
                content = await asyncio.to_thread(store.read_paper_markdown, paper.bibtex_key)
                if content:
                    paper_texts.append((paper.bibtex_key, content))
            if paper_texts:
                await db.rebuild_fulltext(paper_texts)
            logger.info("Fulltext index rebuilt: %d papers", len(paper_texts))
        except Exception:
            logger.error("Fulltext index rebuild failed", exc_info=True)
        finally:
            fulltext_ready.set()

    asyncio.create_task(rebuild_fulltext_index())

    fetcher = MetadataFetcher(config)
    converter = PdfConverter()

    yield {
        "db": db,
        "config": config,
        "store": store,
        "fetcher": fetcher,
        "converter": converter,
        "fulltext_ready": fulltext_ready,
        "remote": config.rclone_remote,
        "sync_state": sync_state,
    }

    await fetcher.close()
    await db.close()


mcp = FastMCP("papertrail", lifespan=lifespan)


def _get_context(ctx: Context) -> dict:
    return ctx.request_context.lifespan_context


def _format_citation(paper: PaperMetadata) -> str:
    """Format a paper as a human-readable citation: Last (Year) or Last and Last (Year)."""
    year = paper.year or "n.d."
    if not paper.authors:
        return f"Unknown ({year})"
    def last_name(author: str) -> str:
        # Handle "Last, First" and "First Last" formats
        if "," in author:
            return author.split(",")[0].strip()
        return author.split()[-1]
    if len(paper.authors) == 1:
        name = last_name(paper.authors[0])
    elif len(paper.authors) == 2:
        name = f"{last_name(paper.authors[0])} and {last_name(paper.authors[1])}"
    else:
        name = f"{last_name(paper.authors[0])} et al."
    return f"{name} ({year})"


async def _ensure_synced(lc: dict) -> None:
    """Re-pull from remote if the last sync is stale, then rebuild the index."""
    config: PapertrailConfig = lc["config"]
    sync_state = lc["sync_state"]
    new_time = await sync_pull_if_stale(
        lc["remote"], config.data_dir, sync_state["last_pull_time"]
    )
    if new_time != sync_state["last_pull_time"]:
        sync_state["last_pull_time"] = new_time
        db: PaperDatabase = lc["db"]
        store: PaperStore = lc["store"]
        papers = await asyncio.to_thread(store.scan_all_papers)
        tags = await asyncio.to_thread(store.read_tags)
        await db.rebuild_from_papers(papers, tags)
        logger.info("Index refreshed after re-sync: %d papers", len(papers))


async def _push_paper(lc: dict, bibtex_key: str) -> None:
    """Push a paper directory to the remote after a local write."""
    await sync_push(lc["remote"], lc["config"].data_dir, f"papers/{bibtex_key}")


async def _push_tags(lc: dict) -> None:
    """Push tags.json to the remote after a local write."""
    await sync_push(lc["remote"], lc["config"].data_dir, "tags.json")


async def _push_all_papers(lc: dict) -> None:
    """Push the entire papers/ tree. rclone copy is additive and skips files
    whose size+mtime match the remote, so this is cheap even when only a
    handful of metadata.json files changed during a bulk tag rewrite.
    """
    await sync_push(lc["remote"], lc["config"].data_dir, "papers")


async def _start_conversion(lc: dict, bibtex_key: str, pdf_path: Path) -> None:
    """Flip paper to 'converting' and run PDF-to-markdown conversion in the background.

    Shared by download_paper (explicit PDF registration) and the auto-download
    background task spawned from ingest_paper.
    """
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]
    converter: PdfConverter = lc["converter"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        return

    paper.status = "converting"
    await asyncio.to_thread(store.write_paper_metadata, paper)
    await db.update_status(bibtex_key, "converting")
    md_path = pdf_path.parent / "paper.md"

    async def _run():
        try:
            content = await converter.convert(pdf_path, md_path)
            await db.index_fulltext(bibtex_key, content)
            paper.status = "summarizing"
            await asyncio.to_thread(store.write_paper_metadata, paper)
            await db.update_status(bibtex_key, "summarizing")
        except Exception:
            logger.error("Conversion failed for %s", bibtex_key, exc_info=True)
            paper.status = "error"
            await asyncio.to_thread(store.write_paper_metadata, paper)
            await db.update_status(bibtex_key, "error")
        await _push_paper(lc, bibtex_key)

    asyncio.create_task(_run())


async def _background_auto_download(lc: dict, bibtex_key: str) -> None:
    """Run the automated PDF download pipeline in the background after ingest.

    On success, kicks off conversion via _start_conversion. On failure,
    leaves the paper in 'pending_pdf' so the find-pdfs skill and manual
    fallbacks still work. Must never raise — spawned as a fire-and-forget
    task.
    """
    try:
        config: PapertrailConfig = lc["config"]
        fetcher: MetadataFetcher = lc["fetcher"]
        store: PaperStore = lc["store"]

        paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
        if paper is None:
            return

        paper_dir = config.papers_dir / bibtex_key
        paper_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = paper_dir / "paper.pdf"

        search_result = SearchResult.from_metadata(paper)
        dl = await fetcher.download_pdf(search_result, pdf_path)
        if not dl.success:
            logger.info(
                "Auto-download did not find a PDF for %s; remains pending_pdf", bibtex_key
            )
            return

        await _start_conversion(lc, bibtex_key, pdf_path)
    except Exception:
        logger.error("Auto-download background task crashed for %s", bibtex_key, exc_info=True)


# ---------------------------------------------------------------------------
# Paper discovery
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_paper(query: str, limit: int = 10, ctx: Context = None) -> str:
    """Search for academic papers by query string.

    Searches Semantic Scholar, arXiv, Crossref, and OpenAlex in parallel.
    Results are deduplicated by DOI / arXiv ID / normalized title with
    Semantic Scholar preferred. Returns titles, authors, years, citation
    counts, and identifiers for matching papers.

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


async def _save_search_result_as_paper(
    lc: dict, result: SearchResult, auto_download: bool
) -> str:
    """Generate a bibtex key, persist the paper, and optionally start auto-download.

    Shared by ingest_paper (metadata-lookup path) and ingest_paper_manual
    (user-provided metadata path). Returns the user-facing response string.
    """
    db: PaperDatabase = lc["db"]
    config: PapertrailConfig = lc["config"]
    fetcher: MetadataFetcher = lc["fetcher"]
    store: PaperStore = lc["store"]

    bibtex_key = await fetcher.generate_unique_key(result, db, store)
    paper_dir = config.papers_dir / bibtex_key
    paper_dir.mkdir(parents=True, exist_ok=True)

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
        status="pending_pdf",
    )

    await asyncio.to_thread(store.write_paper_metadata, paper)
    await db.upsert_paper(paper)
    await _push_paper(lc, bibtex_key)

    header = (
        f"Paper metadata saved as **{bibtex_key}**\n\n"
        f"- Title: {result.title}\n"
        f"- Authors: {', '.join(result.authors)}\n"
        f"- Year: {result.year}\n"
    )

    if auto_download:
        asyncio.create_task(_background_auto_download(lc, bibtex_key))
        return (
            header
            + f"- Status: pending_pdf (PDF download running in background)\n\n"
            f"**NEXT STEP (required):** Poll `conversion_status(\"{bibtex_key}\")` "
            f"every 10s until status is 'summarizing' or 'error'. Then read the "
            f"paper with `read_paper` and call `store_summary`. If status stays "
            f"'pending_pdf' after a minute, the auto-download failed — call "
            f"`download_paper(\"{bibtex_key}\", pdf_url=...)` with a URL you find."
        )

    return (
        header
        + f"- Status: pending_pdf\n\n"
        f"**NEXT STEP (required):** Call `download_paper(\"{bibtex_key}\")` to "
        f"fetch the PDF (or pass `pdf_url=`/`pdf_source_path=`), then poll "
        f"`conversion_status` and call `store_summary` once ready."
    )


@mcp.tool()
async def ingest_paper(
    identifier: str,
    auto_download: bool = True,
    ctx: Context = None,
) -> str:
    """Fetch metadata for a paper and save it to the library.

    When auto_download=True (default), kicks off the PDF download and
    conversion pipeline in the background immediately after saving metadata.
    Returns right away with the bibtex key; poll conversion_status to track
    progress and call store_summary once status is 'summarizing'.

    Set auto_download=False if you plan to provide a pdf_url/pdf_source_path
    manually or orchestrate PDF discovery in parallel (e.g. find-pdfs skill).

    If the identifier cannot be found in any index (Semantic Scholar, Crossref,
    OpenAlex, SSRN), this tool fails. For working papers or unindexed drafts,
    use `ingest_paper_manual` with title/authors provided directly.

    Args:
        identifier: DOI (e.g., "10.1257/aer.2024.001"), arXiv ID (e.g., "2301.12345"),
                    SSRN URL/ID, or direct paper URL
        auto_download: If True, start automated PDF download in the background (default True)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    fetcher: MetadataFetcher = lc["fetcher"]

    result = await fetcher.get_by_identifier(identifier)

    # Fallback for SSRN if Semantic Scholar doesn't have it
    if result is None:
        import re
        ssrn_match = re.search(r"(?:abstract=|^)(\d{5,})", identifier)
        if ssrn_match:
            ssrn_id = ssrn_match.group(1)
            result = await fetcher.get_crossref_metadata(f"10.2139/ssrn.{ssrn_id}")
            if result is None:
                result = await fetcher.get_ssrn_metadata(ssrn_id)

    if result is None:
        return (
            f"Could not find paper with identifier: {identifier}.\n\n"
            f"Options:\n"
            f"- Use `find_paper` to search by title across Semantic Scholar, arXiv, "
            f"Crossref, and OpenAlex.\n"
            f"- For working papers or unindexed drafts, call `ingest_paper_manual` "
            f"with title and authors provided directly."
        )

    return await _save_search_result_as_paper(lc, result, auto_download)


@mcp.tool()
async def ingest_paper_manual(
    title: str,
    authors: list[str],
    year: int | None = None,
    abstract: str | None = None,
    url: str | None = None,
    doi: str | None = None,
    auto_download: bool = False,
    ctx: Context = None,
) -> str:
    """Save a paper to the library using user-provided metadata.

    Use this for working papers, conference drafts, or other unindexed
    documents that `ingest_paper` cannot find. Skips metadata lookup entirely
    and creates a paper directly from the supplied title and authors.

    After this call, provide the PDF by calling `download_paper` with either
    `pdf_source_path=` (local file) or `pdf_url=` (direct PDF link, e.g. an
    author's website). Automated discovery usually won't succeed for working
    papers, so `auto_download` defaults to False here.

    Args:
        title: Full paper title
        authors: List of author names (e.g., ["John Smith", "Jane Doe"])
        year: Publication or working paper year (optional)
        abstract: Abstract text (optional)
        url: Paper URL, e.g. an author's page or working paper series link (optional)
        doi: DOI if the paper has one, even if unindexed (optional)
        auto_download: If True and a URL is provided, try to download from it.
                       Default False — most working papers need manual PDF provision.
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)

    if not title or not title.strip():
        return "Title is required."
    if not authors:
        return "At least one author is required."

    result = SearchResult(
        title=title.strip(),
        authors=[a.strip() for a in authors if a and a.strip()],
        year=year,
        abstract=abstract,
        url=url,
        doi=doi,
        source="manual",
    )

    return await _save_search_result_as_paper(lc, result, auto_download)


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
        "pending_pdf": "Metadata saved, waiting for PDF. Call download_paper to fetch or provide the PDF.",
        "converting": "PDF is being converted to markdown (this may take a minute)",
        "summarizing": "Conversion complete. Ready for summary generation.",
        "ready": "Fully processed with summary",
        "error": "Something went wrong during processing",
    }
    desc = status_desc.get(paper.status, paper.status)
    return f"**{paper.title}**\nStatus: **{paper.status}** - {desc}"


@mcp.tool()
async def download_paper(
    bibtex_key: str,
    pdf_url: str | None = None,
    pdf_source_path: str | None = None,
    ctx: Context = None,
) -> str:
    """Download or provide a PDF for a paper in the library.

    The paper must already exist (from a prior ingest_paper call). Three modes:
    - pdf_url: Download from a specific URL
    - pdf_source_path: Copy from a local file
    - Neither: Run the full automated download pipeline (arXiv, NBER, Unpaywall, etc.)

    On success, starts background conversion to markdown.

    Args:
        bibtex_key: The paper's BibTeX key
        pdf_url: Optional URL to download the PDF from
        pdf_source_path: Optional absolute path to a local PDF file
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    config: PapertrailConfig = lc["config"]
    converter: PdfConverter = lc["converter"]
    fetcher: MetadataFetcher = lc["fetcher"]
    store: PaperStore = lc["store"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    paper_dir = config.papers_dir / bibtex_key
    paper_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = paper_dir / "paper.pdf"

    if pdf_url:
        try:
            response = await fetcher._download_get(pdf_url)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and (
                "pdf" in content_type or response.content[:5] == b"%PDF-"
            ):
                pdf_path.write_bytes(response.content)
            else:
                return (
                    f"Failed to download PDF from URL: status={response.status_code}, "
                    f"content-type={content_type}"
                )
        except Exception as exc:
            return f"Failed to download PDF from URL: {exc}"
    elif pdf_source_path:
        import shutil
        source = Path(pdf_source_path)
        if not source.exists():
            return f"Source PDF not found at: {pdf_source_path}"
        shutil.copy2(source, pdf_path)
    else:
        # Automated pipeline: reconstruct SearchResult and try all sources
        search_result = SearchResult.from_metadata(paper)
        dl = await fetcher.download_pdf(search_result, pdf_path)
        if not dl.success:
            failure_details = []
            for attempt in dl.attempts:
                detail = f"  - {attempt.url}: "
                if attempt.cloudflare_blocked:
                    detail += "blocked by Cloudflare"
                elif attempt.error:
                    detail += attempt.error
                else:
                    detail += f"status={attempt.status_code}"
                failure_details.append(detail)
            details_text = "\n".join(failure_details) if failure_details else "  No URLs to try"
            candidate_text = ""
            if dl.candidate_urls:
                candidate_text = "\n**Candidate URLs:**\n" + "\n".join(
                    f"  - {u}" for u in dl.candidate_urls
                )
            return (
                f"Automated PDF download failed for **{bibtex_key}**.\n\n"
                f"**Attempts:**\n{details_text}\n"
                f"{candidate_text}\n\n"
                f"Try `download_paper(\"{bibtex_key}\", pdf_url=...)` with a direct PDF URL,\n"
                f"or `download_paper(\"{bibtex_key}\", pdf_source_path=...)` with a local file."
            )

    if not pdf_path.exists():
        return (
            f"No PDF found at `{pdf_path}`.\n"
            f"Provide a pdf_url, pdf_source_path, or omit both to try automated download."
        )

    await _start_conversion(lc, bibtex_key, pdf_path)

    return (
        f"PDF registered for **{bibtex_key}**. Converting to markdown in the background.\n"
        f"Use `conversion_status(\"{bibtex_key}\")` to check progress."
    )


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
    await _ensure_synced(lc)
    config: PapertrailConfig = lc["config"]
    md_path = config.papers_dir / bibtex_key / "paper.md"
    if not md_path.exists():
        return f"No markdown file found for {bibtex_key}. Check conversion_status."
    content = md_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    total_lines = len(lines)
    if start_line is not None or end_line is not None:
        start_idx = (start_line or 1) - 1
        end_idx = end_line or total_lines
        selected = lines[start_idx:end_idx]
        return (
            f"Lines {start_idx + 1}-{min(end_idx, total_lines)} of {total_lines} total:\n\n"
            + "\n".join(selected)
        )
    return f"Total lines: {total_lines}\n\n" + content


@mcp.tool()
async def get_paper_metadata(bibtex_key: str, ctx: Context = None) -> str:
    """Get structured metadata and summary for a specific paper.

    Args:
        bibtex_key: The paper's BibTeX key
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
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
    The summary must only contain information from the paper's actual text.
    Do not include claims from outside knowledge.

    Args:
        bibtex_key: The paper's BibTeX key
        summary: JSON string with the summary. Should include keys like
                 "main_contribution", "methodology", "findings", "limitations",
                 "section_summaries" (object mapping section names to summaries)
        keywords: Optional list of descriptive keywords for the paper
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        paper = await db.get_paper(bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    try:
        summary_data = json.loads(summary) if isinstance(summary, str) else summary
    except json.JSONDecodeError as exc:
        return f"Invalid JSON in summary: {exc}"

    # Update paper metadata (source of truth)
    paper.summary = summary_data
    paper.status = "ready"
    if keywords:
        paper.keywords = keywords
    await asyncio.to_thread(store.write_paper_metadata, paper)
    await asyncio.to_thread(store.write_summary_file, bibtex_key, summary_data)

    # Update index
    await db.store_summary(bibtex_key, summary_data)
    if keywords:
        await db.update_keywords(bibtex_key, keywords)
    await db.update_status(bibtex_key, "ready")
    await _push_paper(lc, bibtex_key)

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
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    results = await db.search_metadata(query, limit=limit)
    if not results:
        return f"No papers found matching '{query}' in library metadata/summaries."
    lines = []
    for paper in results:
        paper_tags = await db.get_paper_tags(paper.bibtex_key)
        tags_str = f" [{', '.join(paper_tags)}]" if paper_tags else ""
        citation = _format_citation(paper)
        entry = f"- **{citation}**: {paper.title}{tags_str} `{paper.bibtex_key}`"
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
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    fulltext_ready: asyncio.Event = lc["fulltext_ready"]

    if not fulltext_ready.is_set():
        return "Fulltext index is still building. Please try again in a moment."

    results = await db.search_fulltext(query, limit=limit)
    if not results:
        return f"No matches for '{query}' in paper full text."
    lines = []
    for r in results:
        paper = await db.get_paper(r["bibtex_key"])
        cite = _format_citation(paper) if paper else r["bibtex_key"]
        lines.append(f"**{cite}** `{r['bibtex_key']}`: ...{r['snippet']}...")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Paper listing
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_papers(
    status: str | None = None,
    tag: str | None = None,
    field: str | None = None,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """List papers in the library, optionally filtered by status, field, or tag.

    `field` and `tag` compose as AND — e.g. `field="finance"` with
    `tag="term-structure"` returns finance papers that also carry the
    term-structure concept. `field` requires the name to refer to a tag of
    kind='field' (use `list_tags(kind="field")` to see the options).

    Paper tags in the output are rendered as
    `[field1, field2 | concept1, concept2]` so the reader can tell fields
    from concepts at a glance.

    Args:
        status: Filter by status (downloading, converting, summarizing, ready, error)
        tag: Filter by tag name (any kind)
        field: Filter by field tag name (tag must have kind='field')
        limit: Maximum number of papers to return (default 50)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    papers = await db.list_papers(status=status, tag=tag, field=field, limit=limit)
    if not papers:
        return "No papers found in the library."
    vocab_kinds = {t.tag: t.kind for t in await db.list_tags()}
    lines = []
    for paper in papers:
        paper_tags = await db.get_paper_tags(paper.bibtex_key)
        fields = [t for t in paper_tags if vocab_kinds.get(t) == "field"]
        concepts = [t for t in paper_tags if vocab_kinds.get(t) != "field"]
        if fields and concepts:
            tags_str = f" [{', '.join(fields)} | {', '.join(concepts)}]"
        elif fields:
            tags_str = f" [{', '.join(fields)}]"
        elif concepts:
            tags_str = f" [{', '.join(concepts)}]"
        else:
            tags_str = ""
        citation = _format_citation(paper)
        lines.append(
            f"- **{citation}**: {paper.title}{tags_str} `{paper.bibtex_key}` [{paper.status}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tag management
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_tags(
    prefix: str | None = None,
    kind: str | None = None,
    ctx: Context = None,
) -> str:
    """Return tags in the vocabulary with paper counts.

    When `kind` is None (default), the output is grouped into a `## Fields`
    section followed by a `## Concepts` section, each sorted by paper count.
    When `kind` is specified ("field" or "concept"), output is a flat filtered
    list.

    Fields are broad disciplines (e.g. `finance`, `macroeconomics`). Concepts
    are narrower methods or subfields (e.g. `term-structure`,
    `causal-inference`). Every paper should carry 1-2 fields + a few concepts.

    Args:
        prefix: Optional prefix to filter tags by name (e.g., "climate")
        kind: Optional kind filter — "field" or "concept"
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    tags = await db.list_tags(prefix=prefix, kind=kind)
    if not tags:
        return "No tags in the vocabulary yet."

    def render(tag) -> str:
        desc = f" - {tag.description}" if tag.description else ""
        return f"- **{tag.tag}** ({tag.paper_count} papers){desc}"

    if kind is not None:
        return "\n".join(render(t) for t in tags)

    fields = [t for t in tags if t.kind == "field"]
    concepts = [t for t in tags if t.kind != "field"]
    sections: list[str] = []
    if fields:
        sections.append(
            "## Fields\n" + "\n".join(render(t) for t in fields)
        )
    if concepts:
        sections.append(
            "## Concepts\n" + "\n".join(render(t) for t in concepts)
        )
    return "\n\n".join(sections)


@mcp.tool()
async def add_tags(tags: str, ctx: Context = None) -> str:
    """Add new tags to the vocabulary.

    Prefer reusing existing tags over minting new ones. Call `find_similar_tags`
    on candidate new names first. This tool will still create near-duplicate
    tags (e.g. "graph-methods" alongside "graph-theory") but returns a warning
    listing the similar existing tags so you can reconsider before tagging a
    paper with the new one.

    Each tag object may include `"kind": "field" | "concept"` — defaults to
    "concept". Fields are broad disciplines; concepts are narrower methods or
    subfields. Most new additions should be concepts.

    Args:
        tags: JSON array of objects with 'tag' (required), 'description' (optional),
              and 'kind' (optional, defaults to "concept").
              Example: [{"tag": "causal-inference", "description": "..."},
                        {"tag": "micro-theory", "description": "...", "kind": "field"}]
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(tag_list, list):
        return "Expected a JSON array of tag objects."

    for entry in tag_list:
        if entry.get("kind") not in (None, "field", "concept"):
            return (
                f"Invalid kind for '{entry.get('tag')}': "
                f"must be 'field' or 'concept'."
            )

    existing_tags = await asyncio.to_thread(store.read_tags)
    existing_tag_names = {t["tag"] for t in existing_tags}

    added: list[str] = []
    skipped_duplicates: list[str] = []
    warnings: list[str] = []
    vocabulary_names = list(existing_tag_names)
    tag_counts = {t.tag: t.paper_count for t in await db.list_tags()}

    for new_tag in tag_list:
        name = new_tag["tag"]
        if name in existing_tag_names:
            skipped_duplicates.append(name)
            continue
        entry = dict(new_tag)
        entry.setdefault("kind", "concept")
        existing_tags.append(entry)
        existing_tag_names.add(name)
        added.append(name)
        matches = similar_tags(name, vocabulary_names)
        if matches:
            rendered = ", ".join(
                f"{m} ({tag_counts.get(m, 0)})" for m in matches[:5]
            )
            warnings.append(f"- **{name}** is close to: {rendered}")

    if added:
        await asyncio.to_thread(store.write_tags, existing_tags)
        await db.add_tags([t for t in tag_list if t["tag"] in added])
        await _push_tags(lc)

    lines: list[str] = []
    if added:
        lines.append(f"Added tags: {', '.join(added)}")
    if skipped_duplicates:
        lines.append(f"Already in vocabulary: {', '.join(skipped_duplicates)}")
    if warnings:
        lines.append(
            "\nWarning: some new tags look similar to existing ones. "
            "Consider reusing the existing tag before calling tag_paper."
        )
        lines.extend(warnings)
    if not lines:
        lines.append("No tags provided.")
    return "\n".join(lines)


@mcp.tool()
async def find_similar_tags(
    candidates: str, max_edit_distance: int = 3, ctx: Context = None
) -> str:
    """Check a list of candidate tag names against the existing vocabulary.

    Use this before calling `add_tags` to see whether a proposed new tag would
    duplicate something already present. Flags entries that share a
    non-stopword kebab-case token with the candidate, or that are within the
    given Levenshtein distance. Paper counts are included so you can prefer
    well-established tags.

    Args:
        candidates: JSON array of candidate tag names, e.g. ["graph-methods", "bayes"]
        max_edit_distance: Maximum Levenshtein distance to flag as similar (default 3)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]

    try:
        name_list = json.loads(candidates) if isinstance(candidates, str) else candidates
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(name_list, list) or not all(isinstance(n, str) for n in name_list):
        return "Expected a JSON array of candidate tag name strings."

    vocab = await db.list_tags()
    vocab_names = [t.tag for t in vocab]
    counts = {t.tag: t.paper_count for t in vocab}

    lines: list[str] = []
    for candidate in name_list:
        if candidate in counts:
            lines.append(
                f"**{candidate}** — exact match in vocabulary "
                f"({counts[candidate]} papers). Reuse it."
            )
            continue
        matches = similar_tags(
            candidate, vocab_names, max_edit_distance=max_edit_distance
        )
        if not matches:
            lines.append(f"**{candidate}** — no close matches; safe to add.")
            continue
        rendered = ", ".join(f"{m} ({counts.get(m, 0)})" for m in matches[:6])
        lines.append(f"**{candidate}** — similar to: {rendered}")
    return "\n".join(lines)


@mcp.tool()
async def tag_paper(bibtex_key: str, tags: str, ctx: Context = None) -> str:
    """Associate tags with a paper. Tags must exist in the vocabulary first (use add_tags).

    Args:
        bibtex_key: The paper's BibTeX key
        tags: JSON array of tag names, e.g. ["causal-inference", "macro"]
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
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

    # Update source of truth: append new tags to paper's tag list
    current_tags = set(paper.tags)
    for tag_name in tag_list:
        current_tags.add(tag_name)
    paper.tags = sorted(current_tags)
    await asyncio.to_thread(store.write_paper_metadata, paper)

    # Update index
    await db.tag_paper(bibtex_key, tag_list)
    await _push_paper(lc, bibtex_key)

    return f"Tagged **{bibtex_key}** with: {', '.join(tag_list)}"


@mcp.tool()
async def untag_paper(bibtex_key: str, tags: str, ctx: Context = None) -> str:
    """Remove one or more tags from a single paper.

    Leaves the tags intact in the vocabulary and on other papers — use
    `delete_tag`, `rename_tag`, or `merge_tags` for vocabulary-wide changes.

    Args:
        bibtex_key: The paper's BibTeX key
        tags: JSON array of tag names to remove, e.g. ["macro", "finance"]
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(tag_list, list) or not all(isinstance(t, str) for t in tag_list):
        return "Expected a JSON array of tag names."

    paper = await asyncio.to_thread(store.read_paper_metadata, bibtex_key)
    if paper is None:
        return f"No paper found with key: {bibtex_key}"

    changed = await asyncio.to_thread(
        store.remove_tags_from_paper, bibtex_key, set(tag_list)
    )
    if not changed:
        return f"**{bibtex_key}** already had none of: {', '.join(tag_list)}"

    await db.remove_paper_tags(bibtex_key, tag_list)
    await _push_paper(lc, bibtex_key)
    return f"Removed {', '.join(tag_list)} from **{bibtex_key}**."


@mcp.tool()
async def rename_tag(
    old: str,
    new: str,
    description: str | None = None,
    ctx: Context = None,
) -> str:
    """Rename a tag across the vocabulary and every paper that uses it.

    If `new` doesn't exist yet it is created (inheriting `old`'s description
    unless an override is provided). If `new` already exists the rename
    degenerates into a merge of `old` into `new`, and the supplied description
    (if any) replaces the existing one.

    Args:
        old: Current tag name
        new: Desired tag name
        description: Optional description for the (possibly new) tag
    """
    if old == new:
        return "Old and new tag names are identical; nothing to do."

    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    vocab = await asyncio.to_thread(store.read_tags)
    old_entry = next((t for t in vocab if t["tag"] == old), None)
    if old_entry is None:
        return f"Tag '{old}' is not in the vocabulary."

    resolved_description = description
    if resolved_description is None:
        resolved_description = old_entry.get("description")

    await asyncio.to_thread(
        store.upsert_tag_in_vocab, new, resolved_description
    )
    await db.upsert_tag(new, resolved_description)

    affected = await asyncio.to_thread(store.apply_tag_rewrite, {old: new})
    await db.apply_tag_rewrite({old: new})

    await asyncio.to_thread(store.remove_tags_from_vocab, {old})
    await db.delete_tags_from_vocab([old])

    await _push_tags(lc)
    if affected:
        await _push_all_papers(lc)

    return (
        f"Renamed '{old}' -> '{new}' across {len(affected)} paper(s)."
        if affected
        else f"Renamed '{old}' -> '{new}' (no papers used the old tag)."
    )


@mcp.tool()
async def merge_tags(
    sources: str,
    target: str,
    description: str | None = None,
    ctx: Context = None,
) -> str:
    """Fold one or more source tags into a single target tag.

    Every paper tagged with any source tag gets `target` instead; the source
    tags are then removed from the vocabulary. If `target` doesn't already
    exist, it is created (with the given description, or left empty).

    Args:
        sources: JSON array of tag names to merge away, e.g. ["graph-theory", "graph-methods"]
        target: The tag to keep. Papers from all sources end up with this tag.
        description: Optional description for the target tag (only used if the tag is new)
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    try:
        source_list = json.loads(sources) if isinstance(sources, str) else sources
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    if not isinstance(source_list, list) or not all(
        isinstance(s, str) for s in source_list
    ):
        return "Expected a JSON array of source tag names."
    if not source_list:
        return "No source tags provided."
    if target in source_list:
        return f"Target '{target}' cannot also be a source."

    vocab = await asyncio.to_thread(store.read_tags)
    vocab_names = {t["tag"] for t in vocab}
    real_sources = [s for s in source_list if s in vocab_names]
    unknown = [s for s in source_list if s not in vocab_names]
    if not real_sources:
        return f"None of those sources exist in the vocabulary: {', '.join(source_list)}"

    await asyncio.to_thread(store.upsert_tag_in_vocab, target, description)
    await db.upsert_tag(target, description)

    mapping = {src: target for src in real_sources}
    affected = await asyncio.to_thread(store.apply_tag_rewrite, mapping)
    await db.apply_tag_rewrite(mapping)

    await asyncio.to_thread(store.remove_tags_from_vocab, set(real_sources))
    await db.delete_tags_from_vocab(real_sources)

    await _push_tags(lc)
    if affected:
        await _push_all_papers(lc)

    lines = [
        f"Merged {', '.join(real_sources)} -> '{target}' "
        f"across {len(affected)} paper(s)."
    ]
    if unknown:
        lines.append(f"Ignored (not in vocabulary): {', '.join(unknown)}")
    return "\n".join(lines)


@mcp.tool()
async def delete_tag(
    tag: str, force: bool = False, ctx: Context = None
) -> str:
    """Remove a tag from the vocabulary.

    By default, refuses to delete a tag that's still applied to papers —
    run `merge_tags` into a better tag, or call with `force=True` to strip
    the tag from every paper before removing it.

    Args:
        tag: Tag name to delete
        force: If True, strip the tag from all papers first. Default False.
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    vocab = await asyncio.to_thread(store.read_tags)
    if not any(t["tag"] == tag for t in vocab):
        return f"Tag '{tag}' is not in the vocabulary."

    tag_entries = await db.list_tags()
    paper_count = next((t.paper_count for t in tag_entries if t.tag == tag), 0)

    if paper_count > 0 and not force:
        return (
            f"Tag '{tag}' is still applied to {paper_count} paper(s). "
            f"Use merge_tags to fold it into another tag, or call "
            f"delete_tag again with force=True to strip it from every paper."
        )

    affected: list[str] = []
    if paper_count > 0:
        affected = await asyncio.to_thread(store.apply_tag_rewrite, {tag: None})
        await db.apply_tag_rewrite({tag: None})

    await asyncio.to_thread(store.remove_tags_from_vocab, {tag})
    await db.delete_tags_from_vocab([tag])

    await _push_tags(lc)
    if affected:
        await _push_all_papers(lc)

    suffix = f" (stripped from {len(affected)} paper(s))" if affected else ""
    return f"Deleted tag '{tag}'{suffix}."


@mcp.tool()
async def prune_tags(dry_run: bool = True, ctx: Context = None) -> str:
    """Remove tags from the vocabulary that have zero papers.

    Args:
        dry_run: If True (default), only report what would be pruned.
                 Set to False to actually remove them.
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    tag_entries = await db.list_tags()
    orphans = [t.tag for t in tag_entries if t.paper_count == 0]
    if not orphans:
        return "No orphan tags to prune."

    if dry_run:
        return (
            f"Would prune {len(orphans)} orphan tag(s): "
            f"{', '.join(orphans)}\n\n"
            "Call again with dry_run=False to remove them."
        )

    await asyncio.to_thread(store.remove_tags_from_vocab, set(orphans))
    await db.delete_tags_from_vocab(orphans)
    await _push_tags(lc)
    return f"Pruned {len(orphans)} orphan tag(s): {', '.join(orphans)}"


@mcp.tool()
async def set_tag_kind(tag: str, kind: str, ctx: Context = None) -> str:
    """Promote a concept tag to a field (or demote a field to a concept).

    Fields are broad disciplines used to group concepts; concepts are the
    narrower method/subfield labels that sit underneath a field. Both kinds
    live in the same `tags` table, distinguished only by this attribute.

    Args:
        tag: Existing tag name
        kind: Either "field" or "concept"
    """
    if kind not in {"field", "concept"}:
        return "kind must be 'field' or 'concept'."

    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    vocab_changed = await asyncio.to_thread(
        store.set_tag_kind_in_vocab, tag, kind
    )
    db_changed = await db.set_tag_kind(tag, kind)
    if not db_changed and not vocab_changed:
        return f"Tag '{tag}' is not in the vocabulary."
    if vocab_changed:
        await _push_tags(lc)
    return f"Set '{tag}' to kind='{kind}'."


# ---------------------------------------------------------------------------
# Paper management
# ---------------------------------------------------------------------------


@mcp.tool()
async def delete_paper(bibtex_key: str, ctx: Context = None) -> str:
    """Delete a paper from the library entirely (both files and index).

    Args:
        bibtex_key: The paper's BibTeX key
    """
    lc = _get_context(ctx)
    await _ensure_synced(lc)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    deleted_from_index = await db.delete_paper(bibtex_key)
    deleted_from_store = await asyncio.to_thread(store.delete_paper_dir, bibtex_key)

    if not deleted_from_index and not deleted_from_store:
        return f"No paper found with key: {bibtex_key}"
    await sync_delete(lc["remote"], f"papers/{bibtex_key}")
    return f"Deleted **{bibtex_key}** from the library."


# ---------------------------------------------------------------------------
# Index rebuild (replaces sync)
# ---------------------------------------------------------------------------


@mcp.tool()
async def rebuild_index(ctx: Context = None) -> str:
    """Force a full rescan of the paper library and rebuild the search index.

    Use this if the index seems stale or after manually adding/modifying files.
    """
    lc = _get_context(ctx)
    db: PaperDatabase = lc["db"]
    store: PaperStore = lc["store"]

    await db.initialize()

    papers = await asyncio.to_thread(store.scan_all_papers)
    tags = await asyncio.to_thread(store.read_tags)
    await db.rebuild_from_papers(papers, tags)

    paper_texts = []
    for paper in papers:
        content = await asyncio.to_thread(store.read_paper_markdown, paper.bibtex_key)
        if content:
            paper_texts.append((paper.bibtex_key, content))
    if paper_texts:
        await db.rebuild_fulltext(paper_texts)

    fulltext_ready: asyncio.Event = lc["fulltext_ready"]
    fulltext_ready.set()

    return (
        f"Index rebuilt: {len(papers)} papers, {len(tags)} tags, "
        f"{len(paper_texts)} fulltext entries."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
