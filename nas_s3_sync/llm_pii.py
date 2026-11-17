"""Finds money amounts with a local model (Ollama, default gemma4). Nothing leaves this PC.

Only amounts are redacted. The model reads the text of a file (or the OCR text of a scanned page)
and lists the exact strings that are money: prices, totals, VAT/tax amounts, balances, payments,
salaries, amounts in words, garbled handwritten amounts. It must NOT list quantities,
percentages, invoice/receipt/voucher/account numbers, dates, phone numbers or IDs.

Every string it returns is checked against the text (anything made up is dropped) and then
redacted wherever it occurs. The regex amount patterns run as well, so a model miss is caught
by them and vice versa.

For Excel, judge_money_columns() asks once per sheet which plain-number columns hold money,
from each column's header and a few sample values.
"""

import contextlib
import contextvars
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

CATEGORIES = {
    "amounts": (
        "money amounts: prices, unit prices, totals, subtotals, VAT or tax amounts, discounts, "
        "balances, payments, fees, salaries, amounts written in words (e.g. 'One thousand two "
        "hundred pesos only'), and handwritten or garbled amounts next to words like Amount, "
        "Total, Payment, Due, Cash, Change. NOT: quantities, percentages, invoice/OR/receipt/"
        "voucher/reference/account numbers, account codes, dates, years, phone numbers, TIN or "
        "other IDs, ZIP codes, page numbers",
        "[AMOUNT_REDACTED]",
    ),
}
CHUNK_CHARS = 3000
MIN_SPLIT_CHARS = 300  # below this, a bad answer is an error, not a reason to split again
_NUMBER_WORD = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|million|billion|pesos?|centavos?)\b",
    re.IGNORECASE,
)


def plausible_amount(s: str) -> bool:
    """Drops answers that can't be an amount and would match too much: "1", "a", "only"."""
    digits = sum(c.isdigit() for c in s)
    return digits >= 2 or (digits == 0 and bool(_NUMBER_WORD.search(s)) and len(s) >= 4)


class LlmUnavailable(Exception):
    """Ollama is not reachable or failed; the run stops instead of silently skipping it."""


class LlmBadAnswer(LlmUnavailable):
    """The model's answer was not valid JSON (usually cut off because the list was too long)."""


@dataclass
class LlmConfig:
    url: str = "http://localhost:11434"
    model: str = "gemma4"
    timeout: int = 600


_current: contextvars.ContextVar[tuple[tuple[str, str], ...]] = contextvars.ContextVar(
    "extra_needles", default=()
)


_finder: contextvars.ContextVar = contextvars.ContextVar("page_finder", default=None)


def page_finder():
    """text -> [(string, label)] for scanned pages read during this file, or None (no model)."""
    return _finder.get()


@contextlib.contextmanager
def use_finder(fn):
    token = _finder.set(fn)
    try:
        yield
    finally:
        _finder.reset(token)


def current_needles() -> tuple[tuple[str, str], ...]:
    """(string, label) pairs found by the model for the file being redacted."""
    return _current.get()


@contextlib.contextmanager
def extra_needles(pairs):
    token = _current.set(tuple(pairs))
    try:
        yield
    finally:
        _current.reset(token)


def _prompt(text: str) -> str:
    lines = "\n".join(f"- {k}: {desc}" for k, (desc, _) in CATEGORIES.items())
    return (
        "You find money amounts in documents from a Philippine company (invoices, vouchers, "
        "receipts, statements, letters, payroll) so they can be blacked out. The text may come "
        "from OCR of scans and handwriting, so amounts can be garbled (e.g. '91.200.(' for "
        "'P1,200.00').\n"
        "Copy every amount EXACTLY as it appears in the text: same characters, spacing and "
        "punctuation, including a currency sign or word right before it. List each distinct "
        "amount once. Do not explain. Return an empty list when there is none.\n"
        f"{lines}\n\nTEXT:\n{text}"
    )


