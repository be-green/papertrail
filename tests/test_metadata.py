import pytest

from papertrail.config import PapertrailConfig
from papertrail.metadata import MetadataFetcher
from papertrail.models import SearchResult


@pytest.fixture
def fetcher():
    config = PapertrailConfig(rclone_remote="")
    return MetadataFetcher(config)


class TestGenerateBibtexKey:
    def test_basic(self, fetcher):
        result = SearchResult(
            title="Causal Inference in Economics",
            authors=["John Smith"],
            year=2024,
        )
        assert fetcher.generate_bibtex_key(result) == "smith_2024_causal"

    def test_skips_common_words(self, fetcher):
        result = SearchResult(
            title="The Impact of Climate Change",
            authors=["Jane Doe"],
            year=2023,
        )
        assert fetcher.generate_bibtex_key(result) == "doe_2023_impact"

    def test_multiple_authors_uses_first(self, fetcher):
        result = SearchResult(
            title="Asset Pricing",
            authors=["John Smith", "Jane Doe", "Bob Jones"],
            year=2024,
        )
        assert fetcher.generate_bibtex_key(result) == "smith_2024_asset"

    def test_no_authors(self, fetcher):
        result = SearchResult(
            title="Anonymous Paper",
            authors=[],
            year=2024,
        )
        assert fetcher.generate_bibtex_key(result) == "unknown_2024_anonymous"

    def test_no_year(self, fetcher):
        result = SearchResult(
            title="Timeless Results",
            authors=["Smith"],
            year=None,
        )
        assert fetcher.generate_bibtex_key(result) == "smith_0_timeless"

    def test_special_chars_in_name(self, fetcher):
        result = SearchResult(
            title="Some Paper",
            authors=["O'Brien-Smith"],
            year=2024,
        )
        key = fetcher.generate_bibtex_key(result)
        assert key == "obriensmith_2024_some"

    def test_special_chars_in_title(self, fetcher):
        result = SearchResult(
            title="Risk & Return: A (New) Approach",
            authors=["Smith"],
            year=2024,
        )
        key = fetcher.generate_bibtex_key(result)
        assert key == "smith_2024_risk"


class TestNormalizeIdentifier:
    def test_doi(self, fetcher):
        assert fetcher._normalize_identifier("10.1257/aer.123") == "DOI:10.1257/aer.123"

    def test_doi_with_prefix(self, fetcher):
        assert fetcher._normalize_identifier("DOI:10.1257/aer.123") == "DOI:10.1257/aer.123"

    def test_arxiv_id(self, fetcher):
        assert fetcher._normalize_identifier("2301.12345") == "ARXIV:2301.12345"

    def test_arxiv_id_with_version(self, fetcher):
        assert fetcher._normalize_identifier("2301.12345v2") == "ARXIV:2301.12345"

    def test_arxiv_url(self, fetcher):
        assert fetcher._normalize_identifier("https://arxiv.org/abs/2301.12345") == "ARXIV:2301.12345"

    def test_arxiv_pdf_url(self, fetcher):
        assert fetcher._normalize_identifier("https://arxiv.org/pdf/2301.12345") == "ARXIV:2301.12345"

    def test_ssrn_url(self, fetcher):
        result = fetcher._normalize_identifier("https://ssrn.com/abstract=1234567")
        assert "1234567" in result

    def test_ssrn_bare_id(self, fetcher):
        result = fetcher._normalize_identifier("1234567")
        assert "1234567" in result

    def test_unknown_returns_none(self, fetcher):
        assert fetcher._normalize_identifier("some random text") is None

    def test_generic_url(self, fetcher):
        result = fetcher._normalize_identifier("https://example.com/paper.pdf")
        assert result == "URL:https://example.com/paper.pdf"


class TestDeduplicate:
    def test_deduplicates_by_doi(self, fetcher):
        results = [
            SearchResult(title="Paper A", authors=["Smith"], doi="10.1/a", source="semantic_scholar"),
            SearchResult(title="Paper A", authors=["Smith"], doi="10.1/a", source="arxiv"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 1
        assert deduped[0].source == "semantic_scholar"

    def test_deduplicates_by_arxiv_id(self, fetcher):
        results = [
            SearchResult(title="Paper B", authors=["Doe"], arxiv_id="2301.99999", source="arxiv"),
            SearchResult(title="Paper B", authors=["Doe"], arxiv_id="2301.99999", source="semantic_scholar"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 1
        assert deduped[0].source == "semantic_scholar"

    def test_keeps_different_papers(self, fetcher):
        results = [
            SearchResult(title="Paper A", authors=["Smith"], doi="10.1/a"),
            SearchResult(title="Paper B", authors=["Doe"], doi="10.1/b"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 2
