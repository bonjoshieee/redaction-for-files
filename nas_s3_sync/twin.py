"""Writes a redacted twin of a folder next to it: <folder> -> <folder>-redacted.

Every file in the twin has been checked:
- supported files are redacted, or copied unchanged when nothing was found;
- files that can't be checked (images, scanned or password-protected PDFs, unsupported types,
  files over the size limit, unreadable files) are left out and reported.

The twin is kept in step with the original: a file is regenerated when its original's
modification time changes, and twin files whose original is gone (or is now left out) are
deleted. The original folder is only ever read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .redact import redact
from .ocr_pdf import ScanNotConfident, scan_log
from .extract_office import extract_docx_text, xlsx_text_cells
from .llm_pii import (
    LlmConfig, LlmUnavailable, extra_needles, find_pii_llm, judge_money_columns, use_column_judge,
    use_finder,
)
from .office_redact import OfficeNotSupported, redact_docx, redact_xlsx
from .redact_pdf import EncryptedPdfError, pdf_unreadable_pages, redact_pdf

TWIN_SUFFIX = "-redacted"
TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".xml", ".yaml", ".yml", ".html", ".log", ".md", ".tsv"}
DEFAULT_EXCLUDE_DIRS = ["@eaDir", "#recycle", "#snapshot", ".@__thumb", "@Recycle"]


def twin_of(folder: Path) -> Path:
    return folder.with_name(folder.name + TWIN_SUFFIX)


def is_twin(name: str) -> bool:
    return name.lower().endswith(TWIN_SUFFIX)


def kind_of(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".xlsx"):
        return ext[1:]
    if ext in TEXT_EXTENSIONS:
        return "text"
    return None


@dataclass
class Row:
    status: str  # redacted | clean | left-out | removed | up-to-date | failed | dry-run
    path: str  # relative to the folder
    redactions: int = 0
    note: str = ""
    review: str = ""  # why a person should look at the redacted copy before sharing it


class LeftOut(Exception):
    """The file can't be checked, so it must not appear in the twin."""


def redact_bytes(kind: str, data: bytes) -> tuple[bytes, int]:
    if kind == "pdf":
        try:
            unreadable = pdf_unreadable_pages(data)
        except EncryptedPdfError:
            raise LeftOut("password-protected pdf")
        if unreadable:
            try:
                return redact_pdf(data, ocr_pages=unreadable)
            except ScanNotConfident as e:
                raise LeftOut(f"scan could not be read, {e}")
        return redact_pdf(data)
    if kind in ("docx", "xlsx"):
        try:
            return redact_docx(data) if kind == "docx" else redact_xlsx(data)
        except OfficeNotSupported as e:
            raise LeftOut(str(e))
    text = data.decode("utf-8", "replace")
    out, count = redact(text)
    return out.encode("utf-8"), count


def file_text(kind: str, data: bytes) -> str:
    """Text the local model reads. Scanned PDF pages are read later, page by page, after OCR."""
    try:
        if kind == "pdf":
            import pymupdf

            with pymupdf.open(stream=data, filetype="pdf") as doc:
                return "\n".join(page.get_text("text") for page in doc)
        if kind == "docx":
            return extract_docx_text(data)
        if kind == "xlsx":
            return xlsx_text_cells(data)
        return data.decode("utf-8", "replace")
    except Exception:
        return ""  # unreadable files are caught by redact_bytes


def redact_with_model(kind: str, data: bytes, llm: LlmConfig | None) -> tuple[bytes, int]:
    if llm is None:
        return redact_bytes(kind, data)
    text = file_text(kind, data)
    pairs = find_pii_llm(text, llm) if text.strip() else []
    with (
        extra_needles(pairs),
        use_finder(lambda t: find_pii_llm(t, llm)),
        use_column_judge(lambda cols: judge_money_columns(llm, cols)),
    ):
        return redact_bytes(kind, data)


def review_note(log: list) -> str:
    """Scanned pages are redacted from OCR, which can miss things: ask for a look by eye."""
    if not log:
        return ""
    pages = sorted({n for n, _ in log})
    faint = sorted({n for n, f in log if f})
    note = "scan: check by eye (pages " + ", ".join(map(str, pages)) + ")"
    if faint:
        note = "FAINT " + note + "; faded pages " + ", ".join(map(str, faint))
    return note


def _walk(root: Path, exclude_dirs: list[str]):
    excluded = {d.lower() for d in exclude_dirs}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d.lower() not in excluded and not is_twin(d)
        )
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            yield p


def _up_to_date(src: Path, dst: Path) -> bool:
    try:
        return dst.stat().st_mtime_ns == src.stat().st_mtime_ns
    except FileNotFoundError:
        return False


