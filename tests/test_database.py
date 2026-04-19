import pytest
import pytest_asyncio

from papertrail.database import PaperDatabase
from papertrail.models import PaperMetadata


@pytest_asyncio.fixture
async def db(tmp_path):
    database = PaperDatabase(tmp_path / "index.db")
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
        tags=[],
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


@pytest.mark.asyncio
async def test_rebuild_from_papers(db):
    papers = [
        _make_paper(bibtex_key="a_2024_one", tags=["macro"]),
        _make_paper(bibtex_key="b_2024_two", tags=["macro", "finance"]),
    ]
    tags = [
        {"tag": "macro", "description": "Macroeconomics"},
        {"tag": "finance", "description": "Finance"},
    ]
    await db.rebuild_from_papers(papers, tags)

    listed = await db.list_papers()
    assert len(listed) == 2

    all_tags = await db.list_tags()
    macro_tag = next(t for t in all_tags if t.tag == "macro")
    assert macro_tag.paper_count == 2
    finance_tag = next(t for t in all_tags if t.tag == "finance")
    assert finance_tag.paper_count == 1

    a_tags = await db.get_paper_tags("a_2024_one")
    assert a_tags == ["macro"]
    b_tags = await db.get_paper_tags("b_2024_two")
    assert b_tags == ["finance", "macro"]


@pytest.mark.asyncio
async def test_rebuild_fulltext(db):
    await db.upsert_paper(_make_paper(bibtex_key="a_2024_one"))
    await db.upsert_paper(_make_paper(bibtex_key="b_2024_two"))

    paper_texts = [
        ("a_2024_one", "This paper discusses instrumental variables."),
        ("b_2024_two", "We study climate risk and carbon pricing."),
    ]
    await db.rebuild_fulltext(paper_texts)

    results = await db.search_fulltext("instrumental variables")
    assert len(results) == 1
    assert results[0]["bibtex_key"] == "a_2024_one"

    results = await db.search_fulltext("carbon pricing")
    assert len(results) == 1
    assert results[0]["bibtex_key"] == "b_2024_two"


async def _seed_tagged(db, bibtex_key: str, tags: list[str]) -> None:
    """Insert a paper with the given tags and keep the tags table in sync."""
    paper = _make_paper(bibtex_key=bibtex_key, tags=tags)
    for name in tags:
        await db.add_tags([{"tag": name, "description": None}])
    await db.upsert_paper(paper)


@pytest.mark.asyncio
async def test_remove_paper_tags_recomputes_counts(db):
    await _seed_tagged(db, "a_2024_one", ["macro", "finance"])
    await _seed_tagged(db, "b_2024_two", ["macro"])

    await db.remove_paper_tags("a_2024_one", ["macro"])

    counts = {t.tag: t.paper_count for t in await db.list_tags()}
    assert counts["macro"] == 1
    assert counts["finance"] == 1
    assert await db.get_paper_tags("a_2024_one") == ["finance"]


@pytest.mark.asyncio
async def test_apply_tag_rewrite_renames(db):
    await _seed_tagged(db, "a_2024_one", ["old-tag"])
    await _seed_tagged(db, "b_2024_two", ["old-tag", "other"])

    await db.apply_tag_rewrite({"old-tag": "new-tag"})

    counts = {t.tag: t.paper_count for t in await db.list_tags()}
    assert counts.get("new-tag") == 2
    assert counts.get("old-tag") == 0
    assert await db.get_paper_tags("a_2024_one") == ["new-tag"]
    assert set(await db.get_paper_tags("b_2024_two")) == {"new-tag", "other"}


@pytest.mark.asyncio
async def test_apply_tag_rewrite_merges_without_double_counting(db):
    await _seed_tagged(db, "shared_paper", ["graph-theory", "graph-methods"])

    await db.apply_tag_rewrite({"graph-methods": "graph-theory"})

    tags = await db.get_paper_tags("shared_paper")
    assert tags == ["graph-theory"]
    counts = {t.tag: t.paper_count for t in await db.list_tags()}
    assert counts["graph-theory"] == 1


@pytest.mark.asyncio
async def test_apply_tag_rewrite_strips_when_target_is_none(db):
    await _seed_tagged(db, "a_2024_one", ["macro", "finance"])
    await db.apply_tag_rewrite({"finance": None})
    assert await db.get_paper_tags("a_2024_one") == ["macro"]
    counts = {t.tag: t.paper_count for t in await db.list_tags()}
    assert counts.get("finance", 0) == 0


@pytest.mark.asyncio
async def test_delete_tags_from_vocab(db):
    await db.add_tags([{"tag": "macro", "description": None}])
    await db.delete_tags_from_vocab(["macro"])
    assert all(t.tag != "macro" for t in await db.list_tags())


@pytest.mark.asyncio
async def test_upsert_tag_inserts_and_updates(db):
    await db.upsert_tag("macro", "Macroeconomics")
    tags = await db.list_tags()
    assert any(t.tag == "macro" and t.description == "Macroeconomics" for t in tags)

    await db.upsert_tag("macro", "Updated")
    tags = await db.list_tags()
    assert any(t.tag == "macro" and t.description == "Updated" for t in tags)


@pytest.mark.asyncio
async def test_prune_empty_tags(db):
    await _seed_tagged(db, "a_2024_one", ["used"])
    await db.add_tags([{"tag": "orphan", "description": None}])

    removed = await db.prune_empty_tags()
    assert removed == ["orphan"]
    remaining = {t.tag for t in await db.list_tags()}
    assert remaining == {"used"}
