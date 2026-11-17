import os
from pathlib import Path

import pymupdf
import pytest

from nas_s3_sync.redact_pdf import extract_pdf_text
from nas_s3_sync.twin import redact_folder


def write(path: Path, data: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def pdf(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), text)
    return doc.tobytes()


def scanned_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
    pix.clear_with(255)  # blank white scan: nothing readable
    page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


def quiet(*_):
    pass


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "AP INVOICE"
    write(root / "2026/JAN/A/1.pdf", pdf("TIN 123-456-789-000 total PHP 1,250.00"))
    write(root / "2026/JAN/A/2.pdf", pdf("Delivery receipt, thank you"))
    write(root / "2026/JAN/B/scan.pdf", scanned_pdf())
    write(root / "notes.txt", "mail ops@ceres.ph fee PHP 1,500.00")
    write(root / "photo.jpg", b"\xff\xd8binary")
    write(root / "@eaDir/thumb.txt", "ops@ceres.ph")
    write(root / "old-redacted/x.txt", "ops@ceres.ph")  # a twin inside: never read
    return root


def status(rows):
    return {r.path: r.status for r in rows}


def test_twin_is_written_next_to_the_folder(folder):
    rows = redact_folder(folder, log=quiet)
    twin = folder.with_name("AP INVOICE-redacted")
    files = sorted(p.relative_to(twin).as_posix() for p in twin.rglob("*") if p.is_file())
    assert files == ["2026/JAN/A/1.pdf", "2026/JAN/A/2.pdf", "notes.txt"]
    assert status(rows) == {
        "2026/JAN/A/1.pdf": "redacted",
        "2026/JAN/A/2.pdf": "clean",
        "2026/JAN/B/scan.pdf": "left-out",
        "notes.txt": "redacted",
        "photo.jpg": "left-out",
    }
    text = extract_pdf_text((twin / "2026/JAN/A/1.pdf").read_bytes())
    assert "123-456-789-000" in text and "1,250.00" not in text
    assert (twin / "notes.txt").read_text() == "mail ops@ceres.ph fee [AMOUNT_REDACTED]"
    assert (twin / "2026/JAN/A/2.pdf").read_bytes() == (folder / "2026/JAN/A/2.pdf").read_bytes()


def test_original_is_untouched(folder):
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    redact_folder(folder, log=quiet)
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


def test_scanned_pdf_reason_is_reported(folder):
    rows = redact_folder(folder, log=quiet)
    note = next(r.note for r in rows if r.path == "2026/JAN/B/scan.pdf")
    assert "scan" in note


def test_second_run_does_nothing(folder):
    redact_folder(folder, log=quiet)
    rows = redact_folder(folder, log=quiet)
    assert {r.status for r in rows} <= {"up-to-date", "left-out"}


def test_changed_original_is_regenerated(folder):
    redact_folder(folder, log=quiet)
    f = folder / "notes.txt"
    write(f, "now PHP 100.00 and PHP 200.00")
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10**9))
    rows = redact_folder(folder, log=quiet)
    assert status(rows)["notes.txt"] == "redacted"
    twin = folder.with_name("AP INVOICE-redacted")
    assert (twin / "notes.txt").read_text() == "now [AMOUNT_REDACTED] and [AMOUNT_REDACTED]"


def test_deleted_original_removes_twin_copy_and_empty_folders(folder):
    redact_folder(folder, log=quiet)
    (folder / "2026/JAN/A/1.pdf").unlink()
    (folder / "2026/JAN/A/2.pdf").unlink()
    rows = redact_folder(folder, log=quiet)
    twin = folder.with_name("AP INVOICE-redacted")
    assert {r.path for r in rows if r.status == "removed"} == {"2026/JAN/A/1.pdf", "2026/JAN/A/2.pdf"}
    assert not (twin / "2026").exists()


def test_dry_run_writes_nothing(folder):
    rows = redact_folder(folder, dry_run=True, log=quiet)
    assert not folder.with_name("AP INVOICE-redacted").exists()
    assert status(rows)["2026/JAN/A/1.pdf"] == "redacted"


def test_refuses_to_redact_a_twin(folder):
    redact_folder(folder, log=quiet)
    with pytest.raises(SystemExit):
        redact_folder(folder.with_name("AP INVOICE-redacted"), log=quiet)


def test_share_mode_twins_each_top_level_folder(tmp_path):
    from nas_s3_sync.twin import remove_orphan_twins, share_folders

    share = tmp_path / "CSD"
    write(share / "MPAL/a.txt", "mail ops@ceres.ph fee PHP 1,500.00")
    write(share / "GCHASPI/b.txt", "nothing")
    write(share / "loose.txt", "x")
    write(share / "@eaDir/t.txt", "x")
    write(share / "OLD-redacted/c.txt", "stale twin")  # original OLD is gone
    folders, loose = share_folders(share)
    assert [f.name for f in folders] == ["GCHASPI", "MPAL"]
    assert loose == ["loose.txt"]
    rows = remove_orphan_twins(share, log=quiet)
    assert [r.path for r in rows] == ["OLD-redacted"]
    assert not (share / "OLD-redacted").exists()
    for f in folders:
        redact_folder(f, log=quiet)
    assert (share / "MPAL-redacted/a.txt").read_text() == "mail ops@ceres.ph fee [AMOUNT_REDACTED]"
    assert (share / "GCHASPI-redacted/b.txt").read_text() == "nothing"
