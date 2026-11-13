"""PII and money patterns tuned for Philippine documents.

Ported from the apo-turbo redaction Lambda (redaction-service/lib/lambda/redact.ts). Keep the
two in step until the Lambda is retired.

Every number pattern requires the separators or prefix people actually write, and must not sit
inside a longer number or a decimal. Bare digit runs are never matched: in the 2026-10-15 batch
they blacked out amounts, totals and ticket numbers in accounting spreadsheets (one sheet had
2536 false hits) while matching no real identifier.

Phone numbers are deliberately not redacted: they are business contact details here.
"""

import re

# JavaScript's \d, \w and \b are ASCII-only; re.ASCII gives Python the same behaviour.
_FLAGS = re.ASCII

NOT_IN_NUMBER = r"(?<![\d.])"
NOT_BEFORE_NUMBER = r"(?![\d.])"


def _num(body: str) -> re.Pattern[str]:
    return re.compile(f"{NOT_IN_NUMBER}{body}{NOT_BEFORE_NUMBER}", _FLAGS)


CURRENCY = r"(?:₱|PHP|Php|PhP|USD|US\$|\$)"
GROUPED = r"\d{1,3}(?:,\d{3})+"
AMOUNT_BODY = rf"(?:{GROUPED}|\d+)(?:\.\d{{1,2}})?"

NUMBER_WORD = (
    "(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    "fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
    "ninety|hundred|thousand|million|billion)"
)
WORDS = rf"{NUMBER_WORD}(?:(?:[\s-]+|\s+and\s+){NUMBER_WORD})*"
CURRENCY_WORD = r"(?:pesos?|php|dollars?|centavos?)"

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", _FLAGS), "[EMAIL_REDACTED]"),
    # ₱12,500.00  PHP 12500  Php 1,250.50  $1,200  USD 3,000
    (
        re.compile(rf"(?<![A-Za-z0-9₱$]){CURRENCY}\s?{AMOUNT_BODY}(?![\d.,])", _FLAGS),
        "[AMOUNT_REDACTED]",
    ),
    # P500 / P12,500.00: bare P only when glued to at least three digits or a grouped number,
    # so "Annex P 2024" and part codes like GP500 are left alone.
    (
        re.compile(rf"(?<![A-Za-z0-9])P(?:{GROUPED}|\d{{3,}})(?:\.\d{{2}})?(?![\d.,])", _FLAGS),
        "[AMOUNT_REDACTED]",
    ),
    # 12,500.00 with no sign: thousands separators plus exactly two decimals.
    (re.compile(rf"(?<![\d.,]){GROUPED}\.\d{{2}}(?![\d.,])", _FLAGS), "[AMOUNT_REDACTED]"),
    # 1250.50 with no sign or separators. Exactly two decimals, at least one peso, not a
    # percentage, not part of a version or a longer decimal.
    (re.compile(r"(?<![\d.,])[1-9]\d*\.\d{2}(?![\d.,%])", _FLAGS), "[AMOUNT_REDACTED]"),
    # Spelled out, as written on cheques and vouchers. A currency word is required.
    (
        re.compile(
            rf"\b{WORDS}(?:\s+and\s+\d{{1,2}}/100)?\s+{CURRENCY_WORD}(?:\s+only)?\b",
            _FLAGS | re.IGNORECASE,
        ),
        "[AMOUNT_REDACTED]",
    ),
    (
        re.compile(
            rf"\b{CURRENCY_WORD}\s*:?\s*{WORDS}(?:\s+and\s+\d{{1,2}}/100)?(?:\s+only)?\b",
            _FLAGS | re.IGNORECASE,
        ),
        "[AMOUNT_REDACTED]",
    ),
    # TIN: 123-456-789 or 123-456-789-000
    (_num(r"\d{3}-\d{3}-\d{3}(?:-\d{3})?"), "[TIN_REDACTED]"),
    # SSS: 12-3456789-0
    (_num(r"\d{2}-\d{7}-\d"), "[SSS_REDACTED]"),
    # PhilHealth: 12-345678901-2
    (_num(r"\d{2}-\d{9}-\d"), "[PHILHEALTH_REDACTED]"),
    # US SSN only when written with dashes: 123-45-6789
    (_num(r"\d{3}-\d{2}-\d{4}"), "[SSN_REDACTED]"),
]


def contains_pii(text: str) -> bool:
    return any(pattern.search(text) for pattern, _ in PATTERNS)


def redact(text: str) -> tuple[str, int]:
    """Returns the redacted text and the number of replacements made."""
    count = 0
    for pattern, replacement in PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def find_pii(text: str) -> list[str]:
    """Every distinct string that one of the patterns matches, minus ones nested in another.

    "2887352.33" inside "PHP 2887352.33" is dropped: the longer one already covers its glyphs,
    and counting both would double-count one amount.
    """
    found = {m.group(0) for pattern, _ in PATTERNS for m in pattern.finditer(text)}
    return [n for n in found if not any(o != n and n in o for o in found)]
