"""Usage:
  ./redact.sh [--dry-run] FOLDER [FOLDER ...]      FOLDER-redacted next to each FOLDER
  ./redact.sh [--dry-run] --share SHARE [SHARE ...] a twin for every top-level folder in SHARE
  ./redact.sh [--dry-run]                           every share listed in config.toml

Writes reports/<date>_<time>.csv.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from .llm_pii import LlmConfig, LlmUnavailable
from .twin import Row, redact_folder, remove_orphan_twins, share_folders, twin_of


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(prog="redact.sh", description="Write <folder>-redacted next to each folder.")
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--share", action="store_true", help="paths are shares: twin each top-level folder")
    ap.add_argument("--config", type=Path, default=here / "config.toml")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
    ap.add_argument("--max-mb", type=int, default=None)
    ap.add_argument("--no-llm", action="store_true", help="regex only; don't ask the Ollama model")
    ap.add_argument("--model", default=None, help="Ollama model (default gemma4)")
    ap.add_argument("--force", action="store_true", help="redo files even if their copy is up to date")
    args = ap.parse_args(argv)
    cfg: dict = {}

    max_mb = args.max_mb
    exclude = None
    if args.paths:
        shares = args.paths if args.share else []
        folders = [] if args.share else args.paths
        if args.config.exists():
            with open(args.config, "rb") as f:
                cfg = tomllib.load(f)
    else:
        if not args.config.exists():
            ap.error(f"no folders given and {args.config} not found (copy config.example.toml)")
        with open(args.config, "rb") as f:
            cfg = tomllib.load(f)
        shares = [Path(p).expanduser() for p in cfg.get("shares", [])]
        folders = [Path(p).expanduser() for p in cfg.get("folders", [])]
        if not shares and not folders:
            ap.error(f"{args.config}: no shares = [...] or folders = [...] listed")
        missing = [str(p) for p in shares + folders if not p.is_dir()]
        if missing:
            ap.error(f"{args.config}: not found (NAS not mounted?): {', '.join(missing)}")
        exclude = cfg.get("exclude_dirs")
        max_mb = max_mb or cfg.get("max_redact_mb")
    max_mb = max_mb or 200
    o = cfg.get("ollama", {})
    llm = None
    if not args.no_llm and o.get("enabled", True):
        llm = LlmConfig(
            url=o.get("url", LlmConfig.url),
            model=args.model or o.get("model", LlmConfig.model),
        )
        print(f"Local model: {llm.model} via Ollama at {llm.url}")

    rows: list[tuple[str, Row]] = []
    for share in shares:
        print(f"[share {share}]")
        tops, loose = share_folders(share, exclude)
        for name in loose:
            rows.append((str(share), Row("left-out", name, note="file at the top of the share (only folders get a twin)")))
        rows += [(str(share), r) for r in remove_orphan_twins(share, args.dry_run)]
        folders += tops
    try:
        for folder in folders:
            print(f"[{folder}] -> {twin_of(folder.resolve())}")
            rows += [
                (str(folder), r)
                for r in redact_folder(
                    folder, dry_run=args.dry_run, max_mb=max_mb, exclude_dirs=exclude,
                    llm=llm, force=args.force,
                )
            ]
    except LlmUnavailable as e:
        print(f"\nSTOPPED: {e}\nFix Ollama (ollama run {llm.model} \"hi\") or rerun with --no-llm.")
        return 2

    reports = here / "reports"
    reports.mkdir(exist_ok=True)
    report = reports / f"{datetime.now():%Y-%m-%d_%H%M%S}.csv"
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["folder", "status", "path", "redactions", "needs_review", "note"])
        # faded scans first, then other scans, then the rest
        order = lambda fr: (0 if fr[1].review.startswith("FAINT") else 1 if fr[1].review else 2)
        for folder, r in sorted(rows, key=order):
            w.writerow([folder, r.status, r.path, r.redactions, r.review, r.note])

    counts: dict[str, int] = {}
    for _, r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"\nDone: {', '.join(f'{n} {s}' for s, n in sorted(counts.items())) or 'nothing to do'}")
    review = [r for _, r in rows if r.review]
    if review:
        faint = sum(1 for r in review if r.review.startswith("FAINT"))
        print(f"Check by eye before sharing: {len(review)} scanned file(s), {faint} with faded pages (listed first in the report)")
    print(f"Report: {report}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
