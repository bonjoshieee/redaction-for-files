"""Uploads NAS folders to S3 and writes redacted copies under redacted/<same path>.

Only files whose key is missing in the bucket, or whose size differs, are uploaded. Nothing in
the bucket is ever deleted. Folders named "redacted" (any case, any depth) are never uploaded,
so redacted copies can't be redacted again.

For each new file the redacted copy is written first and the original second: if a run stops
in between, the original is still missing and the next run redoes both.
"""

from __future__ import annotations

import argparse
import csv
import mimetypes
import os
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .extract_office import extract_docx_text, extract_xlsx_text
from .redact import redact
from .redact_pdf import EncryptedPdfError, redact_pdf

REDACTED_PREFIX = "redacted/"
TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".xml", ".yaml", ".yml", ".html", ".log", ".md", ".tsv"}
DEFAULT_EXCLUDE_DIRS = ["@eaDir", "#recycle", "#snapshot"]


# --- config -------------------------------------------------------------------------------


@dataclass
class Config:
    bucket: str
    region: str
    profile: str | None
    folders: dict[str, Path]
    exclude_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    workers: int = 4
    max_redact_mb: int = 200


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    folders = {name: Path(p) for name, p in raw.get("folders", {}).items()}
    if not folders:
        raise SystemExit(f"{path}: no [folders] configured")
    for name in folders:
        if not name or "/" in name or name.lower() == "redacted":
            raise SystemExit(f"{path}: invalid folder name {name!r}")
    return Config(
        bucket=raw["bucket"],
        region=raw.get("region", "ap-southeast-1"),
        profile=raw.get("profile"),
        folders=folders,
        exclude_dirs=raw.get("exclude_dirs", list(DEFAULT_EXCLUDE_DIRS)),
        workers=int(raw.get("workers", 4)),
        max_redact_mb=int(raw.get("max_redact_mb", 200)),
    )


# --- classification -----------------------------------------------------------------------


def classify(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".xlsx"):
        return ext[1:]
    if ext in TEXT_EXTENSIONS:
        return "text"
    return None


def redacted_key_for(key: str, kind: str) -> str:
    if kind in ("docx", "xlsx"):
        return REDACTED_PREFIX + os.path.splitext(key)[0] + ".txt"
    return REDACTED_PREFIX + key


def content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# --- planning -----------------------------------------------------------------------------


@dataclass
class Item:
    local: Path
    key: str
    size: int


def list_existing(s3, bucket: str, prefix: str) -> dict[str, int]:
    existing: dict[str, int] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            existing[obj["Key"]] = obj["Size"]
    return existing


def walk(name: str, root: Path, exclude_dirs: list[str]) -> list[Item]:
    excluded = {d.lower() for d in exclude_dirs} | {"redacted"}
    items: list[Item] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in excluded)
        for fn in sorted(filenames):
            local = Path(dirpath) / fn
            if local.is_symlink() or not local.is_file():
                continue
            rel = local.relative_to(root).as_posix()
            items.append(Item(local, f"{name}/{rel}", local.stat().st_size))
    return items


def plan(items: list[Item], existing: dict[str, int]) -> list[Item]:
    return [i for i in items if existing.get(i.key) != i.size]


# --- processing ---------------------------------------------------------------------------


@dataclass
class Row:
    status: str  # redacted | no-pii | not-redactable | failed | dry-run
    local: str
    key: str
    size: int
    redactions: int = 0
    redacted_key: str = ""
    note: str = ""


def redact_file(kind: str, data: bytes) -> tuple[bytes, int]:
    if kind == "pdf":
        return redact_pdf(data)
    if kind == "docx":
        text = extract_docx_text(data)
    elif kind == "xlsx":
        text = extract_xlsx_text(data)
    else:
        text = data.decode("utf-8", "replace")
    out, count = redact(text)
    return out.encode("utf-8"), count


