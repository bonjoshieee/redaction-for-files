"""Redaction inside .docx and .xlsx files that keeps them as Word/Excel documents.

The files are zip archives of XML. Only the text nodes are changed, so layout, styles, tables
and images stay as they were.

Word and Excel split text into runs ("PHP 1," in one run, "250.00" in the next when part of it is
bold), so matching is done on the joined text of a paragraph or cell, and each replacement is
written into the run where the match starts while the rest of the match is removed from the
following runs.

Excel also stores numbers as numbers: 12500 shown as "PHP 12,500.00" is a number cell with a
currency format, which no text pattern sees. Number cells with a currency or accounting format or
with cents, and plain-number columns the local model judges to be money (from the header), are
replaced by [AMOUNT_REDACTED], and so is every formula that refers to a redacted cell
(its cached result would show the amount). Charts keep their own copy of the numbers, which is
removed so Excel redraws them from the redacted cells. Workbooks with pivot tables carry a full
copy of the source data and are refused (OfficeNotSupported).
"""

from __future__ import annotations

import io
import re
import zipfile

from lxml import etree

from .llm_pii import column_judge
from .redact import active_patterns, redact

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
AMOUNT = "[AMOUNT_REDACTED]"


class OfficeNotSupported(Exception):
    """The file holds data this redaction can't safely clean."""


# --- runs ---------------------------------------------------------------------------------


def redact_runs(nodes: list[etree._Element]) -> int:
    """Redacts the joined text of text nodes (one paragraph / one cell) in place."""
    chars: list[str] = []
    owner: list[int] = []
    for i, n in enumerate(nodes):
        t = n.text or ""
        chars.extend(t)
        owner.extend([i] * len(t))
    text = "".join(chars)
    count = 0
    for pattern, label in active_patterns():
        out_chars: list[str] = []
        out_owner: list[int] = []
        pos = 0
        for m in pattern.finditer(text):
            out_chars.extend(chars[pos : m.start()])
            out_owner.extend(owner[pos : m.start()])
            out_chars.extend(label)
            out_owner.extend([owner[m.start()]] * len(label))
            pos = m.end()
            count += 1
        if pos:
            out_chars.extend(chars[pos:])
            out_owner.extend(owner[pos:])
            chars, owner = out_chars, out_owner
            text = "".join(chars)
    if count:
        parts = [""] * len(nodes)
        for ch, i in zip(chars, owner):
            parts[i] += ch
        for n, t in zip(nodes, parts):
            if (n.text or "") != t:
                n.text = t
                n.set(XML_SPACE, "preserve")
    return count


def _parse(data: bytes) -> etree._ElementTree:
    return etree.fromstring(data, etree.XMLParser(remove_blank_text=False, resolve_entities=False, huge_tree=True)).getroottree()


def _dump(tree: etree._ElementTree) -> bytes:
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _redact_rels(data: bytes) -> tuple[bytes, int]:
    """mailto: links and other targets that carry an email."""
    tree = _parse(data)
    count = 0
    for rel in tree.getroot():
        target = rel.get("Target") or ""
        new, n = redact(target)
        if n:
            rel.set("Target", new.replace("[", "%5B").replace("]", "%5D"))
            count += n
    return (_dump(tree), count) if count else (data, 0)


def _rewrite_zip(data: bytes, edit, force: bool = False) -> tuple[bytes, int]:
    """Copies the zip, letting edit(name, bytes) return (new_bytes | None to drop, count).
    Returns the original bytes when nothing was counted, unless force is set."""
    total = 0
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            new, n = edit(info.filename, body)
            total += n
            if new is None:
                continue
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zout.writestr(zi, new)
    return (out.getvalue(), total) if total or force else (data, 0)


# --- embedded images -----------------------------------------------------------------------

_MEDIA = re.compile(r"^(?:word|xl)/media/[^/]+$")


def _redact_media(name: str, body: bytes) -> tuple[bytes, int]:
    """Pictures pasted into a document (letterheads, scanned IDs, screenshots) are OCR'd and
    flagged lines blacked out. Vector images (EMF/WMF/SVG) can't be checked: refused."""
    from .ocr_pdf import IMAGE_FORMATS, VECTOR_IMAGES, redact_image

    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in VECTOR_IMAGES:
        raise OfficeNotSupported(f"contains a vector image ({ext}) whose text can't be checked")
    try:
        if ext not in IMAGE_FORMATS:  # gif, bmp, tiff: checked, but can't be written back
            _, n = redact_image(body, ".png")
            if n:
                raise OfficeNotSupported(f"image {name.rsplit('/', 1)[-1]} has sensitive text in a format that can't be rewritten")
            return body, 0
        return redact_image(body, ext)
    except OfficeNotSupported:
        raise
    except Exception as e:
        raise OfficeNotSupported(f"image {name.rsplit('/', 1)[-1]} could not be read: {e}")


