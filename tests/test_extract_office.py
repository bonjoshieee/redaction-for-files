import io
import zipfile

from nas_s3_sync.extract_office import extract_docx_text, extract_xlsx_text


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_docx_text():
    data = make_zip({"word/document.xml": "<w:p><w:t>Email ops@ceres.ph &amp; TIN</w:t></w:p>"})
    assert extract_docx_text(data) == "Email ops@ceres.ph & TIN"


def test_docx_without_body_is_empty():
    assert extract_docx_text(make_zip({"other.xml": "<x/>"})) == ""


def test_xlsx_sheets_and_shared_strings():
    data = make_zip({
        "xl/worksheets/sheet1.xml": "<row><c><v>12,500.00</v></c></row>",
        "xl/sharedStrings.xml": "<sst><si><t>Juan</t></si><si><t xml:space=\"preserve\">a@b.ph</t></si></sst>",
    })
    assert extract_xlsx_text(data) == "12,500.00\nJuan a@b.ph"
