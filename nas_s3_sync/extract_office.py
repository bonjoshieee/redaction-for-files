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


_INLINE = re.compile(r"<is>(.*?)</is>", re.S)


def xlsx_text_cells(data: bytes) -> str:
    """Only the text cells of a workbook (shared and inline strings), one per line. Numbers are
    left out: number cells are judged by format, cents and column header instead."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        out = []
        if "xl/sharedStrings.xml" in names:
            ss = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            for si in re.findall(r"<si>(.*?)</si>", ss, re.S):
                t = "".join(unescape(x) for x in _SHARED_STRING.findall(si)).strip()
                if t:
                    out.append(t)
        for n in names:
            if _SHEET.match(n):
                for block in _INLINE.findall(z.read(n).decode("utf-8", "replace")):
                    t = "".join(unescape(x) for x in _SHARED_STRING.findall(block)).strip()
                    if t:
                        out.append(t)
        return "\n".join(dict.fromkeys(out))
