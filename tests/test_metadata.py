import httpx
import pytest
import respx

from papertrail.config import PapertrailConfig
from papertrail.metadata import MetadataFetcher
from papertrail.models import SearchResult


@pytest.fixture
def fetcher():
    config = PapertrailConfig()
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

    def test_prefers_ss_over_crossref_and_openalex(self, fetcher):
        results = [
            SearchResult(title="Paper C", authors=["Smith"], doi="10.1/c", source="openalex"),
            SearchResult(title="Paper C", authors=["Smith"], doi="10.1/c", source="crossref"),
            SearchResult(title="Paper C", authors=["Smith"], doi="10.1/c", source="semantic_scholar"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 1
        assert deduped[0].source == "semantic_scholar"

    def test_doi_case_insensitive(self, fetcher):
        results = [
            SearchResult(title="Paper D", authors=["Smith"], doi="10.1/D", source="semantic_scholar"),
            SearchResult(title="Paper D", authors=["Smith"], doi="10.1/d", source="crossref"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 1

    def test_title_tiebreaker_when_no_doi(self, fetcher):
        """OpenAlex preprints often lack DOI/arXiv ID; dedupe by normalized title."""
        results = [
            SearchResult(title="A Novel Approach", authors=["Smith"], source="semantic_scholar"),
            SearchResult(title="A Novel Approach!", authors=["Smith"], source="openalex"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 1
        assert deduped[0].source == "semantic_scholar"

    def test_title_tiebreaker_does_not_merge_when_doi_present(self, fetcher):
        """If either result has a DOI, don't merge just by title — DOIs are authoritative."""
        results = [
            SearchResult(title="Common Title", authors=["Smith"], doi="10.1/a", source="semantic_scholar"),
            SearchResult(title="Common Title", authors=["Doe"], doi="10.1/b", source="crossref"),
        ]
        deduped = fetcher._deduplicate(results)
        assert len(deduped) == 2


CROSSREF_SEARCH_RESPONSE = {
    "status": "ok",
    "message": {
        "items": [
            {
                "DOI": "10.1093/qje/qjw040",
                "title": ["Technological Innovation, Resource Allocation, and Growth"],
                "author": [
                    {"given": "Leonid", "family": "Kogan"},
                    {"given": "Dimitris", "family": "Papanikolaou"},
                ],
                "published-print": {"date-parts": [[2017, 5]]},
                "abstract": "We study how innovation affects growth.",
                "URL": "https://doi.org/10.1093/qje/qjw040",
                "link": [],
            },
            {
                "DOI": "10.2139/ssrn.4631010",
                "title": ["Technology and Labor Displacement"],
                "author": [{"given": "Leonid", "family": "Kogan"}],
                "published-online": {"date-parts": [[2023, 11, 1]]},
                "abstract": "Impact on workers.",
                "URL": "https://doi.org/10.2139/ssrn.4631010",
                "link": [],
            },
        ]
    },
}

OPENALEX_SEARCH_RESPONSE = {
    "results": [
        {
            "id": "https://openalex.org/W1234567",
            "title": "Technological Innovation and Growth",
            "display_name": "Technological Innovation and Growth",
            "doi": "https://doi.org/10.1093/qje/qjw040",
            "publication_year": 2017,
            "cited_by_count": 1500,
            "authorships": [
                {"author": {"display_name": "Leonid Kogan"}},
                {"author": {"display_name": "Dimitris Papanikolaou"}},
            ],
            "open_access": {"oa_url": "https://repository.edu/paper.pdf"},
            "abstract_inverted_index": {
                "We": [0],
                "study": [1],
                "innovation": [2, 5],
                "and": [3],
                "growth": [4],
                ".": [6],
            },
        },
        {
            "id": "https://openalex.org/W7654321",
            "title": "Another Paper Without DOI",
            "doi": None,
            "publication_year": 2020,
            "authorships": [{"author": {"display_name": "Jane Doe"}}],
            "open_access": {},
            "abstract_inverted_index": None,
        },
    ]
}


class TestCrossrefSearch:
    @pytest.mark.asyncio
    async def test_parses_search_results(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r"https://api\.crossref\.org/works.*").mock(
                return_value=httpx.Response(200, json=CROSSREF_SEARCH_RESPONSE)
            )
            results = await fetcher._search_crossref("kogan innovation", 10)

        assert len(results) == 2
        assert results[0].source == "crossref"
        assert results[0].doi == "10.1093/qje/qjw040"
        assert results[0].year == 2017
        assert results[1].ssrn_id == "4631010"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r"https://api\.crossref\.org/works.*").mock(
                side_effect=httpx.ConnectError("boom")
            )
            results = await fetcher._search_crossref("anything", 10)

        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_non_200(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r"https://api\.crossref\.org/works.*").mock(
                return_value=httpx.Response(500)
            )
            results = await fetcher._search_crossref("anything", 10)

        assert results == []


class TestOpenAlexSearch:
    @pytest.mark.asyncio
    async def test_parses_search_results(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
                return_value=httpx.Response(200, json=OPENALEX_SEARCH_RESPONSE)
            )
            results = await fetcher._search_openalex("innovation growth", 10)

        assert len(results) == 2
        first = results[0]
        assert first.source == "openalex"
        assert first.doi == "10.1093/qje/qjw040"  # stripped of https://doi.org/
        assert first.year == 2017
        assert first.citation_count == 1500
        assert first.open_access_pdf_url == "https://repository.edu/paper.pdf"
        assert "Leonid Kogan" in first.authors
        # Abstract reconstructed from inverted index (positions sorted ascending)
        assert first.abstract is not None
        assert "We study innovation" in first.abstract

        second = results[1]
        assert second.doi is None
        assert second.abstract is None

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
                side_effect=httpx.ConnectError("boom")
            )
            results = await fetcher._search_openalex("anything", 10)

        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_non_200(self, fetcher):
        with respx.mock:
            respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
                return_value=httpx.Response(404)
            )
            results = await fetcher._search_openalex("anything", 10)

        assert results == []


class TestParallelSearch:
    @pytest.mark.asyncio
    async def test_search_merges_all_four_backends(self, fetcher):
        """search() runs SS + arXiv + Crossref + OpenAlex in parallel and dedupes."""
        ss_response = {
            "data": [
                {
                    "paperId": "ssid1",
                    "title": "Paper X",
                    "authors": [{"name": "Alice Smith"}],
                    "year": 2020,
                    "abstract": None,
                    "externalIds": {"DOI": "10.1/x"},
                    "citationCount": 42,
                    "fieldsOfStudy": [],
                    "s2FieldsOfStudy": [],
                    "openAccessPdf": None,
                }
            ]
        }
        arxiv_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Arxiv Paper Y</title>
    <author><name>Bob Jones</name></author>
    <summary>Summary.</summary>
    <published>2021-01-01T00:00:00Z</published>
    <id>http://arxiv.org/abs/2101.00001</id>
  </entry>
</feed>"""
        crossref_response = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1/z",
                        "title": ["Crossref Paper Z"],
                        "author": [{"given": "Carol", "family": "Lee"}],
                        "published-print": {"date-parts": [[2022]]},
                        "URL": "https://doi.org/10.1/z",
                        "link": [],
                    }
                ]
            }
        }
        openalex_response = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "title": "OpenAlex Paper W",
                    "doi": None,
                    "publication_year": 2023,
                    "authorships": [{"author": {"display_name": "Dan Park"}}],
                    "open_access": {},
                    "abstract_inverted_index": None,
                }
            ]
        }

        with respx.mock:
            respx.get(url__regex=r".*semanticscholar\.org.*").mock(
                return_value=httpx.Response(200, json=ss_response)
            )
            respx.get(url__regex=r".*arxiv\.org/api/query.*").mock(
                return_value=httpx.Response(200, text=arxiv_xml)
            )
            respx.get(url__regex=r"https://api\.crossref\.org/works.*").mock(
                return_value=httpx.Response(200, json=crossref_response)
            )
            respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
                return_value=httpx.Response(200, json=openalex_response)
            )
            results = await fetcher.search("something", limit=10)

        sources = {r.source for r in results}
        assert sources == {"semantic_scholar", "arxiv", "crossref", "openalex"}
        assert len(results) == 4

    @pytest.mark.asyncio
    async def test_search_returns_others_when_ss_fails(self, fetcher):
        """If Semantic Scholar is down, the other backends still return results."""
        crossref_response = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1/z",
                        "title": ["Crossref Only Paper"],
                        "author": [{"given": "Carol", "family": "Lee"}],
                        "published-print": {"date-parts": [[2022]]},
                        "URL": "https://doi.org/10.1/z",
                        "link": [],
                    }
                ]
            }
        }
        with respx.mock:
            respx.get(url__regex=r".*semanticscholar\.org.*").mock(
                side_effect=httpx.ConnectError("down")
            )
            respx.get(url__regex=r".*arxiv\.org/api/query.*").mock(
                side_effect=httpx.ConnectError("down")
            )
            respx.get(url__regex=r"https://api\.crossref\.org/works.*").mock(
                return_value=httpx.Response(200, json=crossref_response)
            )
            respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
                return_value=httpx.Response(200, json={"results": []})
            )
            results = await fetcher.search("something", limit=10)

        assert len(results) == 1
        assert results[0].source == "crossref"
