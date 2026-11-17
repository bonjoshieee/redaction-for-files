import io
import zipfile

import openpyxl
import pytest
from openpyxl.chart import BarChart, Reference

from nas_s3_sync.office_redact import OfficeNotSupported, redact_docx, redact_xlsx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(body: str, extra: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
        for name, content in (extra or {}).items():
            z.writestr(name, content)
    return buf.getvalue()


def part(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode()


def run(text: str, bold: bool = False) -> str:
    props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>'


# --- Word ---------------------------------------------------------------------------------


def test_docx_stays_docx_and_keeps_formatting():
    data = docx(f"<w:p>{run('Total: ')}{run('PHP 2,500.00', bold=True)}{run(' juan@example.com')}</w:p>")
    out, n = redact_docx(data)
    xml = part(out, "word/document.xml")
    assert n == 1
    assert "2,500.00" not in xml
    assert "[AMOUNT_REDACTED]" in xml and "juan@example.com" in xml
    assert "<w:b/>" in xml  # bold run still there


def test_docx_match_split_across_runs():
    data = docx(f"<w:p>{run('Total PHP 1,')}{run('250.00', bold=True)}{run(' due')}</w:p>")
    out, n = redact_docx(data)
    xml = part(out, "word/document.xml")
    assert n == 1
    assert "250.00" not in xml and "1," not in xml
    assert "Total [AMOUNT_REDACTED]" in xml and " due" in xml


def test_docx_headers_footers_and_mailto_links():
    data = docx(
        f"<w:p>{run('hello')}</w:p>",
        {
            "word/footer1.xml": f"<w:ftr {W}><w:p>{run('TIN 123-456-789-000 balance PHP 7,000.00')}</w:p></w:ftr>",
            "word/_rels/document.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="hyperlink" Target="mailto:ops@ceres.ph" TargetMode="External"/>'
                "</Relationships>"
            ),
        },
    )
    out, n = redact_docx(data)
    assert n == 1
    footer = part(out, "word/footer1.xml")
    assert "7,000.00" not in footer and "123-456-789-000" in footer
    assert "ops@ceres.ph" in part(out, "word/_rels/document.xml.rels")


def test_docx_without_matches_is_returned_unchanged():
    data = docx(f"<w:p>{run('Nothing to see')}</w:p>")
    assert redact_docx(data) == (data, 0)


# --- Excel --------------------------------------------------------------------------------


def xlsx(build) -> bytes:
    wb = openpyxl.Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def load(data: bytes, data_only=False):
    return openpyxl.load_workbook(io.BytesIO(data), data_only=data_only)


def test_xlsx_text_cells_redacted_and_file_still_opens():
    def build(wb):
        ws = wb.active
        ws["A1"] = "Contact juan@example.com fee PHP 1,500.00"
        ws["A2"] = "SSS 12-3456789-0"
        ws["A3"] = "Remarks"
    out, n = redact_xlsx(xlsx(build))
    ws = load(out).active
    assert n == 1
    assert ws["A1"].value == "Contact juan@example.com fee [AMOUNT_REDACTED]"
    assert ws["A2"].value == "SSS 12-3456789-0"
    assert ws["A3"].value == "Remarks"


def test_xlsx_money_formatted_numbers_and_dependent_formulas():
    def build(wb):
        ws = wb.active
        ws["A1"], ws["B1"] = "Item", "Amount"
        ws["B2"], ws["B3"] = 12500, 300.5
        for c in ("B2", "B3"):
            ws[c].number_format = '"₱"#,##0.00'
        ws["B4"] = "=SUM(B2:B3)"      # total of redacted cells: must go too
        ws["C2"] = 42                 # plain number, General format: kept
        ws["C3"] = "=C2*2"            # formula on kept cells: kept
        ws["D1"] = "=B4/2"            # formula on a redacted formula: goes too
    out, n = redact_xlsx(xlsx(build))
    ws = load(out).active
    assert ws["B2"].value == ws["B3"].value == "[AMOUNT_REDACTED]"
    assert ws["B4"].value == "[AMOUNT_REDACTED]"
    assert ws["D1"].value == "[AMOUNT_REDACTED]"
    assert ws["C2"].value == 42
    assert ws["C3"].value == "=C2*2"
    assert ws["A1"].value == "Item"
    assert n == 4


def test_xlsx_cross_sheet_formula_is_followed():
    def build(wb):
        a = wb.active
        a.title = "Payroll Jan"
        a["A1"] = 25000
        a["A1"].number_format = "#,##0.00"
        b = wb.create_sheet("Summary")
        b["A1"] = "='Payroll Jan'!A1"
    out, _ = redact_xlsx(xlsx(build))
    assert load(out)["Summary"]["A1"].value == "[AMOUNT_REDACTED]"


def test_xlsx_chart_cache_removed():
    def build(wb):
        ws = wb.active
        for i, v in enumerate([100, 200, 300], start=1):
            ws.cell(i, 1, v).number_format = "#,##0.00"
        ch = BarChart()
        ch.add_data(Reference(ws, min_col=1, min_row=1, max_row=3))
        ws.add_chart(ch, "C1")
    out, _ = redact_xlsx(xlsx(build))
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        for name in z.namelist():
            if name.startswith("xl/charts/chart"):
                assert b"numCache" not in z.read(name)


def test_xlsx_with_pivot_table_is_refused():
    data = xlsx(lambda wb: None)
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for i in zin.infolist():
            zout.writestr(i, zin.read(i.filename))
        zout.writestr("xl/pivotCache/pivotCacheRecords1.xml", "<x/>")
    with pytest.raises(OfficeNotSupported):
        redact_xlsx(buf.getvalue())


def test_xlsx_without_matches_is_returned_unchanged():
    data = xlsx(lambda wb: wb.active.__setitem__("A1", "hello"))
    assert redact_xlsx(data) == (data, 0)


def test_docx_hyperlink_field_code_email_is_kept():
    instr = '<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:finance@example.com" \\h </w:instrText></w:r>'
    out, n = redact_docx(docx(f"<w:p>{instr}{run('Email us')}</w:p>"))
    assert n == 0 and "finance@example.com" in part(out, "word/document.xml")  # not an amount


def test_docx_amount_followed_by_comma():
    out, _ = redact_docx(docx(f"<w:p>{run('amounting to ')}{run('₱', bold=True)}{run('419,837.17')}{run(', detailed')}</w:p>"))
    xml = part(out, "word/document.xml")
    assert "419,837.17" not in xml and ", detailed" in xml


def test_xlsx_plain_numbers_with_cents_are_amounts_whole_numbers_are_not():
    def build(wb):
        ws = wb.active
        ws["A1"], ws["A2"], ws["A3"], ws["A4"] = 8333.33, 1250.5, 2026, 0.75
    ws = load(redact_xlsx(xlsx(build))[0]).active
    assert ws["A1"].value == ws["A2"].value == "[AMOUNT_REDACTED]"
    assert ws["A3"].value == 2026 and ws["A4"].value == 0.75


def _image_with_text(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=420, height=80)
    page.insert_text((10, 45), text, fontsize=20)
    return page.get_pixmap(dpi=150).tobytes("png")


def test_text_in_a_pasted_image_is_blacked_out():
    import pymupdf

    from nas_s3_sync.ocr_pdf import _ocr

    data = docx(f"<w:p>{run('see letterhead')}</w:p>", {"word/media/image1.png": "PLACEHOLDER"})
    img = _image_with_text("Total: PHP 1,250.00")
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for i in zin.infolist():
            zout.writestr(i, img if i.filename.endswith(".png") else zin.read(i.filename))
    out, n = redact_docx(buf.getvalue())
    assert n >= 1
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        after = z.read("word/media/image1.png")
    texts = " ".join(_ocr()(after).txts or ())
    assert "1,250.00" not in texts
    assert pymupdf.Pixmap(after).w == pymupdf.Pixmap(img).w  # same picture size


def test_logo_without_text_is_untouched():
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 60), False)
    pix.clear_with(200)
    logo = pix.tobytes("png")
    data = docx(f"<w:p>{run('a@b.ph')}</w:p>", {"word/media/logo.png": logo})
    out, _ = redact_docx(data)
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert z.read("word/media/logo.png") == logo


