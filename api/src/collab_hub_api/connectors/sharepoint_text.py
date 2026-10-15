from __future__ import annotations

import asyncio
import threading

# PDF text extraction for the SharePoint read route.
#
# Microsoft Graph converts Office documents (docx/pptx/xlsx/...) to PDF
# server-side; PDFs are downloaded as-is. Either way the Hub turns PDF bytes
# into bounded plain text here, so raw provider bytes never leave the Hub --
# the same rule Drive and Gmail follow. The engine is PDFium (via pypdfium2),
# the same library apollo-desktop's OCR sidecar uses for attached PDFs.
#
# This parses untrusted input. Every bound is enforced here, not by the caller:
# the byte size was capped before download, the page count and the character
# budget are capped below, and the caller wraps the (synchronous) parse in a
# thread with a deadline. Scanned pages (images, no text layer) extract to
# nothing; OCR is deliberately out of scope for the Hub.

# PDFium is not thread-safe (pypdfium2's "Incompatibility with Threading"): two
# parses at once in one process can crash it, even on different documents. The
# Hub serves many people from one process, so every parse holds this lock.
_PDFIUM_LOCK = threading.Lock()


class PdfExtractionError(RuntimeError):
    """The bytes could not be parsed as a PDF."""


def extract_pdf_text(data: bytes, *, max_chars: int, max_pages: int) -> tuple[str, bool]:
    """Plain text of a PDF, page by page, stopping at ``max_chars``/``max_pages``.

    Returns ``(text, truncated)``. ``truncated`` is True when pages were left
    unread or the assembled text exceeded ``max_chars`` and was cut.
    """
    with _PDFIUM_LOCK:
        return _extract_pdf_text(data, max_chars=max_chars, max_pages=max_pages)


def _extract_pdf_text(data: bytes, *, max_chars: int, max_pages: int) -> tuple[str, bool]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - dependency is pinned in pyproject
        raise PdfExtractionError("PDF text extraction is not available on this hub") from exc

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise PdfExtractionError("not a readable PDF") from exc
    except Exception as exc:  # noqa: BLE001 - any parser failure on untrusted bytes is "unreadable"
        raise PdfExtractionError("not a readable PDF") from exc

    parts: list[str] = []
    truncated = False
    chars = 0
    try:
        page_count = len(document)
        if page_count > max_pages:
            truncated = True
        for index in range(min(page_count, max_pages)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    page_text = text_page.get_text_range()
                finally:
                    text_page.close()
            except pdfium.PdfiumError:
                page_text = ""
            finally:
                page.close()
            page_text = _normalize_text(page_text)
            if page_text:
                parts.append(page_text)
                chars += len(page_text) + 2
            if chars > max_chars:
                # Overshoot slightly; the slice below sets the final truncation.
                # Stopping here bounds parse work, not just the returned size.
                if index + 1 < page_count:
                    truncated = True
                break
    finally:
        document.close()

    assembled = "\n\n".join(parts).strip()
    if len(assembled) > max_chars:
        truncated = True
    return assembled[:max_chars], truncated


async def extract_pdf_text_async(
    data: bytes,
    *,
    max_chars: int,
    max_pages: int,
    timeout_seconds: float,
) -> tuple[str, bool]:
    """Run :func:`extract_pdf_text` off the event loop with a deadline.

    PDFium is synchronous; a worker thread keeps the API responsive. A deadline
    that fires raises ``TimeoutError`` to the caller, but the thread keeps the
    PDFium lock until its page-capped parse ends, so a later read queues behind
    it rather than overlapping it; the page cap keeps that wait finite.
    """
    async with asyncio.timeout(timeout_seconds):
        return await asyncio.to_thread(extract_pdf_text, data, max_chars=max_chars, max_pages=max_pages)


def _normalize_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line and compact and compact[-1] == "":
            continue
        compact.append(line)
    return "\n".join(compact).strip()
