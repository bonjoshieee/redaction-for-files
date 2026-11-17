"""True PDF redaction with PyMuPDF.

Each match gets a redact annotation over its glyphs, and apply_redactions removes the
underlying text and image pixels before drawing black boxes. The returned PDF no longer
contains the matched strings in any extractable form. Scanned PDFs have no text layer, so
nothing in them is found (OCR is out of scope).
"""

import threading

import pymupdf

from .redact import find_pii

# MuPDF is not thread-safe; the sync runs uploads in threads, so PDF work is serialised.
_LOCK = threading.Lock()

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)


class EncryptedPdfError(Exception):
    """The PDF is password-protected; nothing can be read without the password."""


def redact_pdf(data: bytes, ocr_pages: list[int] | tuple = ()) -> tuple[bytes, int]:
    """Returns the redacted PDF bytes and the number of matches removed.

    ocr_pages (1-based) are scanned pages to read with Tesseract first (see ocr_pdf); this
    raises ocr_pdf.ScanNotConfident when one of them can't be read well enough.
    When nothing matches, the original bytes are returned unchanged with a count of 0.
    """
    with _LOCK:
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            if doc.needs_pass:
                raise EncryptedPdfError("PDF is password-protected")
            count = 0
            for page in doc:
                needles = find_pii(page.get_text("text"))
                if not needles:
                    continue
                boxes = 0
                for needle in needles:
                    hits = page.search_for(needle)
                    for rect in hits:
                        page.add_redact_annot(rect, fill=(0, 0, 0))
                        boxes += 1
                    count += len(hits)
                if boxes:
                    page.apply_redactions()
            if ocr_pages:
                from .ocr_pdf import redact_scanned_pages

                count += redact_scanned_pages(doc, list(ocr_pages))
            if count == 0:
                return data, 0
            return doc.tobytes(garbage=3, deflate=True), count
        finally:
            doc.close()


def pdf_unreadable_pages(data: bytes) -> list[int]:
    """1-based numbers of pages that carry images but no text: scans whose content can't be
    checked without OCR. Blank pages don't count."""
    with _LOCK:
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            if doc.needs_pass:
                raise EncryptedPdfError("PDF is password-protected")
            return [
                page.number + 1
                for page in doc
                if not page.get_text("text").strip() and page.get_images(full=False)
            ]
        finally:
            doc.close()


def extract_pdf_text(data: bytes) -> str:
    """Plain text of a PDF, used by tests to prove redacted strings are gone."""
    with _LOCK:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text("text") for page in doc)
