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


def redact_pdf(data: bytes) -> tuple[bytes, int]:
    """Returns the redacted PDF bytes and the number of matches removed.

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
            if count == 0:
                return data, 0
            return doc.tobytes(garbage=3, deflate=True), count
        finally:
            doc.close()


def extract_pdf_text(data: bytes) -> str:
    """Plain text of a PDF, used by tests to prove redacted strings are gone."""
    with _LOCK:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text("text") for page in doc)
