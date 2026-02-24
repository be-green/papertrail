import pytest
from unittest.mock import patch

from papertrail.converter import PdfConverter


@pytest.mark.asyncio
async def test_convert_writes_output(tmp_path):
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake pdf content")
    output_path = tmp_path / "output" / "paper.md"

    with patch("papertrail.converter.pymupdf4llm.to_markdown", return_value="# Test Paper\n\nContent here."):
        converter = PdfConverter()
        result = await converter.convert(pdf_path, output_path)

    assert result == "# Test Paper\n\nContent here."
    assert output_path.exists()
    assert output_path.read_text() == "# Test Paper\n\nContent here."


@pytest.mark.asyncio
async def test_convert_creates_parent_dirs(tmp_path):
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake")
    output_path = tmp_path / "deep" / "nested" / "dir" / "paper.md"

    with patch("papertrail.converter.pymupdf4llm.to_markdown", return_value="content"):
        converter = PdfConverter()
        await converter.convert(pdf_path, output_path)

    assert output_path.exists()


@pytest.mark.asyncio
async def test_convert_propagates_error(tmp_path):
    pdf_path = tmp_path / "bad.pdf"
    pdf_path.write_bytes(b"not a pdf")
    output_path = tmp_path / "output.md"

    with patch("papertrail.converter.pymupdf4llm.to_markdown", side_effect=RuntimeError("bad pdf")):
        converter = PdfConverter()
        with pytest.raises(RuntimeError, match="bad pdf"):
            await converter.convert(pdf_path, output_path)
