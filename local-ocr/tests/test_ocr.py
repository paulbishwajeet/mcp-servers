"""
Tests for local-ocr's core OCR logic (server.run_ocr).

Run with: pytest (from inside local-ocr/, with the venv active).

Real-extraction tests are skipped gracefully when no sample fixture files
are present in tests/fixtures/ -- drop your own sample receipt image/PDF
there to exercise them. Nothing here makes a network call.
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

TESSERACT_AVAILABLE = shutil.which("tesseract") is not None


def _first_fixture(*extensions: str) -> Path | None:
    if not FIXTURES_DIR.is_dir():
        return None
    for ext in extensions:
        matches = sorted(FIXTURES_DIR.glob(f"*{ext}"))
        if matches:
            return matches[0]
    return None


def test_missing_file_returns_structured_error(tmp_path):
    missing = tmp_path / "does-not-exist.jpg"

    result = server.run_ocr(str(missing))

    assert result["success"] is False
    assert result["text"] == ""
    assert result["page_count"] == 0
    assert result["char_count"] == 0
    assert "not found" in result["error"].lower()


def test_unsupported_extension_returns_structured_error(tmp_path):
    bad_file = tmp_path / "notes.txt"
    bad_file.write_text("this is not an image")

    result = server.run_ocr(str(bad_file))

    assert result["success"] is False
    assert result["text"] == ""
    assert "unsupported" in result["error"].lower()


def test_relative_path_is_rejected():
    result = server.run_ocr("relative/path/receipt.jpg")

    assert result["success"] is False
    assert "absolute" in result["error"].lower()


def test_never_raises_on_bad_input():
    # A directory is a valid absolute path but not a valid file -- this
    # must come back as a structured error, never an unhandled exception.
    result = server.run_ocr(str(FIXTURES_DIR.parent))

    assert result["success"] is False
    assert result["error"] is not None


@pytest.mark.skipif(
    not TESSERACT_AVAILABLE,
    reason="tesseract binary not found on PATH; install with `brew install tesseract`",
)
def test_real_image_extraction_if_fixture_present():
    fixture = _first_fixture(".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif")
    if fixture is None:
        pytest.skip("no sample image in tests/fixtures/ yet")

    result = server.run_ocr(str(fixture))

    assert result["success"] is True
    assert result["page_count"] == 1
    assert result["char_count"] == len(result["text"])
    assert result["error"] is None


@pytest.mark.skipif(
    not TESSERACT_AVAILABLE,
    reason="tesseract binary not found on PATH; install with `brew install tesseract`",
)
def test_real_pdf_extraction_if_fixture_present():
    fixture = _first_fixture(".pdf")
    if fixture is None:
        pytest.skip("no sample PDF in tests/fixtures/ yet")

    result = server.run_ocr(str(fixture))

    assert result["success"] is True
    assert result["page_count"] >= 1
    assert result["char_count"] == len(result["text"])
    assert result["error"] is None
