"""Redaction for scanned PDF pages with RapidOCR (PaddleOCR models, runs locally, offline).

A scanned page has no text layer, so it is rendered to an image and read by RapidOCR, which
returns each line of text with its position and a confidence score. Handwriting comes out a bit
garbled ("P1,200.00" can read as "91.200.("), so scans use looser rules than text files, and
when in doubt a line is blacked out:

- amounts the local model finds in the page's OCR text, and the regex amount patterns;
- number or currency words ("one thousand two hundred pesos");
- any line read with low confidence (usually handwriting).
Only amounts are hidden: account codes, receipt numbers, phone numbers and dates stay.

The whole matching line is blacked out and the image pixels under it are destroyed. A scanned
page where OCR finds no text at all (a photo, a blank or very poor scan) can't be checked, so
the file is left out (ScanNotConfident).
"""

from __future__ import annotations

import re
import threading

import pymupdf

from .redact import find_pii

DPI = 200
MIN_LINE_SCORE = 0.80  # lines read with less confidence than this are blacked out

NUMBER_WORDS = re.compile(
    r"\b(?:thousand|hundred|million|billion|pesos?|php|centavos?|dollars?)\b", re.IGNORECASE
)

FAINT_INK = 140  # a page whose darkest 1% is lighter than this is faded (thermal paper)

_engine = None
_engine_lock = threading.Lock()

# Pages read by OCR in the current file: [(page number, faint)]. Set by the caller to collect.
import contextvars

scan_log: contextvars.ContextVar[list | None] = contextvars.ContextVar("scan_log", default=None)


class ScanNotConfident(Exception):
    """A scanned page could not be read, so its redaction can't be trusted."""


def _ocr():
    global _engine
    with _engine_lock:
        if _engine is None:
            import logging

            from rapidocr import RapidOCR

            logging.getLogger("RapidOCR").setLevel(logging.ERROR)
            _engine = RapidOCR()
            logging.getLogger("RapidOCR").setLevel(logging.ERROR)  # it resets the level on start
        return _engine


def scan_line_reason(text: str, score: float) -> str | None:
    """Why this OCR'd line must be blacked out, or None to keep it.

    Only amounts: what the model or the amount patterns flag, amounts in words, and lines OCR
    couldn't read clearly (usually handwriting, which on vouchers is usually the amount).
    Account codes, receipt numbers, phone numbers and dates stay visible."""
    if find_pii(text):
        return "amount"
    if NUMBER_WORDS.search(text):
        return "amount in words"
    if score < MIN_LINE_SCORE:
        return "unclear"
    return None


def _page_images(page: pymupdf.Page):
    """The page as OCR input twice: as scanned, and contrast-boosted so faint print (thermal
    receipts, light photocopies) becomes readable. Lines found in either pass are used."""
    import numpy as np

    pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w)
    log = scan_log.get()
    if log is not None:
        log.append((page.number + 1, float(np.percentile(gray, 1)) >= FAINT_INK))
    yield np.stack([gray] * 3, axis=-1)
    a = gray.astype(np.float32)
    lo, hi = np.percentile(a, 1), np.percentile(a, 60)
    boosted = np.clip((a - lo) * 255 / max(hi - lo, 1.0), 0, 255).astype(np.uint8)
    yield np.stack([boosted] * 3, axis=-1)


def redact_scanned_pages(doc: pymupdf.Document, pages: list[int]) -> int:
    """OCRs the given 1-based pages of an open document and redacts them in place.

    Returns the number of lines blacked out. Raises ScanNotConfident for a page with no
    readable text.
    """
    scale = 72 / DPI
    pad = 2
    count = 0
    for n in pages:
        page = doc[n - 1]
        lines = []
        for img in _page_images(page):
            result = _ocr()(img)
            if result.boxes is not None:
                lines += zip(result.boxes, result.txts, result.scores)
        if not lines:
            raise ScanNotConfident(f"page {n}: no readable text")
        from .llm_pii import current_needles, extra_needles, page_finder

        finder = page_finder()
        found = finder("\n".join(t for _, t, _ in lines)) if finder else []
        with extra_needles(tuple(current_needles()) + tuple(found)):
            reasons = [scan_line_reason(t, float(s)) for _, t, s in lines]
        hits = 0
        for (box, text, score), reason in zip(lines, reasons):
            if reason is None:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            r = pymupdf.Rect(
                min(xs) * scale - pad, min(ys) * scale - pad,
                max(xs) * scale + pad, max(ys) * scale + pad,
            )
            page.add_redact_annot(r * page.derotation_matrix, fill=(0, 0, 0))
            hits += 1
        if hits:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
            count += hits
    return count


# --- images inside Word / Excel files ----------------------------------------------------

IMAGE_FORMATS = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg"}
VECTOR_IMAGES = {".emf", ".wmf", ".svg"}
MIN_IMAGE_PX = 40  # smaller images (icons, bullets) can't hold readable text


def redact_image(data: bytes, ext: str) -> tuple[bytes, int]:
    """Blacks out lines in a PNG/JPEG that the scan rules flag. Returns the original bytes and 0
    when nothing is flagged or the image holds no text (a plain logo or photo is fine)."""
    import numpy as np

    pix = pymupdf.Pixmap(data)
    if pix.w < MIN_IMAGE_PX or pix.h < MIN_IMAGE_PX:
        return data, 0
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # gray / CMYK -> RGB (alpha is kept)
    n = 4 if pix.alpha else 3
    px = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, n).copy()
    if n == 4:  # OCR sees the picture on white, as it appears on the page
        a = px[..., 3:4].astype(np.float32) / 255
        rgb = (px[..., :3] * a + 255 * (1 - a)).astype(np.uint8)
    else:
        rgb = px
    gray = rgb.mean(axis=2).astype(np.uint8)
    lines = []
    for img in (rgb, _boost(gray)):
        result = _ocr()(img)
        if result.boxes is not None:
            lines += zip(result.boxes, result.txts, result.scores)
    if not lines:
        return data, 0
    from .llm_pii import current_needles, extra_needles, page_finder

    finder = page_finder()
    found = finder("\n".join(t for _, t, _ in lines)) if finder else []
    with extra_needles(tuple(current_needles()) + tuple(found)):
        flagged = [box for box, t, s in lines if scan_line_reason(t, float(s)) is not None]
    if not flagged:
        return data, 0
    for box in flagged:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, y0 = max(0, int(min(xs)) - 2), max(0, int(min(ys)) - 2)
        x1, y1 = min(pix.w, int(max(xs)) + 3), min(pix.h, int(max(ys)) + 3)
        px[y0:y1, x0:x1, :3] = 0
        if n == 4:
            px[y0:y1, x0:x1, 3] = 255  # solid black, also where the picture was transparent
    out = pymupdf.Pixmap(pymupdf.csRGB, pix.w, pix.h, px.tobytes(), n == 4)
    if ext != ".png" and n == 4:
        out = pymupdf.Pixmap(out, 0)  # JPEG has no transparency
    return out.tobytes(IMAGE_FORMATS[ext]), len(flagged)


def _boost(gray):
    import numpy as np

    a = gray.astype(np.float32)
    lo, hi = np.percentile(a, 1), np.percentile(a, 60)
    boosted = np.clip((a - lo) * 255 / max(hi - lo, 1.0), 0, 255).astype(np.uint8)
    return np.stack([boosted] * 3, axis=-1)
