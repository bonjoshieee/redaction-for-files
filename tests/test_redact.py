"""Ported 1:1 from redaction-service/test/redact.test.ts in apo-turbo."""

import pytest

from nas_s3_sync.redact import contains_pii, redact


def test_emails_are_kept_only_amounts_are_redacted():
    out, count = redact("CERES SUMMIT CORPORATION Email: ops@ceres.ph Ref 1680000")
    assert out == "CERES SUMMIT CORPORATION Email: ops@ceres.ph Ref 1680000"
    assert count == 0


@pytest.mark.parametrize(
    "amount,sentence",
    [
        ("₱12,500.00", "Total ₱12,500.00 due"),
        ("₱ 12,500.00", "Total ₱ 12,500.00 due"),
        ("PHP 1,680,000", "Contract price PHP 1,680,000 inclusive"),
        ("Php 1,250.50", "Php 1,250.50 per unit"),
        ("PHP 12500", "PHP 12500 flat"),
        ("$1,200", "Deposit $1,200 received"),
        ("USD 3,000.00", "USD 3,000.00 wire"),
        ("P500", "Fee P500 each"),
        ("P12,500.00", "Balance P12,500.00"),
        ("12,500.00", "Grand total 12,500.00 net"),
        ("2,887,352.33", "Sum 2,887,352.33 as of June"),
        ("1250.50", "Unit price 1250.50 each"),
        ("2887352.33", "Total 2887352.33 as of June"),
        ("540.00", "VAT 540.00 inclusive"),
        ("twelve thousand five hundred pesos", "Amount: twelve thousand five hundred pesos payable"),
        ("TWELVE THOUSAND FIVE HUNDRED PESOS ONLY", "TWELVE THOUSAND FIVE HUNDRED PESOS ONLY"),
        ("One Thousand and 50/100 Pesos", "Pay One Thousand and 50/100 Pesos to bearer"),
        ("Pesos: Twelve Thousand Only", "Pesos: Twelve Thousand Only ***"),
        ("PHP Two Hundred Fifty", "Fee PHP Two Hundred Fifty per head"),
        ("forty-five dollars", "about forty-five dollars"),
    ],
)
def test_redacts_amount(amount, sentence):
    out, count = redact(sentence)
    assert out == sentence.replace(amount, "[AMOUNT_REDACTED]")
    assert count == 1


def test_redacts_every_amount_on_an_invoice_line():
    out, count = redact("Qty 3 @ PHP 1,500.00 = PHP 4,500.00 VAT 540.00 Total 5,040.00")
    assert out == (
        "Qty 3 @ [AMOUNT_REDACTED] = [AMOUNT_REDACTED] VAT [AMOUNT_REDACTED] Total [AMOUNT_REDACTED]"
    )
    assert count == 4


@pytest.mark.parametrize(
    "text",
    [
        # Look like money but are not, or the shape is too common to redact safely.
        "0.75 0.12342985246013072 2038161.1234567890",
        "Rate 12.50% and 7.5% and 100.00%",
        "3.027970544219877E-6 1.5 12.345",
        "one hundred people and two thousand units",
        "Twelve apostles, ten thousand reasons, a million thanks",
        "Annex P 2024 and Section P 12",
        "Part GP500 and model XP1200",
        "1,234 units and 12,500 pcs",
        "1,234,567 reference",
        "12,500.000 and 1,2345.00",
        "v1.2.3 build 10.15.2026",
        "Ticket 123456 and 2026100112345",
        # Real cell values from accounting spreadsheets in the 2026-10-15 batch. None is PII.
        "225000 2303599 32535",
        "B28/B10 0.12342985246013072",
        "SUM(B6:B85) 160000 43008 10752",
        "3.027970544219877E-6 5.815445053411733E-5",
        "079-1234567890 YV6LT3 Cebu-Manila-Cebu",
        "Invoice 2026100112345 due 10/15/2026",
        "07/03/2023 and 1680000",
        "17 1000 2000 18 10000 10000 20000",
        "100 200 3000 400 500 6000",
        # Phone numbers are kept on purpose.
        "Call 0917 123 4567 or (032) 234-5678",
        "Tel +63 915 209 2203 | 555-123-4567",
    ],
)
def test_leaves_alone(text):
    assert redact(text) == (text, 0)


@pytest.mark.parametrize(
    "value,tag",
    [
        ("123-456-789-000", "[TIN_REDACTED]"),
        ("123-456-789", "[TIN_REDACTED]"),
        ("12-3456789-0", "[SSS_REDACTED]"),
        ("12-345678901-2", "[PHILHEALTH_REDACTED]"),
        ("123-45-6789", "[SSN_REDACTED]"),
    ],
)
def test_government_ids_are_kept(value, tag):
    assert redact(f"Contact: {value} today") == (f"Contact: {value} today", 0)


def test_does_not_match_inside_a_longer_digit_run():
    assert redact("ref 99009171234567001")[1] == 0
    assert redact("amount 1555-123-45678")[1] == 0


def test_contains_pii_agrees_with_redact():
    assert not contains_pii("email ops@ceres.ph")
    assert not contains_pii("call 0917 123 4567")
    assert contains_pii("total 2887352.33")
    assert contains_pii("total 2,887,352.33")
