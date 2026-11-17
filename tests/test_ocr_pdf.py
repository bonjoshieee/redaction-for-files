import pytest

from nas_s3_sync.ocr_pdf import scan_line_reason


@pytest.mark.parametrize("text", [
    "One thousand two hundred pcsos only pesos",
    "TOTAL DUE PHP1,200.00",
    "300.00",
    "1,200.00",
])
def test_blacked_out(text):
    assert scan_line_reason(text, 0.99) is not None


def test_garbled_handwriting_is_hidden_when_the_model_flags_it():
    from nas_s3_sync.llm_pii import extra_needles

    assert scan_line_reason("91.200.(", 0.99) is None
    with extra_needles([("91.200.(", "[AMOUNT_REDACTED]")]):
        assert scan_line_reason("91.200.(", 0.99) == "amount"


@pytest.mark.parametrize("text", [
    "OFFICIAL RECEIPT",
    "Date: 01/06/26",
    "Affidavit - Bamboo Bay",
    "No VAT",
    "NON-VAT REG. TIN: 267-105-674-00000",
    "accounting@envicomm.org",
    "Mobile no: +63917-177-3171",
    "701014 Legal and",
    "Reference No: 147",
])
def test_kept(text):
    assert scan_line_reason(text, 0.99) is None


def test_unclear_line_is_blacked_out():
    assert scan_line_reason("Bually andly:", 0.5) == "unclear"
