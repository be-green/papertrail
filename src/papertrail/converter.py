import asyncio
import logging
from pathlib import Path

import pymupdf4llm

logger = logging.getLogger(__name__)


class PdfConverter:
    async def convert(self, pdf_path: Path, output_path: Path) -> str:
        """Convert a PDF to markdown and write to output_path.

        Returns the markdown content.
        """
        markdown_content = await asyncio.to_thread(self._sync_convert, pdf_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown_content, encoding="utf-8")
        return markdown_content

    def _sync_convert(self, pdf_path: Path) -> str:
        """Synchronous conversion using pymupdf4llm."""
        try:
            return pymupdf4llm.to_markdown(str(pdf_path))
        except Exception as exc:
            logger.error("PDF conversion failed for %s: %s", pdf_path, exc)
            raise
