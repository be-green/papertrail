import json
import logging
import shutil
from pathlib import Path

from papertrail.config import PapertrailConfig
from papertrail.models import PaperMetadata

logger = logging.getLogger(__name__)


class PaperStore:
    """JSON file I/O for the paper library on the mounted filesystem.

    All methods are synchronous — callers should use asyncio.to_thread
    for bulk operations.
    """

    def __init__(self, config: PapertrailConfig):
        self.config = config

    def read_paper_metadata(self, bibtex_key: str) -> PaperMetadata | None:
        metadata_path = self.config.papers_dir / bibtex_key / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            return PaperMetadata(**data)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to read metadata for %s: %s", bibtex_key, exc)
            return None

    def write_paper_metadata(self, paper: PaperMetadata) -> None:
        paper_dir = self.config.papers_dir / paper.bibtex_key
        paper_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = paper_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(paper.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )

    def write_summary_file(self, bibtex_key: str, summary: dict) -> None:
        paper_dir = self.config.papers_dir / bibtex_key
        paper_dir.mkdir(parents=True, exist_ok=True)
        summary_path = paper_dir / "summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )

    def read_tags(self) -> list[dict]:
        tags_path = self.config.tags_path
        if not tags_path.exists():
            return []
        try:
            data = json.loads(tags_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def write_tags(self, tags: list[dict]) -> None:
        self.config.tags_path.write_text(
            json.dumps(tags, indent=2),
            encoding="utf-8",
        )

    def scan_all_papers(self) -> list[PaperMetadata]:
        papers = []
        papers_dir = self.config.papers_dir
        if not papers_dir.exists():
            return papers
        for metadata_path in papers_dir.glob("*/metadata.json"):
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                papers.append(PaperMetadata(**data))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("Skipping %s: %s", metadata_path, exc)
        return papers

    def paper_dir_exists(self, bibtex_key: str) -> bool:
        return (self.config.papers_dir / bibtex_key).is_dir()

    def read_paper_markdown(self, bibtex_key: str) -> str | None:
        md_path = self.config.papers_dir / bibtex_key / "paper.md"
        if not md_path.exists():
            return None
        return md_path.read_text(encoding="utf-8")

    def write_bibtex(self, bibtex_key: str, bibtex_entry: str) -> None:
        paper_dir = self.config.papers_dir / bibtex_key
        paper_dir.mkdir(parents=True, exist_ok=True)
        bib_path = paper_dir / "citation.bib"
        bib_path.write_text(bibtex_entry, encoding="utf-8")

    def read_bibtex(self, bibtex_key: str) -> str | None:
        bib_path = self.config.papers_dir / bibtex_key / "citation.bib"
        if not bib_path.exists():
            return None
        return bib_path.read_text(encoding="utf-8")

    def delete_paper_dir(self, bibtex_key: str) -> bool:
        paper_dir = self.config.papers_dir / bibtex_key
        if not paper_dir.exists():
            return False
        shutil.rmtree(paper_dir)
        return True
