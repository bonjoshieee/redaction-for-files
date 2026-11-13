"""Plain text from .docx and .xlsx files (redacted copies of these are written as .txt)."""

import io
import re
import zipfile
from html import unescape

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SHEET = re.compile(r"^xl/worksheets/sheet\d+\.xml$")
_SHARED_STRING = re.compile(r"<t[^>]*>([^<]*)</t>")


def _strip_xml(xml: str) -> str:
    return _SPACE.sub(" ", unescape(_TAG.sub(" ", xml))).strip()


def extract_docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" not in z.namelist():
            return ""
        return _strip_xml(z.read("word/document.xml").decode("utf-8", "replace"))


def extract_xlsx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        texts = [
            _strip_xml(z.read(n).decode("utf-8", "replace")) for n in names if _SHEET.match(n)
        ]
        if "xl/sharedStrings.xml" in names:
            ss = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            shared = [unescape(s) for s in _SHARED_STRING.findall(ss)]
            if shared:
                texts.append(" ".join(shared))
        return "\n".join(texts)
