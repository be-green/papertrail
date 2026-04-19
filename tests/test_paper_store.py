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


class TestRemoveTagsFromPaper:
    def test_removes_specified_tags(self, store):
        store.write_paper_metadata(
            _make_paper(tags=["macro", "finance", "causal"])
        )
        changed = store.remove_tags_from_paper(
            "smith_2024_causal", {"finance", "causal"}
        )
        assert changed is True
        loaded = store.read_paper_metadata("smith_2024_causal")
        assert loaded.tags == ["macro"]

    def test_noop_when_tag_missing(self, store):
        store.write_paper_metadata(_make_paper(tags=["macro"]))
        changed = store.remove_tags_from_paper(
            "smith_2024_causal", {"does-not-exist"}
        )
        assert changed is False

    def test_returns_false_for_missing_paper(self, store):
        assert store.remove_tags_from_paper("nope", {"macro"}) is False


class TestApplyTagRewrite:
    def test_rename_across_papers(self, store):
        store.write_paper_metadata(
            _make_paper(bibtex_key="a_2024_one", tags=["old-tag"])
        )
        store.write_paper_metadata(
            _make_paper(bibtex_key="b_2024_two", tags=["old-tag", "unrelated"])
        )
        store.write_paper_metadata(
            _make_paper(bibtex_key="c_2024_skip", tags=["unrelated"])
        )

        affected = store.apply_tag_rewrite({"old-tag": "new-tag"})

        assert set(affected) == {"a_2024_one", "b_2024_two"}
        assert store.read_paper_metadata("a_2024_one").tags == ["new-tag"]
        b_tags = store.read_paper_metadata("b_2024_two").tags
        assert set(b_tags) == {"new-tag", "unrelated"}
        assert store.read_paper_metadata("c_2024_skip").tags == ["unrelated"]

    def test_merge_deduplicates_when_both_present(self, store):
        store.write_paper_metadata(
            _make_paper(tags=["graph-theory", "graph-methods"])
        )
        affected = store.apply_tag_rewrite({"graph-methods": "graph-theory"})
        assert affected == ["smith_2024_causal"]
        assert store.read_paper_metadata("smith_2024_causal").tags == ["graph-theory"]

    def test_none_value_strips_tag(self, store):
        store.write_paper_metadata(_make_paper(tags=["macro", "finance"]))
        affected = store.apply_tag_rewrite({"finance": None})
        assert affected == ["smith_2024_causal"]
        assert store.read_paper_metadata("smith_2024_causal").tags == ["macro"]

    def test_empty_mapping_is_noop(self, store):
        store.write_paper_metadata(_make_paper(tags=["macro"]))
        assert store.apply_tag_rewrite({}) == []


class TestVocabularyMutations:
    def test_upsert_adds_new_entry(self, store):
        assert store.upsert_tag_in_vocab("macro", "Macroeconomics") is True
        vocab = store.read_tags()
        assert vocab == [{"tag": "macro", "description": "Macroeconomics"}]

    def test_upsert_updates_description(self, store):
        store.write_tags([{"tag": "macro", "description": "old"}])
        assert store.upsert_tag_in_vocab("macro", "new") is True
        vocab = store.read_tags()
        assert vocab[0]["description"] == "new"

    def test_upsert_is_noop_when_unchanged(self, store):
        store.write_tags([{"tag": "macro", "description": "same"}])
        assert store.upsert_tag_in_vocab("macro", "same") is False

    def test_remove_tags_from_vocab(self, store):
        store.write_tags(
            [
                {"tag": "macro", "description": None},
                {"tag": "finance", "description": None},
                {"tag": "causal", "description": None},
            ]
        )
        removed = store.remove_tags_from_vocab({"macro", "ghost"})
        assert removed == ["macro"]
        remaining = {t["tag"] for t in store.read_tags()}
        assert remaining == {"finance", "causal"}
