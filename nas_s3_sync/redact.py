"""Money-amount patterns tuned for Philippine documents.

ONLY AMOUNTS ARE REDACTED (decided 2026-11-16). Emails, TIN, SSS, PhilHealth, names, addresses
and phone numbers stay visible; the ID patterns are kept in ID_PATTERNS but are not used.
The local model (llm_pii) finds amounts these patterns miss; both are applied.

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
# An amount may be followed by punctuation ("PHP 1,250.00, payable...") but not by more digits.
END_AMOUNT = r"(?!\d|[.,]\d)"
START_AMOUNT = r"(?<!\d)(?<!\d[.,])"
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
    # ₱12,500.00  PHP 12500  Php 1,250.50  $1,200  USD 3,000
    (
        re.compile(rf"(?<![A-Za-z0-9₱$]){CURRENCY}\s?{AMOUNT_BODY}{END_AMOUNT}", _FLAGS),
        "[AMOUNT_REDACTED]",
    ),
    # P500 / P12,500.00: bare P only when glued to at least three digits or a grouped number,
    # so "Annex P 2024" and part codes like GP500 are left alone.
    (
        re.compile(rf"(?<![A-Za-z0-9])P(?:{GROUPED}|\d{{3,}})(?:\.\d{{2}})?{END_AMOUNT}", _FLAGS),
        "[AMOUNT_REDACTED]",
    ),
    # 12,500.00 with no sign: thousands separators plus exactly two decimals.
    (re.compile(rf"{START_AMOUNT}{GROUPED}\.\d{{2}}{END_AMOUNT}", _FLAGS), "[AMOUNT_REDACTED]"),
    # 1250.50 with no sign or separators. Exactly two decimals, at least one peso, not a
    # percentage, not part of a version or a longer decimal.
    (re.compile(rf"{START_AMOUNT}[1-9]\d*\.\d{{2}}(?!%){END_AMOUNT}", _FLAGS), "[AMOUNT_REDACTED]"),
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
]

# Not used: only amounts are redacted. Kept for reference / a future switch.
ID_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", _FLAGS), "[EMAIL_REDACTED]"),
    # TIN: 123-456-789, 123-456-789-000 or with a 5-digit branch code 123-456-789-00000
    (_num(r"\d{3}-\d{3}-\d{3}(?:-\d{3}(?:\d{2})?)?"), "[TIN_REDACTED]"),
    # SSS: 12-3456789-0
    (_num(r"\d{2}-\d{7}-\d"), "[SSS_REDACTED]"),
    # PhilHealth: 12-345678901-2
    (_num(r"\d{2}-\d{9}-\d"), "[PHILHEALTH_REDACTED]"),
    # US SSN only when written with dashes: 123-45-6789
    (_num(r"\d{3}-\d{2}-\d{4}"), "[SSN_REDACTED]"),
]


def _needle(s: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(w) for w in s.split())
    start = r"(?<![\w])(?<!\d[.,])" if s[:1].isdigit() else r"(?<!\w)"
    end = r"(?!\d|[.,]\d)(?!\w)" if s[-1:].isdigit() else r"(?!\w)"
    return re.compile(start + body + end, re.IGNORECASE)


def active_patterns() -> list[tuple[re.Pattern[str], str]]:
    """The amount patterns plus the amounts the local model found in the current file (see
    llm_pii.extra_needles). Model strings go first, longest first; spaces in them match any run of
    whitespace, and a number never matches inside a longer number ("300.00" in "1,300.00")."""
    from .llm_pii import current_needles

    extra = [(_needle(s), label) for s, label in current_needles()]
    return extra + PATTERNS


def contains_pii(text: str) -> bool:
    return any(pattern.search(text) for pattern, _ in active_patterns())


def redact(text: str) -> tuple[str, int]:
    """Returns the redacted text and the number of replacements made."""
    count = 0
    for pattern, replacement in active_patterns():
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def find_pii(text: str) -> list[str]:
    """Every distinct string that one of the patterns matches, minus ones nested in another.

    "2887352.33" inside "PHP 2887352.33" is dropped: the longer one already covers its glyphs,
    and counting both would double-count one amount.
    """
    found = {m.group(0) for pattern, _ in active_patterns() for m in pattern.finditer(text)}
    return [n for n in found if not any(o != n and n in o for o in found)]
