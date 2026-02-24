import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from papertrail.config import PapertrailConfig
from papertrail.models import SearchResult

logger = logging.getLogger(__name__)

SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
ARXIV_API_BASE = "http://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

SEMANTIC_SCHOLAR_FIELDS = (
    "paperId,title,authors,year,abstract,venue,externalIds,"
    "citationCount,fieldsOfStudy,s2FieldsOfStudy,"
    "isOpenAccess,openAccessPdf,journal,publicationDate"
)

SKIP_TITLE_WORDS = {"a", "an", "the", "on", "in", "of", "for", "and", "to", "with", "by", "from", "is", "are", "at"}

SSRN_ABSTRACT_PATTERN = re.compile(r"ssrn\.com/abstract=(\d+)")
SSRN_ID_PATTERN = re.compile(r"^\d{5,}$")
ARXIV_ID_PATTERN = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
DOI_PATTERN = re.compile(r"^10\.\d{4,}")


class MetadataFetcher:
    def __init__(self, config: PapertrailConfig):
        self.api_key = config.semantic_scholar_api_key
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        self.client = httpx.AsyncClient(timeout=30.0, headers=headers)

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search Semantic Scholar and arXiv, merge and deduplicate results."""
        ss_results = await self._search_semantic_scholar(query, limit)
        arxiv_results = await self._search_arxiv(query, limit)
        combined = ss_results + arxiv_results
        return self._deduplicate(combined)[:limit]

    async def get_by_identifier(self, identifier: str) -> SearchResult | None:
        """Look up a paper by DOI, arXiv ID, SSRN ID/URL, or Semantic Scholar URL."""
        paper_id = self._normalize_identifier(identifier)
        if paper_id is None:
            return None

        response = await self._ss_get(f"/paper/{paper_id}")
        if response is None:
            return None
        return self._parse_ss_result(response)

    async def get_ssrn_metadata(self, ssrn_id: str) -> SearchResult | None:
        """Scrape metadata from an SSRN abstract page as a fallback."""
        url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_id}"
        try:
            response = await self.client.get(url, follow_redirects=True)
            if response.status_code != 200:
                return None
        except httpx.HTTPError:
            return None

        html = response.text
        title = self._extract_meta(html, "citation_title")
        authors_raw = self._extract_meta_all(html, "citation_author")
        abstract = self._extract_meta(html, "description")
        date = self._extract_meta(html, "citation_publication_date") or self._extract_meta(html, "citation_online_date")
        doi = self._extract_meta(html, "citation_doi")
        pdf_url = self._extract_meta(html, "citation_pdf_url")

        if not title:
            return None

        year = None
        if date:
            year_match = re.search(r"(\d{4})", date)
            if year_match:
                year = int(year_match.group(1))

        return SearchResult(
            title=title,
            authors=authors_raw or [],
            year=year,
            abstract=abstract,
            doi=doi,
            ssrn_id=ssrn_id,
            url=url,
            open_access_pdf_url=pdf_url,
            source="ssrn",
        )

    def generate_bibtex_key(self, result: SearchResult) -> str:
        """Generate a bibtex key: lastname_year_firstword."""
        first_author_last = "unknown"
        if result.authors:
            last_name = result.authors[0].split()[-1]
            first_author_last = re.sub(r"[^a-z]", "", last_name.lower())

        year = result.year or 0

        title_words = re.sub(r"[^a-z\s]", "", result.title.lower()).split()
        first_word = "paper"
        for word in title_words:
            if word not in SKIP_TITLE_WORDS and len(word) > 1:
                first_word = word
                break

        return f"{first_author_last}_{year}_{first_word}"

    async def generate_unique_key(self, result: SearchResult, db) -> str:
        """Generate a bibtex key, ensuring uniqueness by appending a suffix if needed."""
        base_key = self.generate_bibtex_key(result)
        key = base_key
        suffix = ord("a")
        while await db.check_bibtex_key_exists(key):
            key = f"{base_key}_{chr(suffix)}"
            suffix += 1
            if suffix > ord("z"):
                key = f"{base_key}_{suffix - ord('a')}"
                break
        return key

    async def download_pdf(self, result: SearchResult, dest_path: Path) -> Path:
        """Download the PDF for a paper, trying multiple sources."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        urls_to_try = []

        if result.open_access_pdf_url:
            urls_to_try.append(result.open_access_pdf_url)

        if result.arxiv_id:
            clean_id = result.arxiv_id.split("v")[0] if "v" in result.arxiv_id else result.arxiv_id
            urls_to_try.append(f"https://arxiv.org/pdf/{clean_id}")

        if result.ssrn_id:
            urls_to_try.append(f"https://papers.ssrn.com/sol3/Delivery.cfm?abstractid={result.ssrn_id}")

        if result.doi:
            urls_to_try.append(f"https://doi.org/{result.doi}")

        if result.url and result.url not in urls_to_try:
            urls_to_try.append(result.url)

        last_error = None
        for url in urls_to_try:
            try:
                response = await self.client.get(url, follow_redirects=True)
                content_type = response.headers.get("content-type", "")
                if response.status_code == 200 and (
                    "pdf" in content_type or response.content[:5] == b"%PDF-"
                ):
                    dest_path.write_bytes(response.content)
                    return dest_path
                last_error = f"URL {url}: status={response.status_code}, content-type={content_type}"
            except httpx.HTTPError as exc:
                last_error = f"URL {url}: {exc}"
                continue

        raise RuntimeError(f"Could not download PDF from any source. Last error: {last_error}")

    async def close(self) -> None:
        await self.client.aclose()

    # --- Private methods ---

    async def _search_semantic_scholar(self, query: str, limit: int) -> list[SearchResult]:
        try:
            params = {
                "query": query,
                "fields": SEMANTIC_SCHOLAR_FIELDS,
                "limit": min(limit, 100),
            }
            response = await self.client.get(
                f"{SEMANTIC_SCHOLAR_BASE}/paper/search",
                params=params,
            )
            if response.status_code == 429:
                logger.warning("Semantic Scholar rate limited")
                return []
            response.raise_for_status()
            data = response.json()
            return [self._parse_ss_result(item) for item in data.get("data", [])]
        except httpx.HTTPError as exc:
            logger.warning("Semantic Scholar search failed: %s", exc)
            return []

    async def _search_arxiv(self, query: str, limit: int) -> list[SearchResult]:
        try:
            params = {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": min(limit, 50),
            }
            response = await self.client.get(ARXIV_API_BASE, params=params)
            response.raise_for_status()
            return self._parse_arxiv_xml(response.text)
        except httpx.HTTPError as exc:
            logger.warning("arXiv search failed: %s", exc)
            return []

    async def _ss_get(self, path: str) -> dict | None:
        """Make a GET request to Semantic Scholar and return JSON or None."""
        try:
            response = await self.client.get(
                f"{SEMANTIC_SCHOLAR_BASE}{path}",
                params={"fields": SEMANTIC_SCHOLAR_FIELDS},
            )
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                logger.warning("Semantic Scholar rate limited")
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            logger.warning("Semantic Scholar request failed: %s", exc)
            return None

    def _normalize_identifier(self, identifier: str) -> str | None:
        """Convert a user-provided identifier into a Semantic Scholar paper ID."""
        identifier = identifier.strip()

        # SSRN URL
        ssrn_match = SSRN_ABSTRACT_PATTERN.search(identifier)
        if ssrn_match:
            # Semantic Scholar indexes some SSRN papers by DOI
            return f"URL:https://papers.ssrn.com/sol3/papers.cfm?abstract_id={ssrn_match.group(1)}"

        # Bare SSRN ID
        if SSRN_ID_PATTERN.match(identifier):
            return f"URL:https://papers.ssrn.com/sol3/papers.cfm?abstract_id={identifier}"

        # arXiv ID
        arxiv_match = ARXIV_ID_PATTERN.match(identifier)
        if arxiv_match:
            return f"ARXIV:{arxiv_match.group(1)}"

        # arXiv URL
        arxiv_url_match = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", identifier)
        if arxiv_url_match:
            return f"ARXIV:{arxiv_url_match.group(1)}"

        # DOI
        if DOI_PATTERN.match(identifier):
            return f"DOI:{identifier}"

        # DOI with prefix
        if identifier.upper().startswith("DOI:"):
            return identifier

        # Semantic Scholar URL
        if "semanticscholar.org" in identifier:
            parts = identifier.rstrip("/").split("/")
            return parts[-1]  # corpus ID or paper hash

        # Generic URL — try as-is
        if identifier.startswith("http"):
            return f"URL:{identifier}"

        # Last resort: treat as a title search — won't work with get_by_identifier
        return None

    def _parse_ss_result(self, data: dict) -> SearchResult:
        authors = [a.get("name", "") for a in data.get("authors", [])]
        external_ids = data.get("externalIds", {}) or {}
        fields = data.get("fieldsOfStudy", []) or []
        s2_fields = data.get("s2FieldsOfStudy", []) or []
        topics = [f["category"] for f in s2_fields if f.get("category")]

        oa_pdf = None
        oa_data = data.get("openAccessPdf")
        if oa_data and isinstance(oa_data, dict):
            oa_pdf = oa_data.get("url")

        return SearchResult(
            title=data.get("title", ""),
            authors=authors,
            year=data.get("year"),
            abstract=data.get("abstract"),
            doi=external_ids.get("DOI"),
            arxiv_id=external_ids.get("ArXiv"),
            url=f"https://www.semanticscholar.org/paper/{data.get('paperId', '')}",
            citation_count=data.get("citationCount"),
            topics=topics,
            fields_of_study=fields,
            open_access_pdf_url=oa_pdf,
            source="semantic_scholar",
        )

    def _parse_arxiv_xml(self, xml_text: str) -> list[SearchResult]:
        results = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        for entry in root.findall("atom:entry", ARXIV_NS):
            title_el = entry.find("atom:title", ARXIV_NS)
            title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""

            authors = []
            for author_el in entry.findall("atom:author", ARXIV_NS):
                name_el = author_el.find("atom:name", ARXIV_NS)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text.strip())

            abstract_el = entry.find("atom:summary", ARXIV_NS)
            abstract = abstract_el.text.strip().replace("\n", " ") if abstract_el is not None and abstract_el.text else None

            published_el = entry.find("atom:published", ARXIV_NS)
            year = None
            if published_el is not None and published_el.text:
                year_match = re.search(r"(\d{4})", published_el.text)
                if year_match:
                    year = int(year_match.group(1))

            id_el = entry.find("atom:id", ARXIV_NS)
            arxiv_id = None
            if id_el is not None and id_el.text:
                id_match = re.search(r"(\d{4}\.\d{4,5})", id_el.text)
                if id_match:
                    arxiv_id = id_match.group(1)

            doi_el = entry.find("{http://arxiv.org/schemas/atom}doi")
            doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

            categories = []
            for cat_el in entry.findall("{http://arxiv.org/schemas/atom}primary_category"):
                term = cat_el.get("term")
                if term:
                    categories.append(term)

            results.append(SearchResult(
                title=title,
                authors=authors,
                year=year,
                abstract=abstract,
                doi=doi,
                arxiv_id=arxiv_id,
                url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
                topics=categories,
                open_access_pdf_url=f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
                source="arxiv",
            ))
        return results

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """Deduplicate results by DOI or arXiv ID, preferring Semantic Scholar."""
        seen_dois: set[str] = set()
        seen_arxiv: set[str] = set()
        unique = []

        # Sort so semantic_scholar comes first
        sorted_results = sorted(results, key=lambda r: 0 if r.source == "semantic_scholar" else 1)

        for result in sorted_results:
            if result.doi and result.doi in seen_dois:
                continue
            if result.arxiv_id and result.arxiv_id in seen_arxiv:
                continue
            if result.doi:
                seen_dois.add(result.doi)
            if result.arxiv_id:
                seen_arxiv.add(result.arxiv_id)
            unique.append(result)
        return unique

    def _extract_meta(self, html: str, name: str) -> str | None:
        """Extract a single meta tag value from HTML."""
        pattern = rf'<meta\s+(?:name|property)="{re.escape(name)}"\s+content="([^"]*)"'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        # Try reversed order (content before name)
        pattern = rf'<meta\s+content="([^"]*)"\s+(?:name|property)="{re.escape(name)}"'
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    def _extract_meta_all(self, html: str, name: str) -> list[str]:
        """Extract all matching meta tag values from HTML."""
        results = []
        for pattern in [
            rf'<meta\s+(?:name|property)="{re.escape(name)}"\s+content="([^"]*)"',
            rf'<meta\s+content="([^"]*)"\s+(?:name|property)="{re.escape(name)}"',
        ]:
            for match in re.finditer(pattern, html, re.IGNORECASE):
                value = match.group(1).strip()
                if value and value not in results:
                    results.append(value)
        return results
