from pathlib import Path

import pymupdf
import pytest

from nas_s3_sync.sync import Config, load_config, run


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None, fail_keys=()):
        self.objects = dict(objects or {})
        self.types: dict[str, str] = {}
        self.fail_keys = set(fail_keys)

    def get_paginator(self, _name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                yield {"Contents": [{"Key": k, "Size": len(fake.objects[k])} for k in keys]}

        return P()

    def put_object(self, Bucket, Key, Body, ContentType):
        if Key in self.fail_keys:
            raise RuntimeError("boom")
        self.objects[Key] = Body
        self.types[Key] = ContentType

    def upload_file(self, Filename, Bucket, Key, ExtraArgs):
        if Key in self.fail_keys:
            raise RuntimeError("boom")
        self.objects[Key] = Path(Filename).read_bytes()
        self.types[Key] = ExtraArgs["ContentType"]


def write(path: Path, data: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


def pdf(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), text)
    return doc.tobytes()


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "MPAL"
    write(root / "notes.txt", "mail ops@ceres.ph fee PHP 1,500.00")
    write(root / "plain.txt", "nothing here")
    write(root / "sub/slip.pdf", pdf("Total PHP 1,250.00"))
    write(root / "sub/photo.jpg", b"\xff\xd8binary")
    write(root / "sub/~tmp.temp", b"scratch")
    write(root / "redacted/old.txt", "ops@ceres.ph")       # never uploaded
    write(root / "deep/Redacted/x.txt", "ops@ceres.ph")    # any case, any depth
    write(root / "@eaDir/thumb.jpg", b"synology")          # Synology metadata
    return root


def cfg_for(*roots: Path) -> Config:
    return Config(bucket="b", region="r", profile=None, folders={r.name: r for r in roots}, workers=2)


def statuses(rows):
    return {r.key: r.status for r in rows}


def test_uploads_everything_and_redacts_supported_files(share):
    s3 = FakeS3()
    rows = run(s3, cfg_for(share), [], dry_run=False, log=lambda *_: None)

    assert set(s3.objects) == {
        "MPAL/notes.txt", "MPAL/plain.txt", "MPAL/sub/slip.pdf", "MPAL/sub/photo.jpg",
        "MPAL/sub/~tmp.temp",
        "redacted/MPAL/notes.txt", "redacted/MPAL/sub/slip.pdf",
    }
    assert s3.objects["redacted/MPAL/notes.txt"] == b"mail ops@ceres.ph fee [AMOUNT_REDACTED]"
    assert statuses(rows) == {
        "MPAL/notes.txt": "redacted",
        "MPAL/plain.txt": "no-pii",
        "MPAL/sub/slip.pdf": "redacted",
        "MPAL/sub/photo.jpg": "not-redactable",
        "MPAL/sub/~tmp.temp": "not-redactable",
    }
    assert s3.types["MPAL/sub/slip.pdf"] == "application/pdf"


def test_second_run_uploads_nothing(share):
    s3 = FakeS3()
    run(s3, cfg_for(share), [], dry_run=False, log=lambda *_: None)
    assert run(s3, cfg_for(share), [], dry_run=False, log=lambda *_: None) == []


def test_changed_size_is_uploaded_again(share):
    s3 = FakeS3()
    run(s3, cfg_for(share), [], dry_run=False, log=lambda *_: None)
    write(share / "plain.txt", "now with PHP 9,000.00")
    rows = run(s3, cfg_for(share), [], dry_run=False, log=lambda *_: None)
    assert statuses(rows) == {"MPAL/plain.txt": "redacted"}
    assert s3.objects["redacted/MPAL/plain.txt"] == b"now with [AMOUNT_REDACTED]"


def test_office_copies_are_txt(tmp_path):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:t>SSS 12-3456789-0 fee PHP 2,000.00</w:t>")
    root = tmp_path / "HR"
    write(root / "form.docx", buf.getvalue())
    s3 = FakeS3()
    run(s3, cfg_for(root), [], dry_run=False, log=lambda *_: None)
    assert s3.objects["redacted/HR/form.txt"] == b"SSS 12-3456789-0 fee [AMOUNT_REDACTED]"
    assert s3.types["redacted/HR/form.txt"].startswith("text/plain")


def test_dry_run_changes_nothing(share):
    s3 = FakeS3()
    rows = run(s3, cfg_for(share), [], dry_run=True, log=lambda *_: None)
    assert s3.objects == {}
    assert {r.status for r in rows} == {"dry-run"}
    assert len(rows) == 5


def test_only_named_folder(share, tmp_path):
    other = tmp_path / "YARENA"
    write(other / "a.txt", "x")
    s3 = FakeS3()
    run(s3, cfg_for(share, other), ["YARENA"], dry_run=False, log=lambda *_: None)
    assert set(s3.objects) == {"YARENA/a.txt"}


def test_unknown_folder_name_is_an_error(share):
    with pytest.raises(SystemExit):
        run(FakeS3(), cfg_for(share), ["NOPE"], dry_run=False, log=lambda *_: None)


def test_failed_redacted_copy_leaves_original_unuploaded_for_retry(share):
    s3 = FakeS3(fail_keys={"redacted/MPAL/notes.txt"})
    rows = run(s3, cfg_for(share), [], dry_run=False, log=lambda *_: None)
    assert statuses(rows)["MPAL/notes.txt"] == "failed"
    assert "MPAL/notes.txt" not in s3.objects


def test_unreadable_pdf_is_still_uploaded(tmp_path):
    root = tmp_path / "X"
    write(root / "broken.pdf", b"not a pdf at all")
    s3 = FakeS3()
    rows = run(s3, cfg_for(root), [], dry_run=False, log=lambda *_: None)
    assert rows[0].status == "not-redactable"
    assert "X/broken.pdf" in s3.objects


def test_missing_mount_is_reported(tmp_path):
    rows = run(FakeS3(), cfg_for(tmp_path / "GONE"), [], dry_run=False, log=lambda *_: None)
    assert rows[0].status == "failed"


def test_config_rejects_folder_named_redacted(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('bucket = "b"\n[folders]\nredacted = "/x"\n')
    with pytest.raises(SystemExit):
        load_config(p)


def test_example_config_loads():
    cfg = load_config(Path(__file__).parent.parent / "config.s3.example.toml")
    assert cfg.bucket == "envicomm-nas-datahub"
    assert cfg.profile == "nas-s3-sync"
