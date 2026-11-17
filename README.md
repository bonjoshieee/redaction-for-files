# nas-redact (repo still named nas-s3-sync)

Writes a redacted twin next to each folder, on this PC or on the NAS:

```
\\192.168.0.50\CSD\
├─ MPAL\               original (only ever read)
└─ MPAL-redacted\      same structure, every file checked
```

Everything runs locally: regex, OCR (RapidOCR) and the optional language model (Ollama).
Nothing is sent to a cloud service.

## Usage

Install once: `ln -sf ~/nas-s3-sync/redact.sh ~/.local/bin/nas-redact`, then from any folder:

```bash
nas-redact "AP INVOICE"                      # -> AP INVOICE-redacted, next to it
nas-redact --dry-run "AP INVOICE"            # show what would happen, write nothing
nas-redact --share /mnt/nas/CSD              # a twin for every top-level folder in the share
nas-redact                                   # every share in config.toml
nas-redact --no-llm ...                      # regex + OCR only (fast; no Ollama)
nas-redact --force ...                       # redo files whose twin copy is up to date
```

`./redact.sh` in the repo does the same. It stops at once if Ollama isn't running (unless `--no-llm`).

Each run writes `reports/<date>_<time>.csv`: one row per file with its status, number of
redactions, a `needs_review` column and the reason a file was left out. Faded scans are listed
first. Exit code 1 if anything failed, 2 if Ollama stopped responding.

## What goes into the twin

| File | In the twin |
|---|---|
| PDF with text | Same PDF, matched text truly removed and boxed in black |
| Scanned PDF pages | Read with OCR; flagged lines blacked out in the image itself |
| `.docx` | Same Word file and layout; matched text replaced, pictures OCR'd |
| `.xlsx` | Same Excel file; text, money cells and dependent formulas replaced, pictures OCR'd |
| `.txt .csv .md .json .xml .html .log .yaml .tsv` | Same file, redacted |
| Supported file with nothing to hide | Copied unchanged, so the twin is complete |

**Left out** (listed in the report, never copied): images, other file types, password-protected
or corrupt files, scans with no readable text, workbooks with pivot tables, Word/Excel files with
vector pictures (EMF/WMF/SVG) or with sensitive text in a GIF/BMP/TIFF picture, files over
`max_redact_mb` (200 MB).

**Kept in step**: a copy is regenerated when its original changes (modification time), copies of
deleted originals are removed, and `X-redacted` folders whose `X` is gone are deleted. Folders
ending in `-redacted` are never read as input.

## What is hidden: money amounts only

Everything else stays visible: names, addresses, emails, phone numbers, TIN/SSS/PhilHealth,
bank account numbers, account codes, invoice/OR/voucher/reference numbers, dates, quantities and
percentages.

Two checks run on every file and anything either one flags is hidden:

1. **Local model** (Ollama, `gemma4`, on by default): reads the text and lists the money amounts
   (prices, totals, VAT/tax amounts, balances, payments, salaries, amounts in words, garbled
   handwritten amounts). Strings it invents are dropped. For Excel it judges each plain-number
   column from its header and sample values ("Amount" vs "Qty"). If Ollama can't be reached the
   run stops; use `--no-llm` to run with regex only.
2. **Regex backup**: ₱/PHP/$ amounts, `P500`, `12,500.00`, `1250.50`, amounts in words with a
   currency word. It can hide a few non-money numbers (an item number `1.01` written as text).

Excel also: cells with a currency/accounting format, and formulas that use a hidden cell, are
always hidden (no model needed). Percent-formatted cells are never treated as money. Without the
model, plain numbers with cents count as money.

Scans and pictures: the model and regex judge each OCR'd line; amounts in words and lines OCR
couldn't read clearly (usually handwriting) are blacked out too. Every scanned file is marked
**check by eye** in the report; faded thermal receipts can still leak.

## Setup

```bash
cp config.example.toml config.toml     # list the shares, Ollama settings
```

- Shares are mounted read/write under `/mnt/nas/<SHARE>` with a dedicated NAS user (e.g.
  `redactor`), credentials in `/etc/nas-credentials` (mode 600).
- Ollama: `ollama run gemma4 "hi"` must answer.
- The first run creates `.venv` and installs the dependencies (PyMuPDF, RapidOCR, lxml).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## S3 upload (off)

The original S3 sync (`./sync.sh`, `nas_s3_sync/sync.py`) is kept but not used. It reads
`config.s3.toml` (see `config.s3.example.toml`) and the IAM policy in `iam/`.