def process(s3, cfg: Config, item: Item) -> Row:
    row = Row("no-pii", str(item.local), item.key, item.size)
    kind = classify(item.local.name)
    try:
        body = None
        if kind is None:
            row.status, row.note = "not-redactable", "unsupported file type"
        elif item.size > cfg.max_redact_mb * 1024 * 1024:
            row.status, row.note = "not-redactable", f"larger than {cfg.max_redact_mb} MB"
        else:
            data = item.local.read_bytes()
            try:
                body, row.redactions = redact_file(kind, data)
            except EncryptedPdfError:
                row.status, row.note = "not-redactable", "password-protected pdf"
            except Exception as e:  # corrupt files still get uploaded, just not redacted
                row.status, row.note = "not-redactable", f"could not read: {type(e).__name__}: {e}"

        if body is not None and row.redactions > 0:
            row.status = "redacted"
            row.redacted_key = redacted_key_for(item.key, kind)
            if kind == "pdf":
                ctype = "application/pdf"
            elif kind == "text":
                ctype = content_type(item.local.name) + "; charset=utf-8"
            else:  # docx/xlsx copies are plain text
                ctype = "text/plain; charset=utf-8"
            s3.put_object(Bucket=cfg.bucket, Key=row.redacted_key, Body=body, ContentType=ctype)

        s3.upload_file(
            str(item.local),
            cfg.bucket,
            item.key,
            ExtraArgs={"ContentType": content_type(item.local.name)},
        )
    except Exception as e:
        row.status, row.note = "failed", f"{type(e).__name__}: {e}"
    return row


# --- run ----------------------------------------------------------------------------------


def run(s3, cfg: Config, only: list[str], dry_run: bool, log=print) -> list[Row]:
    unknown = [n for n in only if n not in cfg.folders]
    if unknown:
        raise SystemExit(f"not in config: {', '.join(unknown)} (known: {', '.join(cfg.folders)})")
    names = only or list(cfg.folders)

    rows: list[Row] = []
    lock = threading.Lock()
    for name in names:
        root = cfg.folders[name]
        if not root.is_dir():
            log(f"[{name}] SKIPPED: {root} is not a directory (is the NAS share mounted?)")
            rows.append(Row("failed", str(root), f"{name}/", 0, note="folder not found"))
            continue
        items = walk(name, root, cfg.exclude_dirs)
        todo = plan(items, list_existing(s3, cfg.bucket, f"{name}/"))
        log(f"[{name}] {len(items)} files, {len(todo)} new or changed")
        if dry_run:
            rows += [Row("dry-run", str(i.local), i.key, i.size) for i in todo]
            continue

        done = 0

        def work(item: Item) -> Row:
            nonlocal done
            row = process(s3, cfg, item)
            with lock:
                done += 1
                extra = f" ({row.redactions} redactions)" if row.redactions else ""
                note = f" - {row.note}" if row.note else ""
                log(f"  [{done}/{len(todo)}] {row.status}: {row.key}{extra}{note}")
            return row

        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            rows += list(pool.map(work, todo))
    return rows


def write_report(rows: list[Row], reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{datetime.now():%Y-%m-%d_%H%M%S}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "local_path", "key", "size", "redactions", "redacted_key", "note"])
        for r in rows:
            w.writerow([r.status, r.local, r.key, r.size, r.redactions, r.redacted_key, r.note])
    return path


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(prog="sync.sh", description=__doc__.splitlines()[0])
    ap.add_argument("folders", nargs="*", help="folder names from the config (default: all)")
    ap.add_argument("--config", type=Path, default=here / "config.toml")
    ap.add_argument("--dry-run", action="store_true", help="list what would upload; change nothing")
    args = ap.parse_args(argv)

    import boto3  # imported here so tests don't need AWS set up

    cfg = load_config(args.config)
    session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
    s3 = session.client("s3")

    rows = run(s3, cfg, args.folders, args.dry_run)
    report = write_report(rows, here / "reports")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing to do"
    print(f"\nDone: {summary}\nReport: {report}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
