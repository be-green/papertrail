import json
import pytest

from papertrail.config import PapertrailConfig
from papertrail.models import PaperMetadata
from papertrail.paper_store import PaperStore


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


def _make_paper(**overrides) -> PaperMetadata:
    defaults = dict(
        bibtex_key="smith_2024_causal",
        title="Causal Inference in Economics",
        authors=["John Smith", "Jane Doe"],
        year=2024,
        abstract="We study causal inference methods.",
        status="ready",
        tags=["macro"],
        keywords=["causal"],
    )
    defaults.update(overrides)
    return PaperMetadata(**defaults)


class TestReadWriteMetadata:
    def test_write_and_read(self, store):
        paper = _make_paper()
        store.write_paper_metadata(paper)
        loaded = store.read_paper_metadata("smith_2024_causal")
        assert loaded is not None
        assert loaded.bibtex_key == "smith_2024_causal"
        assert loaded.title == "Causal Inference in Economics"
        assert loaded.tags == ["macro"]
        assert loaded.status == "ready"

    def test_read_nonexistent(self, store):
        assert store.read_paper_metadata("nonexistent") is None

    def test_write_creates_directory(self, store, config):
        paper = _make_paper(bibtex_key="new_2024_paper")
        store.write_paper_metadata(paper)
        assert (config.papers_dir / "new_2024_paper" / "metadata.json").exists()

    def test_overwrite_existing(self, store):
        paper = _make_paper()
        store.write_paper_metadata(paper)
        paper.title = "Updated Title"
        store.write_paper_metadata(paper)
        loaded = store.read_paper_metadata("smith_2024_causal")
        assert loaded.title == "Updated Title"


class TestSummaryFile:
    def test_write_summary(self, store, config):
        paper = _make_paper()
        store.write_paper_metadata(paper)
        summary = {"main_contribution": "Key finding"}
        store.write_summary_file("smith_2024_causal", summary)
        summary_path = config.papers_dir / "smith_2024_causal" / "summary.json"
        assert summary_path.exists()
        loaded = json.loads(summary_path.read_text())
        assert loaded == summary


class TestTags:
    def test_write_and_read_tags(self, store):
        tags = [
            {"tag": "macro", "description": "Macroeconomics"},
            {"tag": "finance", "description": "Finance papers"},
        ]
        store.write_tags(tags)
        loaded = store.read_tags()
        assert len(loaded) == 2
        assert loaded[0]["tag"] == "macro"

    def test_read_empty_tags(self, store):
        assert store.read_tags() == []


class TestScanAllPapers:
    def test_scans_multiple_papers(self, store):
        store.write_paper_metadata(_make_paper(bibtex_key="a_2024_one"))
        store.write_paper_metadata(_make_paper(bibtex_key="b_2024_two"))
        papers = store.scan_all_papers()
        keys = {p.bibtex_key for p in papers}
        assert keys == {"a_2024_one", "b_2024_two"}

    def test_scan_empty_library(self, store):
        assert store.scan_all_papers() == []

    def test_skips_invalid_json(self, store, config):
        store.write_paper_metadata(_make_paper(bibtex_key="good_paper"))
        bad_dir = config.papers_dir / "bad_paper"
        bad_dir.mkdir(parents=True)
        (bad_dir / "metadata.json").write_text("not json")
        papers = store.scan_all_papers()
        assert len(papers) == 1
        assert papers[0].bibtex_key == "good_paper"


class TestPaperDirExists:
    def test_exists(self, store):
        store.write_paper_metadata(_make_paper())
        assert store.paper_dir_exists("smith_2024_causal") is True

    def test_not_exists(self, store):
        assert store.paper_dir_exists("nonexistent") is False


class TestReadPaperMarkdown:
    def test_read_existing(self, store, config):
        paper_dir = config.papers_dir / "smith_2024_causal"
        paper_dir.mkdir(parents=True)
        (paper_dir / "paper.md").write_text("# Test Paper\n\nContent here.")
        content = store.read_paper_markdown("smith_2024_causal")
        assert content == "# Test Paper\n\nContent here."

    def test_read_nonexistent(self, store):
        assert store.read_paper_markdown("nonexistent") is None


class TestDeletePaperDir:
    def test_delete_existing(self, store, config):
        store.write_paper_metadata(_make_paper())
        assert store.paper_dir_exists("smith_2024_causal")
        result = store.delete_paper_dir("smith_2024_causal")
        assert result is True
        assert not store.paper_dir_exists("smith_2024_causal")

    def test_delete_nonexistent(self, store):
        result = store.delete_paper_dir("nonexistent")
        assert result is False
