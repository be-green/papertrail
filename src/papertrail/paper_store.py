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

    def delete_paper_dir(self, bibtex_key: str) -> bool:
        paper_dir = self.config.papers_dir / bibtex_key
        if not paper_dir.exists():
            return False
        shutil.rmtree(paper_dir)
        return True

    def remove_tags_from_paper(
        self, bibtex_key: str, tags_to_remove: set[str]
    ) -> bool:
        """Strip the given tags from a single paper's metadata.json.

        Returns True if the file changed, False if the paper was missing or
        didn't have any of the requested tags.
        """
        paper = self.read_paper_metadata(bibtex_key)
        if paper is None:
            return False
        remaining = [t for t in paper.tags if t not in tags_to_remove]
        if len(remaining) == len(paper.tags):
            return False
        paper.tags = remaining
        self.write_paper_metadata(paper)
        return True

    def apply_tag_rewrite(self, mapping: dict[str, str | None]) -> list[str]:
        """Apply a rename/merge/remove mapping across every paper's metadata.

        For each tag on each paper: if the tag is a key in `mapping`, it is
        replaced with `mapping[tag]` (None means strip it). Other tags pass
        through. Duplicates created by the rewrite are deduplicated while
        preserving the paper's original tag ordering.

        Returns the bibtex keys whose metadata.json changed.
        """
        if not mapping:
            return []
        affected: list[str] = []
        for paper in self.scan_all_papers():
            new_tags: list[str] = []
            seen: set[str] = set()
            for tag in paper.tags:
                if tag in mapping:
                    replacement = mapping[tag]
                    if replacement is None:
                        continue
                    if replacement in seen:
                        continue
                    new_tags.append(replacement)
                    seen.add(replacement)
                else:
                    if tag in seen:
                        continue
                    new_tags.append(tag)
                    seen.add(tag)
            if new_tags != paper.tags:
                paper.tags = new_tags
                self.write_paper_metadata(paper)
                affected.append(paper.bibtex_key)
        return affected

    def upsert_tag_in_vocab(
        self, tag: str, description: str | None = None
    ) -> bool:
        """Add a tag to tags.json if absent, or update its description if given.

        Returns True if tags.json changed.
        """
        vocab = self.read_tags()
        for entry in vocab:
            if entry["tag"] == tag:
                if description is not None and entry.get("description") != description:
                    entry["description"] = description
                    self.write_tags(vocab)
                    return True
                return False
        vocab.append({"tag": tag, "description": description})
        self.write_tags(vocab)
        return True

    def remove_tags_from_vocab(self, tags_to_remove: set[str]) -> list[str]:
        """Remove entries from tags.json. Returns the tag names actually removed."""
        if not tags_to_remove:
            return []
        vocab = self.read_tags()
        kept = [t for t in vocab if t["tag"] not in tags_to_remove]
        removed = sorted({t["tag"] for t in vocab} & tags_to_remove)
        if removed:
            self.write_tags(kept)
        return removed
