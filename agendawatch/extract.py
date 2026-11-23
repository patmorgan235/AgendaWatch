"""Turn a downloaded agenda (PDF, Word .docx or HTML) into per-page text."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from pypdf import PdfReader

log = logging.getLogger(__name__)

logging.getLogger("pypdf").setLevel(logging.ERROR)


def _normalize(text: str) -> str:
    return (
        text.replace("­", "")      # soft hyphen
        .replace("ﬁ", "fi")
        .replace("ﬂ", "fl")
        .replace(" ", " ")
    )


def _pdf_pages(data: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            pass
    pages = []
    for page in reader.pages:
        try:
            pages.append(_normalize(page.extract_text() or ""))
        except Exception:  # noqa: BLE001 - one bad page shouldn't sink the document
            pages.append("")
    return pages


def _ocr(data: bytes) -> list[str] | None:
    """OCR a scanned PDF with ocrmypdf if it's installed; otherwise return None."""
    if not shutil.which("ocrmypdf"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in.pdf"), Path(tmp, "out.pdf")
        src.write_bytes(data)
        try:
            subprocess.run(
                ["ocrmypdf", "--skip-text", "--quiet", str(src), str(dst)],
                check=True, timeout=900, capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("OCR failed: %s", exc)
            return None
        return _pdf_pages(dst.read_bytes())


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCX_MAX_XML = 50 * 1024 * 1024   # refuse absurdly large (or zip-bomb) document.xml


def _is_docx(data: bytes, content_type: str) -> bool:
    return "wordprocessingml" in content_type or (data[:2] == b"PK" and b"word/" in data[:4096])


def _docx_pages(data: bytes) -> list[str] | None:
    """Text of a Word .docx, split into pages at explicit page breaks. None if it isn't readable."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > DOCX_MAX_XML:
                return None
            root = ElementTree.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return None
    pages, lines = [], []
    for para in root.iter(f"{_W}p"):
        text = []
        for node in para.iter():
            if node.tag == f"{_W}t" and node.text:
                text.append(node.text)
            elif node.tag == f"{_W}tab":
                text.append("\t")
            elif node.tag == f"{_W}br" and node.get(f"{_W}type") == "page":
                lines.append("".join(text))
                pages.append("\n".join(lines))
                lines, text = [], []
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                text.append("\n")
        lines.append("".join(text))
    pages.append("\n".join(lines))
    return [_normalize(p) for p in pages if p.strip()] or [""]


def looks_scanned(pages: list[str]) -> bool:
    if not pages:
        return False
    chars = sum(len(p.strip()) for p in pages)
    return chars < 40 * len(pages)


def extract_pages(data: bytes, content_type: str = "", ocr: str = "auto") -> tuple[list[str], str]:
    """Return (pages, note). Note explains anything unusual, e.g. a scanned PDF."""
    head = data[:1024].lstrip().lower()
    if data.lstrip()[:5] == b"%PDF-" or "pdf" in content_type:
        pages = _pdf_pages(data)
        if looks_scanned(pages):
            if ocr != "off":
                ocr_pages = _ocr(data)
                if ocr_pages is not None:
                    return ocr_pages, "scanned PDF, text read with OCR"
            return pages, "looks like a scanned PDF with little text (install ocrmypdf to read it)"
        return pages, ""
    if _is_docx(data, content_type):
        pages = _docx_pages(data)
        if pages is not None:
            return pages, "Word document"
        return [], "unreadable Word document"
    if "html" in content_type or head.startswith((b"<!doctype", b"<html")) or b"<body" in head:
        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return [_normalize(soup.get_text("\n"))], "HTML page"
    try:
        return [_normalize(data.decode("utf-8"))], "plain text"
    except UnicodeDecodeError:
        return [], f"unsupported file type ({content_type or 'unknown'})"
