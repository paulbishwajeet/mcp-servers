#!/usr/bin/env python3
"""
local-ocr: a fully local, offline OCR MCP server.

Exposes a single MCP tool, `ocr_extract_text`, that reads text out of a
local image or PDF using on-device Tesseract OCR (via pytesseract /
poppler). No image or PDF bytes -- and no extracted text -- are ever sent
anywhere. This process makes zero network calls of any kind.

Runs as a stdio MCP server: `python server.py`.
"""

import logging
from pathlib import Path
from typing import Optional, TypedDict

from mcp.server.fastmcp import FastMCP
from PIL import Image, UnidentifiedImageError
import pytesseract
from pdf2image import convert_from_path
from pdf2image.exceptions import (
    PDFInfoNotInstalledError,
    PDFPageCountError,
    PDFSyntaxError,
)

# ---------------------------------------------------------------------------
# Logging: local file only, inside this project folder. We log which file
# was processed (by name) and basic stats, never the OCR'd text itself,
# since receipts/invoices can contain sensitive financial or personal data.
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "ocr_server.log"

logger = logging.getLogger("local_ocr")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.FileHandler(LOG_FILE)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS


class OcrResult(TypedDict):
    success: bool
    text: str
    page_count: int
    char_count: int
    error: Optional[str]


def _error_result(message: str) -> OcrResult:
    return {"success": False, "text": "", "page_count": 0, "char_count": 0, "error": message}


def _ok_result(text: str, page_count: int) -> OcrResult:
    return {"success": True, "text": text, "page_count": page_count, "char_count": len(text), "error": None}


def _ocr_image_file(path: Path, language: str) -> str:
    with Image.open(path) as img:
        return pytesseract.image_to_string(img, lang=language)


def _ocr_pdf_file(path: Path, language: str) -> tuple[str, int]:
    pages = convert_from_path(str(path))
    page_texts = []
    for page_number, page_image in enumerate(pages, start=1):
        page_text = pytesseract.image_to_string(page_image, lang=language)
        page_texts.append(f"--- Page {page_number} ---\n{page_text}")
    return "\n\n".join(page_texts), len(pages)


def run_ocr(file_path: str, language: str = "eng") -> OcrResult:
    """
    Core OCR implementation, deliberately kept separate from the MCP tool
    wrapper below so it (and this module) can grow a second tool later --
    e.g. a receipt-field parser that calls run_ocr() and then regexes the
    result -- without needing to restructure this file.
    """
    try:
        path = Path(file_path).expanduser()
    except Exception as exc:  # pragma: no cover - Path() rarely raises
        logger.info("rejected request: invalid path (%s)", exc)
        return _error_result(f"Invalid file path: {exc}")

    display_name = path.name or file_path

    if not path.is_absolute():
        logger.info("rejected request for %s: path is not absolute", display_name)
        return _error_result("file_path must be an absolute path")

    if not path.exists():
        logger.info("failed request for %s: file not found", display_name)
        return _error_result(f"File not found: {file_path}")

    if not path.is_file():
        logger.info("failed request for %s: not a regular file", display_name)
        return _error_result(f"Not a file: {file_path}")

    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.info("rejected request for %s: unsupported extension '%s'", display_name, ext)
        return _error_result(
            f"Unsupported file extension '{ext}'. Supported: "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    try:
        if ext in PDF_EXTENSIONS:
            text, page_count = _ocr_pdf_file(path, language)
        else:
            text = _ocr_image_file(path, language)
            page_count = 1
    except pytesseract.TesseractNotFoundError:
        logger.error("failed request for %s: tesseract binary not found on PATH", display_name)
        return _error_result(
            "Tesseract is not installed or not on PATH. Install with: brew install tesseract"
        )
    except PDFInfoNotInstalledError:
        logger.error("failed request for %s: poppler not installed on PATH", display_name)
        return _error_result(
            "Poppler is not installed or not on PATH. Install with: brew install poppler"
        )
    except (PDFPageCountError, PDFSyntaxError) as exc:
        logger.error("failed request for %s: unreadable PDF (%s)", display_name, exc)
        return _error_result(f"Could not read PDF (corrupt or invalid file): {exc}")
    except UnidentifiedImageError as exc:
        logger.error("failed request for %s: unreadable image (%s)", display_name, exc)
        return _error_result(f"Could not read image (corrupt or invalid file): {exc}")
    except pytesseract.TesseractError as exc:
        logger.error("failed request for %s: tesseract error (%s)", display_name, exc)
        return _error_result(
            f"Tesseract failed to process this file (check the 'language' argument is a "
            f"valid installed language pack): {exc}"
        )
    except Exception as exc:  # noqa: BLE001 - last-resort guard; must never raise through MCP
        logger.error("failed request for %s: unexpected error: %s", display_name, exc)
        return _error_result(f"Unexpected error during OCR: {exc}")

    result = _ok_result(text, page_count)
    logger.info(
        "ok for %s: pages=%d chars=%d lang=%s",
        display_name, result["page_count"], result["char_count"], language,
    )
    return result


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("local-ocr")


@mcp.tool()
def ocr_extract_text(file_path: str, language: str = "eng") -> OcrResult:
    """
    Extract plain text from a local receipt/invoice image or PDF using
    on-device Tesseract OCR.

    Runs entirely locally: no network calls, no cloud vision/LLM APIs.
    Only the extracted plain text is ever returned -- image or PDF bytes
    never leave this process.

    Args:
        file_path: Absolute path to a .jpg, .jpeg, .png, .webp, .bmp,
            .tiff, or .pdf file.
        language: Tesseract language code, e.g. "eng" or "eng+fra".
            Must match a language pack installed for Tesseract.
            Defaults to "eng".

    Returns:
        A dict with:
          success (bool): whether OCR completed without error.
          text (str): the extracted text (empty string on failure).
          page_count (int): pages processed (1 for images).
          char_count (int): len(text) -- a cheap "did this actually read
              anything" signal; near-zero often means a bad/blurry scan.
          error (str | None): human-readable error message, or null on
              success.
    """
    return run_ocr(file_path, language)


if __name__ == "__main__":
    mcp.run()