def _write(dst: Path, body: bytes, src: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp")
    tmp.write_bytes(body)
    st = src.stat()
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))  # marks the copy as matching this original
    os.replace(tmp, dst)


def redact_folder(
    src: Path,
    dry_run: bool = False,
    max_mb: int = 200,
    exclude_dirs: list[str] | None = None,
    log=print,
    llm: LlmConfig | None = None,
    force: bool = False,
) -> list[Row]:
    """llm: also ask the local Ollama model which strings and Excel columns are money amounts.
    force: redo files even when their twin copy is up to date (e.g. after turning the model on).
    Raises LlmUnavailable when the model can't be reached, so no file is redacted without it."""
    src = src.resolve()
    if is_twin(src.name):
        raise SystemExit(f"{src} is itself a redacted twin")
    if not src.is_dir():
        raise SystemExit(f"{src} is not a folder")
    dst_root = twin_of(src)
    exclude = DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs

    rows: list[Row] = []
    keep: set[Path] = set()
    files = list(_walk(src, exclude))
    for n, f in enumerate(files, 1):
        rel = f.relative_to(src)
        dst = dst_root / rel
        row = Row("clean", rel.as_posix())
        try:
            kind = kind_of(f.name)
            if kind is None:
                raise LeftOut("unsupported file type")
            if f.stat().st_size > max_mb * 1024 * 1024:
                raise LeftOut(f"larger than {max_mb} MB")
            keep.add(dst)
            if not force and _up_to_date(f, dst):
                row.status = "up-to-date"
                rows.append(row)
                continue
            scanned: list = []
            token = scan_log.set(scanned)
            try:
                body, row.redactions = redact_with_model(kind, f.read_bytes(), llm)
            except (LeftOut, LlmUnavailable):
                raise
            except Exception as e:
                raise LeftOut(f"could not read: {type(e).__name__}: {e}")
            finally:
                scan_log.reset(token)
            row.review = review_note(scanned)
            row.status = "redacted" if row.redactions else "clean"
            if dry_run:
                row.note = "dry run, nothing written"
            else:
                _write(dst, body, f)
        except LeftOut as e:
            keep.discard(dst)
            row.status, row.note = "left-out", str(e)
        except LlmUnavailable:
            raise  # stop the run (and skip the clean-up pass, which needs every file seen)
        except Exception as e:
            keep.discard(dst)
            row.status, row.note = "failed", f"{type(e).__name__}: {e}"
        rows.append(row)
        if row.status != "up-to-date":
            extra = f" ({row.redactions} redactions)" if row.redactions else ""
            note = f" - {row.note}" if row.note else ""
            rev = f" [{row.review}]" if row.review else ""
            log(f"  [{n}/{len(files)}] {row.status}: {row.path}{extra}{note}{rev}")

    # Delete twin files whose original is gone or is now left out, then empty folders.
    if dst_root.is_dir():
        for dirpath, _dirs, filenames in os.walk(dst_root, topdown=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                if p not in keep:
                    rows.append(Row("removed", p.relative_to(dst_root).as_posix()))
                    if dry_run:
                        rows[-1].note = "dry run, nothing deleted"
                    else:
                        p.unlink()
                    log(f"  removed: {rows[-1].path}")
            d = Path(dirpath)
            if not dry_run and d != dst_root and not any(d.iterdir()):
                d.rmdir()
    return rows


def share_folders(share: Path, exclude_dirs: list[str] | None = None) -> tuple[list[Path], list[str]]:
    """Top-level folders of a share that get a twin, and loose top-level files (no twin)."""
    share = share.resolve()
    if not share.is_dir():
        raise SystemExit(f"{share} is not a folder (is the NAS share mounted?)")
    excluded = {d.lower() for d in (DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs)}
    folders, loose = [], []
    for p in sorted(share.iterdir()):
        if p.is_symlink() or p.name.startswith("."):
            continue
        if p.is_dir():
            if not is_twin(p.name) and p.name.lower() not in excluded:
                folders.append(p)
        elif p.is_file():
            loose.append(p.name)
    return folders, loose


def remove_orphan_twins(share: Path, dry_run: bool = False, log=print) -> list[Row]:
    """Deletes <X>-redacted folders whose original folder <X> no longer exists."""
    rows = []
    for p in sorted(share.resolve().iterdir()):
        if p.is_dir() and is_twin(p.name):
            original = p.with_name(p.name[: -len(TWIN_SUFFIX)])
            if not original.exists():
                rows.append(Row("removed", p.name, note="original folder is gone"))
                log(f"  removed twin {p.name} (original folder is gone)")
                if not dry_run:
                    import shutil

                    shutil.rmtree(p)
    return rows