# --- Word ---------------------------------------------------------------------------------

_DOCX_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
)


def _redact_word_xml(data: bytes) -> tuple[bytes, int]:
    tree = _parse(data)
    p_tag, t_tag = f"{{{W}}}p", f"{{{W}}}t"
    count = 0
    for p in tree.iter(p_tag):
        # Only the text of this paragraph, not of paragraphs nested in its text boxes.
        nodes = [t for t in p.iter(t_tag) if next(t.iterancestors(p_tag)) is p]
        if nodes:
            count += redact_runs(nodes)
    # Field codes, e.g. HYPERLINK "mailto:finance@example.com": each on its own.
    for instr in tree.iter(f"{{{W}}}instrText"):
        count += redact_runs([instr])
    return (_dump(tree), count) if count else (data, 0)


def redact_docx(data: bytes) -> tuple[bytes, int]:
    def edit(name: str, body: bytes):
        if _DOCX_TEXT_PARTS.match(name):
            return _redact_word_xml(body)
        if name.startswith("word/_rels/") and name.endswith(".rels"):
            return _redact_rels(body)
        if _MEDIA.match(name):
            return _redact_media(name, body)
        return body, 0

    return _rewrite_zip(data, edit)


# --- Excel --------------------------------------------------------------------------------

# Built-in number formats that show money: 5-8 currency, 37-44 accounting, 4 = #,##0.00.
_MONEY_BUILTIN = {4, 5, 6, 7, 8, 37, 38, 39, 40, 41, 42, 43, 44}
_MONEY_CODE = re.compile(r'₱|\$|PHP|Php|"P"|\[\$|_\(|_-|#,##0\.00', re.IGNORECASE)
_REF = re.compile(
    r"(?:(?:'((?:[^']|'')+)'|([A-Za-z0-9_.]+))!)?"
    r"(\$?)([A-Z]{1,3})(\$?)(\d+)(?::(\$?)([A-Z]{1,3})(\$?)(\d+))?"
)


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _letters(col: int) -> str:
    out = ""
    while col:
        col, r = divmod(col - 1, 26)
        out = chr(65 + r) + out
    return out


def _split(ref: str) -> tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)", ref)
    return _col(m.group(1)), int(m.group(2))


def _percent_styles(styles: bytes | None) -> set[int]:
    """Cell styles that show a percentage (built-in 9/10 or a custom code with %)."""
    if styles is None:
        return set()
    root = _parse(styles).getroot()
    pct = {9, 10}
    for nf in root.iter(f"{{{S}}}numFmt"):
        if "%" in (nf.get("formatCode") or ""):
            pct.add(int(nf.get("numFmtId")))
    xfs = root.find(f"{{{S}}}cellXfs")
    if xfs is None:
        return set()
    return {i for i, xf in enumerate(xfs) if int(xf.get("numFmtId", "0")) in pct}


def _money_styles(styles: bytes | None) -> set[int]:
    if styles is None:
        return set()
    root = _parse(styles).getroot()
    money_ids = set(_MONEY_BUILTIN)
    for nf in root.iter(f"{{{S}}}numFmt"):
        if _MONEY_CODE.search(nf.get("formatCode") or ""):
            money_ids.add(int(nf.get("numFmtId")))
    xfs = root.find(f"{{{S}}}cellXfs")
    if xfs is None:
        return set()
    return {i for i, xf in enumerate(xfs) if int(xf.get("numFmtId", "0")) in money_ids}