def test_vector_image_is_refused():
    data = docx(f"<w:p>{run('hello')}</w:p>", {"word/media/image1.emf": "x"})
    with pytest.raises(OfficeNotSupported):
        redact_docx(data)


def test_review_note_flags_faded_pages():
    from nas_s3_sync.twin import review_note

    assert review_note([]) == ""
    assert review_note([(1, False), (1, False)]) == "scan: check by eye (pages 1)"
    assert review_note([(1, False), (2, True)]).startswith("FAINT scan: check by eye (pages 1, 2)")


def test_transparent_picture_keeps_its_background():
    import numpy as np
    import pymupdf

    from nas_s3_sync.ocr_pdf import redact_image

    src = pymupdf.Pixmap(_image_with_text("Total: PHP 1,250.00"))
    rgb = np.frombuffer(src.samples, dtype=np.uint8).reshape(src.h, src.w, src.n)[..., :3]
    alpha = np.where(rgb.mean(axis=2) > 200, 0, 255).astype(np.uint8)  # white -> transparent
    rgba = np.dstack([rgb, alpha])
    png = pymupdf.Pixmap(pymupdf.csRGB, src.w, src.h, rgba.tobytes(), True).tobytes("png")
    out, n = redact_image(png, ".png")
    after = pymupdf.Pixmap(out)
    a = np.frombuffer(after.samples, dtype=np.uint8).reshape(after.h, after.w, 4)[..., 3]
    assert n >= 1 and after.alpha
    assert (a == 0).mean() > 0.5  # most of the picture is still transparent, not black