def _ask(cfg: LlmConfig, text: str) -> dict:
    schema = {
        "type": "object",
        "properties": {k: {"type": "array", "items": {"type": "string"}} for k in CATEGORIES},
        "required": list(CATEGORIES),
    }
    return _chat(cfg, _prompt(text), schema)


def _chat(cfg: LlmConfig, prompt: str, schema: dict) -> dict:
    body = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "format": schema,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 4096},
    }
    req = urllib.request.Request(
        cfg.url.rstrip("/") + "/api/chat",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            reply = json.load(r)
        return json.loads(reply["message"]["content"])
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LlmUnavailable(f"Ollama error {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LlmUnavailable(f"Ollama not reachable at {cfg.url}: {e}") from e
    except (KeyError, ValueError) as e:
        raise LlmBadAnswer(f"Ollama gave an unreadable answer: {e}") from e


def _chunks(text: str):
    """Splits on line breaks into pieces the model can read in one go."""
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > CHUNK_CHARS and buf:
            yield buf
            buf = ""
        buf += line
        while len(buf) > CHUNK_CHARS:
            yield buf[:CHUNK_CHARS]
            buf = buf[CHUNK_CHARS:]
    if buf.strip():
        yield buf


def find_pii_llm(text: str, cfg: LlmConfig, ask=None) -> list[tuple[str, str]]:
    """(exact string, label) pairs to hide, longest first. Only strings present in the text."""
    flat = re.sub(r"\s+", " ", text)
    found: dict[str, str] = {}
    def answers(chunk: str):
        try:
            yield (ask or _ask)(cfg, chunk)
        except LlmBadAnswer:
            if len(chunk) < MIN_SPLIT_CHARS:
                raise
            cut = chunk.rfind("\n", 0, len(chunk) // 2) + 1 or len(chunk) // 2
            yield from answers(chunk[:cut])
            yield from answers(chunk[cut:])

    for answer in (a for chunk in _chunks(text) for a in answers(chunk)):
        for key, (_, label) in CATEGORIES.items():
            for item in answer.get(key) or []:
                s = re.sub(r"\s+", " ", str(item)).strip()
                if plausible_amount(s) and (s in text or s in flat):
                    found.setdefault(s, label)
    return sorted(found.items(), key=lambda kv: -len(kv[0]))


# --- Excel columns ------------------------------------------------------------------------


def judge_money_columns(cfg: LlmConfig, columns: list[dict], chat=None) -> set[str]:
    """columns: [{"id": "Sheet1!C", "header": "Amount", "samples": [12500, 300]}].
    Returns the ids of the columns whose numbers are money."""
    if not columns:
        return set()
    listing = "\n".join(
        f'{c["id"]}: header "{c["header"]}", sample values {c["samples"]}' for c in columns
    )
    prompt = (
        "These are columns of plain numbers from spreadsheets of a Philippine company. Which "
        "columns hold MONEY (prices, amounts, totals, VAT/tax amounts, balances, payments, "
        "salaries, fees, costs)? Not money: quantities, counts, hours, days, rates in %, years, "
        "IDs, codes, phone numbers, measurements. When unsure and the header suggests money, "
        "say money. Answer with the ids of the money columns only.\n\n" + listing
    )
    schema = {
        "type": "object",
        "properties": {"money_columns": {"type": "array", "items": {"type": "string"}}},
        "required": ["money_columns"],
    }
    answer = (chat or _chat)(cfg, prompt, schema)
    ids = {c["id"] for c in columns}
    return {i for i in (answer.get("money_columns") or []) if i in ids}


_judge: contextvars.ContextVar = contextvars.ContextVar("column_judge", default=None)


def column_judge():
    """columns -> set of money column ids, or None when no model is used."""
    return _judge.get()


@contextlib.contextmanager
def use_column_judge(fn):
    token = _judge.set(fn)
    try:
        yield
    finally:
        _judge.reset(token)
