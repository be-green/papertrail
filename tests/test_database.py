import pytest
import pytest_asyncio

from papertrail.database import PaperDatabase
from papertrail.models import PaperMetadata


@pytest_asyncio.fixture
async def db(tmp_path):
    database = PaperDatabase(tmp_path / "test.db")
    await database.initialize()
    yield database
    await database.close()


def _make_paper(**overrides) -> PaperMetadata:
    defaults = dict(
        bibtex_key="smith_2024_causal",
        title="Causal Inference in Economics",
        authors=["John Smith", "Jane Doe"],
        year=2024,
        abstract="We study causal inference methods in economic research.",
        journal="American Economic Review",
        doi="10.1257/aer.2024.001",
        arxiv_id=None,
        ssrn_id=None,
        url="https://example.com/paper",
        topics=["causal inference", "economics"],
        keywords=["instrumental variables", "regression discontinuity"],
        fields_of_study=["Economics"],
        citation_count=42,
        added_date="2024-01-15T00:00:00Z",
        status="ready",
        summary={"main_contribution": "A survey of causal methods"},
    )
    defaults.update(overrides)
    return PaperMetadata(**defaults)


@pytest.mark.asyncio
async def test_initialize_creates_tables(db):
    # Verify tables exist by exercising them (no direct connection access
    # since sqlite3 enforces thread affinity)
    papers = await db.list_papers()
    assert papers == []
    tags = await db.list_tags()
    assert tags == []


@pytest.mark.asyncio
async def test_upsert_and_get_paper(db):
    paper = _make_paper()
    await db.upsert_paper(paper)
    retrieved = await db.get_paper("smith_2024_causal")
    assert retrieved is not None
    assert retrieved.title == "Causal Inference in Economics"
    assert retrieved.authors == ["John Smith", "Jane Doe"]
    assert retrieved.year == 2024
    assert retrieved.summary == {"main_contribution": "A survey of causal methods"}


@pytest.mark.asyncio
async def test_upsert_replaces_existing(db):
    paper = _make_paper()
    await db.upsert_paper(paper)
    updated = _make_paper(title="Updated Title", citation_count=100)
    await db.upsert_paper(updated)
    retrieved = await db.get_paper("smith_2024_causal")
    assert retrieved.title == "Updated Title"
    assert retrieved.citation_count == 100


@pytest.mark.asyncio
async def test_get_nonexistent_paper(db):
    result = await db.get_paper("nonexistent_key")
    assert result is None


@pytest.mark.asyncio
async def test_list_papers_empty(db):
    papers = await db.list_papers()
    assert papers == []


@pytest.mark.asyncio
async def test_list_papers_with_status_filter(db):
    await db.upsert_paper(_make_paper(bibtex_key="a_2024_one", status="ready"))
    await db.upsert_paper(_make_paper(bibtex_key="b_2024_two", status="converting"))
    ready = await db.list_papers(status="ready")
    assert len(ready) == 1
    assert ready[0].bibtex_key == "a_2024_one"


@pytest.mark.asyncio
async def test_list_papers_with_tag_filter(db):
    await db.upsert_paper(_make_paper(bibtex_key="a_2024_one"))
    await db.upsert_paper(_make_paper(bibtex_key="b_2024_two"))
    await db.add_tags([{"tag": "macro", "description": "Macroeconomics"}])
    await db.tag_paper("a_2024_one", ["macro"])
    tagged = await db.list_papers(tag="macro")
    assert len(tagged) == 1
    assert tagged[0].bibtex_key == "a_2024_one"


@pytest.mark.asyncio
async def test_update_status(db):
    await db.upsert_paper(_make_paper())
    await db.update_status("smith_2024_causal", "error")
    paper = await db.get_paper("smith_2024_causal")
    assert paper.status == "error"


@pytest.mark.asyncio
async def test_store_summary(db):
    await db.upsert_paper(_make_paper(summary=None))
    new_summary = {"main_contribution": "New findings", "sections": ["Intro", "Model"]}
    await db.store_summary("smith_2024_causal", new_summary)
    paper = await db.get_paper("smith_2024_causal")
    assert paper.summary == new_summary


@pytest.mark.asyncio
async def test_update_keywords(db):
    await db.upsert_paper(_make_paper())
    await db.update_keywords("smith_2024_causal", ["new-keyword", "another"])
    paper = await db.get_paper("smith_2024_causal")
    assert paper.keywords == ["new-keyword", "another"]


@pytest.mark.asyncio
async def test_search_metadata_fts(db):
    await db.upsert_paper(_make_paper(
        bibtex_key="smith_2024_causal",
        title="Causal Inference in Economics",
    ))
    await db.upsert_paper(_make_paper(
        bibtex_key="jones_2023_climate",
        title="Climate Risk and Asset Pricing",
        abstract="We study how climate risk affects asset prices.",
        topics=["climate finance"],
        keywords=["climate risk", "asset pricing"],
    ))
    results = await db.search_metadata("causal")
    assert len(results) >= 1
    assert results[0].bibtex_key == "smith_2024_causal"

    results = await db.search_metadata("climate")
    assert len(results) >= 1
    assert results[0].bibtex_key == "jones_2023_climate"


@pytest.mark.asyncio
async def test_search_fulltext(db):
    await db.upsert_paper(_make_paper())
    await db.index_fulltext("smith_2024_causal", "This paper discusses regression discontinuity designs in detail.")
    results = await db.search_fulltext("regression discontinuity")
    assert len(results) >= 1
    assert results[0]["bibtex_key"] == "smith_2024_causal"


@pytest.mark.asyncio
async def test_tag_operations(db):
    await db.upsert_paper(_make_paper())

    # Add tags
    await db.add_tags([
        {"tag": "causal-inference", "description": "Papers on causal methods"},
        {"tag": "macro", "description": "Macroeconomics"},
    ])
    tags = await db.list_tags()
    assert len(tags) == 2
    assert all(t.paper_count == 0 for t in tags)

    # Tag a paper
    await db.tag_paper("smith_2024_causal", ["causal-inference"])
    paper_tags = await db.get_paper_tags("smith_2024_causal")
    assert paper_tags == ["causal-inference"]

    # Check counts updated
    tags = await db.list_tags()
    ci_tag = next(t for t in tags if t.tag == "causal-inference")
    assert ci_tag.paper_count == 1

    # Filter by prefix
    tags = await db.list_tags(prefix="causal")
    assert len(tags) == 1


@pytest.mark.asyncio
async def test_check_bibtex_key_exists(db):
    assert not await db.check_bibtex_key_exists("smith_2024_causal")
    await db.upsert_paper(_make_paper())
    assert await db.check_bibtex_key_exists("smith_2024_causal")