def _sheet_names(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> zip path of its XML."""
    wb = _parse(z.read("xl/workbook.xml")).getroot()
    rels = _parse(z.read("xl/_rels/workbook.xml.rels")).getroot()
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = {}
    for sh in wb.iter(f"{{{S}}}sheet"):
        t = target.get(sh.get(f"{{{R}}}id"), "")
        t = t.lstrip("/")
        out[sh.get("name")] = t if t.startswith("xl/") else "xl/" + t
    return out


def _decimal_amount(value: str | None) -> bool:
    """A plain number that reads like money: at least 1, with cents (1250.5, 8333.33).

    Same idea as the bare-decimal text rule; whole numbers (counts, IDs, years) are kept.
    """
    try:
        x = abs(float(value or ""))
    except ValueError:
        return False
    cents = round(x * 100)
    return x >= 1 and abs(x * 100 - cents) < 1e-6 and cents % 100 != 0


def _to_inline(cell: etree._Element, text: str) -> None:
    for child in list(cell):
        cell.remove(child)
    cell.set("t", "inlineStr")
    is_ = etree.SubElement(cell, f"{{{S}}}is")
    t = etree.SubElement(is_, f"{{{S}}}t")
    t.text = text


def redact_xlsx(data: bytes) -> tuple[bytes, int]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if any(n.startswith("xl/pivotCache/") for n in names):
            raise OfficeNotSupported("workbook has pivot tables (they keep a hidden copy of the data)")
        styles = z.read("xl/styles.xml") if "xl/styles.xml" in names else None
        money = _money_styles(styles)
        percent = _percent_styles(styles)
        sheets = _sheet_names(z)
        trees = {path: _parse(z.read(path)) for path in sheets.values() if path in names}
        shared = _parse(z.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in names else None

    count = 0
    path_of = {name: path for name, path in sheets.items()}
    c_tag, f_tag, v_tag, t_tag = (f"{{{S}}}{x}" for x in ("c", "f", "v", "t"))

    # 1. shared strings and inline strings: text patterns
    if shared is not None:
        for si in shared.getroot():
            nodes = list(si.iter(t_tag))
            if nodes:
                count += redact_runs(nodes)

    # 2. money-formatted number cells, then formulas that depend on redacted cells
    shared_text = (
        ["".join(t.text or "" for t in si.iter(t_tag)) for si in shared.getroot()]
        if shared is not None else []
    )
    labels: dict[tuple[str, int, int], str] = {}  # text cells, to find column headers
    plain: dict[tuple[str, int], list] = {}  # plain-number cells per (sheet, column)
    redacted: set[tuple[str, int, int]] = set()
    formulas = []  # (sheet path, cell, formula text, shared index or None)
    shared_master: dict[tuple[str, str], tuple[str, int, int]] = {}  # text, col, row
    for path, tree in trees.items():
        for cell in tree.iter(c_tag):
            ref = cell.get("r")
            if ref is None:
                continue
            col, row = _split(ref)
            is_ = cell.find(f"{{{S}}}is")
            if is_ is not None:
                count += redact_runs(list(is_.iter(t_tag)))
            f = cell.find(f_tag)
            v = cell.find(v_tag)
            if f is not None:
                si = f.get("si") if f.get("t") == "shared" else None
                if si is not None and f.text:
                    shared_master[(path, si)] = (f.text, col, row)
                formulas.append((path, cell, f.text or "", si))
            is_number = cell.get("t") in (None, "n") and v is not None
            if cell.get("t") == "s" and v is not None and (v.text or "").isdigit():
                i = int(v.text)
                if i < len(shared_text):
                    labels[(path, col, row)] = shared_text[i]
            elif is_ is not None:
                labels[(path, col, row)] = "".join(t.text or "" for t in is_.iter(t_tag))
            # With the model, plain numbers are judged per column (step 3); without it, a number
            # with cents counts as money (the regex backup).
            cents_rule = column_judge() is None and _decimal_amount(v.text) if v is not None else False
            if is_number and (int(cell.get("s", "0")) in money or cents_rule):
                redacted.add((path, col, row))
                _to_inline(cell, AMOUNT)
                count += 1
            elif is_number and int(cell.get("s", "0")) not in percent:  # percentages are never money
                plain.setdefault((path, col), []).append((row, cell, v.text))
            elif cell.get("t") == "str" and v is not None:  # formula with a text result
                new, n = redact(v.text or "")
                if n:
                    v.text = new
                    count += n

    # 3. plain-number columns the local model judges to be money (from header + samples)
    judge = column_judge()
    if judge is not None and plain:
        name_of = {v: k for k, v in path_of.items()}
        columns, cells_of = [], {}
        for (path, col), cells in plain.items():
            first = min(r for r, _, _ in cells)
            above = [
                labels[(path, c, r)].strip()
                for r in range(first - 1, max(0, first - 6), -1)
                for c in (col, col - 1, col - 2)  # merged header cells sit left of the column
                if labels.get((path, c, r), "").strip()
                and (c == col or r < first - 1)
            ]
            header = " / ".join(reversed(list(dict.fromkeys(above))[:3]))
            cid = f"{name_of.get(path, path)}!{_letters(col)}"
            columns.append({"id": cid, "header": header[:60], "samples": [x for _, _, x in sorted(cells, key=lambda c: c[0])[:5]]})
            cells_of[cid] = (path, col, cells)
        money_ids: set[str] = set()
        for i in range(0, len(columns), 60):
            money_ids |= judge(columns[i : i + 60])
        for cid in money_ids:
            path, col, cells = cells_of[cid]
            for row, cell, _ in cells:
                redacted.add((path, col, row))
                _to_inline(cell, AMOUNT)
                count += 1

    def refs(path: str, text: str, dc: int = 0, dr: int = 0):
        """Cell ranges a formula refers to; dc/dr shift relative refs (shared formula copies)."""
        for m in _REF.finditer(text):
            sheet = (m.group(1) or "").replace("''", "'") or m.group(2)
            p = path_of.get(sheet, path) if sheet else path
            c1 = _col(m.group(4)) + (0 if m.group(3) else dc)
            r1 = int(m.group(6)) + (0 if m.group(5) else dr)
            if m.group(8):
                c2 = _col(m.group(8)) + (0 if m.group(7) else dc)
                r2 = int(m.group(10)) + (0 if m.group(9) else dr)
            else:
                c2, r2 = c1, r1
            yield p, min(c1, c2), max(c1, c2), min(r1, r2), max(r1, r2)

    def touches(path: str, text: str, dc: int = 0, dr: int = 0) -> bool:
        for p, c1, c2, r1, r2 in refs(path, text, dc, dr):
            if any(rp == p and c1 <= c <= c2 and r1 <= r <= r2 for rp, c, r in redacted):
                return True
        return False

    changed = True
    while changed:  # repeat: a formula on a redacted formula is redacted too
        changed = False
        for path, cell, text, si in formulas:
            if cell.get("t") == "inlineStr":
                continue
            if int(cell.get("s", "0")) in percent:
                continue  # a percentage computed from amounts doesn't reveal them
            col, row = _split(cell.get("r"))
            dc = dr = 0
            src = text
            if si is not None and not text and (path, si) in shared_master:
                src, mc, mr = shared_master[(path, si)]
                dc, dr = col - mc, row - mr
            if src and touches(path, src, dc, dr):
                redacted.add((path, col, row))
                _to_inline(cell, AMOUNT)
                count += 1
                changed = True

    def edit(name: str, body: bytes):
        if name in trees:
            return _dump(trees[name]), 0
        if name == "xl/sharedStrings.xml" and shared is not None:
            return _dump(shared), 0
        if name == "xl/calcChain.xml":
            return None, 0  # lists formula cells; Excel rebuilds it
        if name == "[Content_Types].xml" or name == "xl/_rels/workbook.xml.rels":
            return _drop_calc_chain(body), 0
        if re.match(r"^xl/comments\d*\.xml$", name):
            tree = _parse(body)
            n = 0
            for t in tree.iter(t_tag):
                n += redact_runs([t])
            return (_dump(tree), n) if n else (body, 0)
        if re.match(r"^xl/charts/chart\d+\.xml$", name):
            return _strip_chart_cache(body), 0
        if name.startswith("xl/") and "/_rels/" in name and name.endswith(".rels"):
            return _redact_rels(body)
        if _MEDIA.match(name):
            return _redact_media(name, body)
        return body, 0

    out, n = _rewrite_zip(data, edit, force=count > 0)
    count += n
    return (out, count) if count else (data, 0)


def _drop_calc_chain(body: bytes) -> bytes:
    tree = _parse(body)
    root = tree.getroot()
    for el in list(root):
        if "calcChain" in (el.get("PartName") or "") or "calcChain" in (el.get("Target") or ""):
            root.remove(el)
    return _dump(tree)


def _strip_chart_cache(body: bytes) -> bytes:
    tree = _parse(body)
    for tag in ("numCache", "strCache"):
        for el in list(tree.iter(f"{{{C}}}{tag}")):
            el.getparent().remove(el)
    return _dump(tree)
