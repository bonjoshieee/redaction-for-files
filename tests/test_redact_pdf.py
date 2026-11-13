import pymupdf
import pytest

from nas_s3_sync.redact_pdf import EncryptedPdfError, extract_pdf_text, redact_pdf


def make_pdf(pages: list[list[str]]) -> bytes:
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        for i, text in enumerate(lines):
            page.insert_text((50, 80 + i * 24), text, fontsize=12, fontname="helv")
    return doc.tobytes()


def test_removes_matched_text_not_just_paints_over_it():
    data = make_pdf([[
        "Oscar Lopez Jr., Chief Strategy and Growth Officer",
        "csgo@envicomm.org | TIN 123-456-789-000 | +63 915 209 2203",
        "Amount due: PHP 2887352.33 ref 2026100112345",
    ]])
    out, count = redact_pdf(data)
    assert count == 3
    text = extract_pdf_text(out)
    for gone in ("csgo@envicomm.org", "123-456-789-000", "2887352.33"):
        assert gone not in text
    for kept in ("209 2203", "Oscar Lopez Jr.", "Amount due:", "2026100112345"):
        assert kept in text


def test_returns_original_bytes_when_nothing_matches():
    data = make_pdf([["Total 160000 43008 10752", "SUM(B6:B85) 2303599"]])
    assert redact_pdf(data) == (data, 0)


def test_redacts_the_same_string_on_every_page():
    out, count = redact_pdf(make_pdf([["SSS 12-3456789-0"]] * 3))
    assert count == 3
    assert "3456789" not in extract_pdf_text(out)


def test_raises_for_password_protected_pdf():
    doc = pymupdf.open(stream=make_pdf([["secret ops@ceres.ph"]]), filetype="pdf")
    encrypted = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="hunter2", owner_pw="hunter2"
    )
    with pytest.raises(EncryptedPdfError):
        redact_pdf(encrypted)
