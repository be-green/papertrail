"""Tests for server-layer helpers: auto-download, conversion orchestration."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from papertrail.config import PapertrailConfig
from papertrail.database import PaperDatabase
from papertrail.models import PaperMetadata, SearchResult
from papertrail.paper_store import PaperStore
from papertrail.server import (
    _background_auto_download,
    _save_search_result_as_paper,
    _start_conversion,
)
from papertrail.metadata import DownloadResult, MetadataFetcher


@pytest.fixture
def config(tmp_path):
    return PapertrailConfig(
        data_dir=tmp_path / "data",
        index_dir=tmp_path / "cache",
    )


@pytest.fixture
def store(config):
    config.ensure_directories()
    return PaperStore(config)


@pytest.fixture
async def db(config):
    database = PaperDatabase(config.index_db_path)
    await database.initialize()
    yield database
    await database.close()


def _make_paper(**overrides) -> PaperMetadata:
    defaults = dict(
        bibtex_key="smith_2024_causal",
        title="Causal Inference in Economics",
        authors=["John Smith"],
        year=2024,
        status="pending_pdf",
    )
    defaults.update(overrides)
    return PaperMetadata(**defaults)


def _make_lc(config, store, db, fetcher_mock, converter_mock):
    return {
        "config": config,
        "store": store,
        "db": db,
        "fetcher": fetcher_mock,
        "converter": converter_mock,
        "remote": "",
        "sync_state": {"last_pull_time": 0.0},
    }


class TestBackgroundAutoDownload:
    @pytest.mark.asyncio
    async def test_success_triggers_conversion(self, config, store, db):
        paper = _make_paper()
        store.write_paper_metadata(paper)
        await db.upsert_paper(paper)

        pdf_path = config.papers_dir / paper.bibtex_key / "paper.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        async def fake_download(result, dest):
            dest.write_bytes(b"%PDF-1.4 fake")
            return DownloadResult(success=True, pdf_path=dest)

        fetcher = MagicMock()
        fetcher.download_pdf = AsyncMock(side_effect=fake_download)

        converter = MagicMock()
        converter.convert = AsyncMock(return_value="# Paper content")

        lc = _make_lc(config, store, db, fetcher, converter)

        await _background_auto_download(lc, paper.bibtex_key)
        # _start_conversion spawns its own background task; wait for it
        import asyncio as _asyncio
        pending = [t for t in _asyncio.all_tasks() if not t.done() and t is not _asyncio.current_task()]
        if pending:
            await _asyncio.gather(*pending, return_exceptions=True)

        fetcher.download_pdf.assert_awaited_once()
        assert pdf_path.exists()
        stored = store.read_paper_metadata(paper.bibtex_key)
        assert stored.status == "summarizing"

    @pytest.mark.asyncio
    async def test_failure_leaves_pending_pdf(self, config, store, db):
        paper = _make_paper()
        store.write_paper_metadata(paper)
        await db.upsert_paper(paper)

        async def fake_download(result, dest):
            return DownloadResult(success=False)

        fetcher = MagicMock()
        fetcher.download_pdf = AsyncMock(side_effect=fake_download)
        converter = MagicMock()
        converter.convert = AsyncMock()

        lc = _make_lc(config, store, db, fetcher, converter)

        await _background_auto_download(lc, paper.bibtex_key)

        fetcher.download_pdf.assert_awaited_once()
        converter.convert.assert_not_awaited()
        stored = store.read_paper_metadata(paper.bibtex_key)
        assert stored.status == "pending_pdf"

    @pytest.mark.asyncio
    async def test_crash_is_swallowed(self, config, store, db):
        """Background task must never raise — it's spawned fire-and-forget."""
        paper = _make_paper()
        store.write_paper_metadata(paper)
        await db.upsert_paper(paper)

        fetcher = MagicMock()
        fetcher.download_pdf = AsyncMock(side_effect=RuntimeError("boom"))
        converter = MagicMock()
        lc = _make_lc(config, store, db, fetcher, converter)

        # Should not raise
        await _background_auto_download(lc, paper.bibtex_key)
        stored = store.read_paper_metadata(paper.bibtex_key)
        assert stored.status == "pending_pdf"

    @pytest.mark.asyncio
    async def test_missing_paper_is_noop(self, config, store, db):
        fetcher = MagicMock()
        fetcher.download_pdf = AsyncMock()
        converter = MagicMock()
        lc = _make_lc(config, store, db, fetcher, converter)

        await _background_auto_download(lc, "does_not_exist")
        fetcher.download_pdf.assert_not_awaited()


class TestSaveSearchResultAsPaper:
    """Covers the working-paper path: manual SearchResult → paper on disk + index."""

    @pytest.mark.asyncio
    async def test_minimal_manual_metadata_persists_paper(self, config, store, db):
        fetcher = MetadataFetcher(config)
        try:
            lc = _make_lc(config, store, db, fetcher, MagicMock())
            result = SearchResult(
                title="A Working Paper With No DOI",
                authors=["Alice Smith", "Bob Jones"],
                year=2026,
                source="manual",
            )
            response = await _save_search_result_as_paper(lc, result, auto_download=False)
        finally:
            await fetcher.close()

        assert "smith_2026_working" in response
        stored = store.read_paper_metadata("smith_2026_working")
        assert stored is not None
        assert stored.title == "A Working Paper With No DOI"
        assert stored.authors == ["Alice Smith", "Bob Jones"]
        assert stored.year == 2026
        assert stored.status == "pending_pdf"
        assert stored.doi is None
        assert "download_paper" in response

    @pytest.mark.asyncio
    async def test_auto_download_false_does_not_spawn_task(self, config, store, db):
        fetcher = MagicMock()
        fetcher.download_pdf = AsyncMock()
        fetcher.generate_unique_key = AsyncMock(return_value="doe_2025_novel")
        converter = MagicMock()
        lc = _make_lc(config, store, db, fetcher, converter)
        result = SearchResult(
            title="Novel Draft",
            authors=["Jane Doe"],
            year=2025,
            source="manual",
        )

        await _save_search_result_as_paper(lc, result, auto_download=False)

        # No auto-download was triggered — fetcher.download_pdf should not be scheduled.
        # Give any (erroneously spawned) background task a chance to run.
        import asyncio as _asyncio
        await _asyncio.sleep(0)
        fetcher.download_pdf.assert_not_awaited()


class TestStartConversion:
    @pytest.mark.asyncio
    async def test_conversion_failure_sets_error_status(self, config, store, db):
        paper = _make_paper()
        store.write_paper_metadata(paper)
        await db.upsert_paper(paper)

        pdf_path = config.papers_dir / paper.bibtex_key / "paper.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        converter = MagicMock()
        converter.convert = AsyncMock(side_effect=RuntimeError("conversion blew up"))
        fetcher = MagicMock()

        lc = _make_lc(config, store, db, fetcher, converter)

        await _start_conversion(lc, paper.bibtex_key, pdf_path)
        import asyncio as _asyncio
        pending = [t for t in _asyncio.all_tasks() if not t.done() and t is not _asyncio.current_task()]
        if pending:
            await _asyncio.gather(*pending, return_exceptions=True)

        stored = store.read_paper_metadata(paper.bibtex_key)
        assert stored.status == "error"
