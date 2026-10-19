# -*- coding: utf-8 -*-
"""文档转换：PDF / Word(.docx) / Excel(.xlsx) / Markdown 互转。

只用标准库（zipfile + xml + zlib）。docx 和 xlsx 本质都是 zip 包里的 XML，
读写都可靠；PDF 只能做**文本抽取**，没有版式还原，扫描件和复杂中文字体
抽不出来很正常 —— 这是格式本身的限制，不是实现偷懒。
"""
from __future__ import annotations

import os
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

SUPPORTED = {
    "docx": "Word 文档",
    "xlsx": "Excel 表格",
    "md": "Markdown",
    "pdf": "PDF（仅文本，无版式）",
}


class ConvertError(Exception):
    pass


# ---------------------------------------------------------------- Word 读

def read_docx(path) -> list:
    """返回块列表：[("p", 文本), ("table", [[单元格…]…])]。"""
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise ConvertError("读不了这个 .docx：" + str(exc)[:100]) from None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ConvertError("docx 里的 XML 坏了：" + str(exc)[:100]) from None

    blocks: list = []
    body = root.find(W + "body")
    if body is None:
        return blocks
    for node in body:
        tag = node.tag
        if tag == W + "p":
            text = "".join(t.text or "" for t in node.iter(W + "t"))
            blocks.append(("p", text))
        elif tag == W + "tbl":
            rows = []
            for tr in node.iter(W + "tr"):
                cells = []
                for tc in tr.iter(W + "tc"):
                    cells.append("".join(t.text or "" for t in tc.iter(W + "t")).strip())
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append(("table", rows))
    return blocks


# ---------------------------------------------------------------- Word 写

def write_docx(blocks, path) -> None:
    paras, tables = [], []
    for kind, value in blocks:
        if kind == "p":
            paras.append(value)
        elif kind == "table":
            tables.append((len(tables), value))
    body_parts = []
    for kind, value in blocks:
        if kind == "p":
            body_parts.append(
                '<w:p><w:r><w:t xml:space="preserve">' + escape(str(value)) + "</w:t></w:r></w:p>")
        elif kind == "table":
            rows_xml = []
            for row in value:
                cells_xml = "".join(
                    '<w:tc><w:p><w:r><w:t xml:space="preserve">' + escape(str(c)) + "</w:t></w:r></w:p></w:tc>"
                    for c in row)
                rows_xml.append("<w:tr>" + cells_xml + "</w:tr>")
            body_parts.append(
                '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>' + "".join(rows_xml) + "</w:tbl>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body_parts) + "</w:body></w:document>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>")
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>")
        zf.writestr("word/document.xml", document)


# ---------------------------------------------------------------- Excel 读

def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    value = 0
    for ch in letters.group(1):
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def read_xlsx(path) -> dict:
    """返回 {工作表名: [[单元格…]…]}。"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter(S + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(S + "t")))
            sheets = {}
            sheet_files = sorted(n for n in names
                                 if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
            titles = _sheet_titles(zf, names)
            for idx, member in enumerate(sheet_files):
                root = ET.fromstring(zf.read(member))
                rows = []
                for row in root.iter(S + "row"):
                    cells: dict = {}
                    for c in row.iter(S + "c"):
                        ref = c.get("r") or ""
                        kind = c.get("t")
                        value = ""
                        if kind == "inlineStr":
                            value = "".join(t.text or "" for t in c.iter(S + "t"))
                        else:
                            v = c.find(S + "v")
                            if v is not None and v.text is not None:
                                if kind == "s":
                                    try:
                                        value = shared[int(v.text)]
                                    except (ValueError, IndexError):
                                        value = v.text
                                else:
                                    value = v.text
                        if value != "":
                            cells[_col_index(ref)] = value
                    if cells:
                        width = max(cells) + 1
                        rows.append([cells.get(i, "") for i in range(width)])
                title = titles[idx] if idx < len(titles) else "Sheet" + str(idx + 1)
                sheets[title] = rows
            return sheets
    except (KeyError, zipfile.BadZipFile, OSError, ET.ParseError) as exc:
        raise ConvertError("读不了这个 .xlsx：" + str(exc)[:100]) from None


def _sheet_titles(zf, names) -> list:
    if "xl/workbook.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/workbook.xml"))
    except ET.ParseError:
        return []
    return [sh.get("name") or "" for sh in root.iter(S + "sheet")]


# ---------------------------------------------------------------- Excel 写

def _col_letter(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def write_xlsx(sheets: dict, path) -> None:
    def cell_xml(r, c, value):
        ref = _col_letter(c) + str(r + 1)
        text = "" if value is None else str(value)
        if isinstance(value, (int, float)):
            return '<c r="' + ref + '"><v>' + text + "</v></c>"
        return ('<c r="' + ref + '" t="inlineStr"><is><t xml:space="preserve">'
                + escape(text) + "</t></is></c>")

    sheet_xmls = []
    for title, rows in sheets.items():
        body = []
        for r, row in enumerate(rows):
            cells = "".join(cell_xml(r, c, v) for c, v in enumerate(row) if v != "" and v is not None)
            if cells:
                body.append('<row r="' + str(r + 1) + '">' + cells + "</row>")
        sheet_xmls.append((
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>" + "".join(body) + "</sheetData></worksheet>"))

    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet' + str(i + 1) + '.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(sheet_xmls)))
    sheet_entries = "".join(
        '<sheet name="' + escape(str(t)[:31] or ("Sheet" + str(i + 1))) + '" sheetId="'
        + str(i + 1) + '" r:id="rId' + str(i + 1) + '"/>'
        for i, t in enumerate(sheets))
    rels = "".join(
        '<Relationship Id="rId' + str(i + 1) + '" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet' + str(i + 1) + '.xml"/>'
        for i in range(len(sheet_xmls)))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + overrides + "</Types>")
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>")
        zf.writestr("xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>" + sheet_entries + "</sheets></workbook>")
        zf.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + rels + "</Relationships>")
        for i, xml in enumerate(sheet_xmls):
            zf.writestr("xl/worksheets/sheet" + str(i + 1) + ".xml", xml)


# ---------------------------------------------------------------- PDF 文本抽取

def read_pdf_text(path) -> list:
    """按页抽文本。只处理未加密、文本型 PDF；扫描件没有文字层，抽不出来。"""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ConvertError("读不了这个 PDF：" + str(exc)[:100]) from None
    if not raw.startswith(b"%PDF"):
        raise ConvertError("这不是一个 PDF 文件")

    pages: list = []
    for match in re.finditer(rb"stream\r?\n", raw):
        start = match.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        chunk = raw[start:end]
        data = None
        for attempt in (lambda: zlib.decompress(chunk),
                        lambda: zlib.decompressobj().decompress(chunk)):
            try:
                data = attempt()
                break
            except zlib.error:
                continue
        if data is None:
            # 未压缩的流是合法的 PDF 写法，直接按原文处理
            data = chunk
        text = _pdf_text_from_stream(data)
        if text.strip():
            pages.append(text)
    if not pages:
        raise ConvertError(
            "这个 PDF 里没有可抽取的文字层。"
            "扫描件、图片型 PDF，或用 CID 子集字体嵌入的中文 PDF 都属于这种情况。")
    return pages


def _pdf_text_from_stream(data: bytes) -> str:
    out = []
    # (…)、<…> 两种字符串 + Tj / TJ 两种绘制指令
    for token in re.finditer(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>|\bTJ\b|\bTj\b|\bTd\b|\bT\*\b|\bET\b", data):
        piece = token.group(0)
        if piece in (b"Td", b"T*", b"ET"):
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            continue
        if piece in (b"TJ", b"Tj"):
            continue
        if piece.startswith(b"("):
            out.append(_pdf_unescape(piece[1:-1]))
        elif piece.startswith(b"<"):
            hexpart = re.sub(rb"\s", b"", piece[1:-1])
            try:
                decoded = bytes.fromhex(hexpart.decode("ascii"))
            except ValueError:
                continue
            out.append(decoded.decode("utf-16-be", "replace") if len(decoded) % 2 == 0
                       else decoded.decode("latin-1", "replace"))
    return "".join(out)


def _pdf_unescape(body: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == 0x5C and i + 1 < len(body):
            nxt = body[i + 1]
            mapping = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                       0x28: 40, 0x29: 41, 0x5C: 92}
            if nxt in mapping:
                out.append(mapping[nxt]); i += 2; continue
            if 0x30 <= nxt <= 0x37:
                oct_digits = body[i + 1:i + 4]
                try:
                    out.append(int(oct_digits, 8) & 0xFF); i += 1 + len(oct_digits); continue
                except ValueError:
                    pass
            out.append(nxt); i += 2; continue
        out.append(ch); i += 1
    return out.decode("utf-8", "replace")


# ---------------------------------------------------------------- Markdown

def to_markdown(kind: str, data) -> str:
    lines: list = []
    if kind == "docx":
        for btype, value in data:
            if btype == "p":
                text = value.strip()
                if text:
                    lines.append(text)
                    lines.append("")
            else:
                if value:
                    lines.append("| " + " | ".join(value[0]) + " |")
                    lines.append("|" + "|".join([" --- "] * len(value[0])) + "|")
                    for row in value[1:]:
                        row = list(row) + [""] * (len(value[0]) - len(row))
                        lines.append("| " + " | ".join(row) + " |")
                    lines.append("")
    elif kind == "xlsx":
        for name, rows in data.items():
            lines.append("## " + name)
            lines.append("")
            if rows:
                lines.append("| " + " | ".join(str(c) for c in rows[0]) + " |")
                lines.append("|" + "|".join([" --- "] * len(rows[0])) + "|")
                for row in rows[1:]:
                    lines.append("| " + " | ".join(str(c) for c in row) + " |")
                lines.append("")
    elif kind == "pdf":
        for i, page in enumerate(data):
            lines.append("## 第 " + str(i + 1) + " 页")
            lines.append("")
            lines.append(page.strip())
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def from_markdown(text: str) -> list:
    """把 Markdown 拆成 docx 块；表格转成真表格。"""
    blocks: list = []
    table: list = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c):
                continue                       # 分隔行
            table.append(cells)
            continue
        if table:
            blocks.append(("table", table)); table = []
        if stripped:
            blocks.append(("p", re.sub(r"^#{1,6}\s*", "", stripped)))
    if table:
        blocks.append(("table", table))
    return blocks


# ---------------------------------------------------------------- 调度

READERS = {"docx": read_docx, "xlsx": read_xlsx, "pdf": read_pdf_text}


def convert(src_path, out_dir, target: str) -> dict:
    """把 src_path 转成 target 格式，结果写进 out_dir，返回产物信息。"""
    src = Path(src_path)
    if not src.is_file():
        raise ConvertError("文件不存在：" + str(src))
    source = src.suffix.lower().lstrip(".")
    # md 是纯文本，直接读；其余走各自解析器
    if source != "md" and source not in READERS:
        raise ConvertError("不支持读取 ." + source + "，目前支持：docx、xlsx、pdf、md")
    if target not in ("docx", "xlsx", "md", "pdf"):
        raise ConvertError("不支持输出 ." + target + "，可选：docx、xlsx、md、pdf")
    if source == target:
        raise ConvertError("源文件和目标格式一样了")
    if target == "pdf":
        # PDF 只能由 Office 生成，源必须是 docx / xlsx
        if source not in ("docx", "xlsx"):
            raise ConvertError("只有 .docx 或 .xlsx 能转成 PDF；PDF 本身不能再转 PDF")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (src.stem + ".pdf")
        index = 2
        while out_path.exists():
            out_path = out_dir / (src.stem + " (" + str(index) + ").pdf")
            index += 1
        # 优先用 Office（版式最准），没有或失败就退回"文档→HTML→无头浏览器"
        engine = ""
        try:
            office_to_pdf(src, out_path)
            engine = "office"
        except ConvertError as office_error:
            try:
                doc_to_html_pdf(src, out_path)
                engine = "browser"
            except ConvertError:
                # 两条都不通时，把 Office 的原因抛出去（信息更具体）
                raise office_error from None
        return {"ok": True, "source": source, "target": "pdf", "engine": engine,
                "output": str(out_path), "size": out_path.stat().st_size}

    data = (Path(src).read_text(encoding="utf-8", errors="replace")
            if source == "md" else READERS[source](src))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (src.stem + "." + target)
    index = 2
    while out_path.exists():
        out_path = out_dir / (src.stem + " (" + str(index) + ")." + target)
        index += 1

    if target == "md":
        out_path.write_text(to_markdown(source, data), encoding="utf-8")
    else:
        if source == "md":
            blocks = from_markdown(data)
        elif source == "docx":
            blocks = data
        elif source == "xlsx":
            blocks = []
            for name, rows in data.items():
                blocks.append(("p", name))
                if rows:
                    blocks.append(("table", rows))
        else:                                   # pdf
            blocks = [("p", page.strip()) for page in data]
        if target == "docx":
            write_docx(blocks, out_path)
        else:
            sheets = {}
            if source == "xlsx":
                sheets = {name: rows for name, rows in data.items()}
            else:
                rows: list = []
                for btype, value in blocks:
                    if btype == "p":
                        if value.strip():
                            rows.append([value.strip()])
                    else:
                        rows.extend(value)
                sheets = {(src.stem or "Sheet1")[:31]: rows or [["(空)"]]}
            write_xlsx(sheets, out_path)
    return {"ok": True, "source": source, "target": target,
            "output": str(out_path), "size": out_path.stat().st_size}


# ---------------------------------------------------------------- 转 PDF（走 Office COM）

# 纯标准库写不出带中文的 PDF：base-14 字体只有 Latin-1，嵌中文字体要自己
# 解析 TTF/TTC 并做 CID 子集，那是另一个量级的工程。既然目标用户是 Windows
# 桌面，直接用已装的 Office 转最靠谱 —— 版式、字体、分页都由 Word/Excel 自己保证。
_PS_TEMPLATE = r'''
$ErrorActionPreference = 'Stop'
$src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{src}'))
$dst = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{dst}'))
$app = New-Object -ComObject {progid}
try {
    $app.Visible = $false
    $app.DisplayAlerts = 0
    if ('{progid}' -eq 'Word.Application') {
        $doc = $app.Documents.Open($src, $false, $true)
        try { $doc.SaveAs([ref]$dst, [ref]17) } finally { $doc.Close(0) }
    } else {
        $book = $app.Workbooks.Open($src, 0, $true)
        try { $book.ExportAsFixedFormat(0, $dst) } finally { $book.Close($false) }
    }
    'OK'
} finally {
    $app.Quit()
}
'''


def _office_progid(ext: str) -> str:
    return {"docx": "Word.Application", "xlsx": "Excel.Application"}.get(ext, "")


def office_to_pdf(src_path, out_path) -> None:
    """用本机已安装的 Word / Excel 把文档另存为 PDF。"""
    if os.name != "nt":
        raise ConvertError("转 PDF 依赖本机的 Word / Excel，只能在 Windows 上用")
    src = Path(src_path)
    progid = _office_progid(src.suffix.lower().lstrip("."))
    if not progid:
        raise ConvertError("只有 .docx 和 .xlsx 能转 PDF")
    import base64
    import subprocess
    import tempfile
    script = (_PS_TEMPLATE
              .replace("{src}", base64.b64encode(str(src).encode("utf-8")).decode())
              .replace("{dst}", base64.b64encode(str(out_path).encode("utf-8")).decode())
              .replace("{progid}", progid))
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        script_path = fh.name
    # 必须用绝对路径：实测环境 PATH 里没有 WindowsPowerShell 目录，
    # 按名字调 powershell 会直接失败（进程起不来）。
    from .localfile import powershell_exe
    # 关键：**不要** capture_output。把 stdout/stderr 接成管道时，Word COM
    # 启动会卡死（实测挂到 500 秒超时）；重定向到 DEVNULL 就正常，10 秒内出结果。
    # 成功与否靠产物文件判断，不靠退出码。
    try:
        subprocess.run(
            [powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", script_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConvertError("调用 Office 失败：" + str(exc)[:120]) from None
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
    if not Path(out_path).exists() or Path(out_path).stat().st_size < 512:
        hint = "调用 Office 失败" if progid == "Word.Application" else "调用 Excel 失败"
        raise ConvertError(
            "转 PDF 失败（" + hint + "）。常见原因：没装 Microsoft Office、"
            "Office 正弹窗等待操作，或被杀毒软件拦截了 COM 自动化。")


# ---------------------------------------------------------------- 转 PDF（不依赖 Office 的备用通路）

def _html_for_blocks(blocks, title: str) -> str:
    """把文档块渲染成一份 A4 排版的 HTML。"""
    parts = []
    for kind, value in blocks:
        if kind == "p":
            text = str(value).strip()
            if text:
                parts.append("<p>" + escape(text) + "</p>")
        elif kind == "table":
            rows = value or []
            if not rows:
                continue
            head = "".join("<th>" + escape(str(c)) + "</th>" for c in rows[0])
            body = "".join(
                "<tr>" + "".join("<td>" + escape(str(c)) + "</td>" for c in row) + "</tr>"
                for row in rows[1:])
            parts.append("<table><thead><tr>" + head + "</tr></thead><tbody>"
                         + body + "</tbody></table>")
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>" + escape(title) + "</title><style>"
        "@page{size:A4;margin:18mm 16mm}"
        'body{font-family:"Microsoft YaHei","SimSun","Segoe UI",sans-serif;'
        "font-size:11pt;line-height:1.7;color:#111}"
        "p{margin:0 0 .6em}"
        "table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:10pt}"
        "th,td{border:1px solid #999;padding:4px 6px;text-align:left}"
        "th{background:#eee}"
        "</style></head><body>" + "".join(parts) + "</body></html>")


def _browser_candidates() -> list:
    """能用 --print-to-pdf 的浏览器。Edge 在 Win10/11 上是系统自带的。"""
    names = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        names.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        names.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    names.append(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    names.append(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    return [n for n in names if os.path.isfile(n)]


def _html_to_pdf_once(html: str, out_path) -> bool:
    """用无头浏览器把 HTML 打成 PDF。成功返回 True。"""
    import subprocess
    import tempfile
    browsers = _browser_candidates()
    if not browsers:
        return False
    with tempfile.TemporaryDirectory(prefix="vgpdf-") as tmp:
        html_path = os.path.join(tmp, "doc.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        profile = os.path.join(tmp, "profile")
        for exe in browsers:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
                   "--no-default-browser-check", "--disable-extensions",
                   "--user-data-dir=" + profile,
                   "--print-to-pdf-no-header",
                   "--print-to-pdf=" + str(out_path),
                   "file:///" + html_path.replace("\\", "/")]
            try:
                subprocess.run(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=120)
            except (OSError, subprocess.SubprocessError):
                continue
            target = Path(out_path)
            if target.is_file() and target.stat().st_size > 512:
                try:
                    if target.read_bytes()[:5] == b"%PDF-":
                        return True
                except OSError:
                    pass
    return False


def doc_to_html_pdf(src_path, out_path) -> None:
    """不依赖 Office 的转 PDF：文档 → HTML → 无头浏览器打印。"""
    source = Path(src_path).suffix.lower().lstrip(".")
    if source == "docx":
        blocks = read_docx(src_path)
    elif source == "xlsx":
        blocks = []
        for name, rows in read_xlsx(src_path).items():
            blocks.append(("p", name))
            if rows:
                blocks.append(("table", rows))
    else:
        raise ConvertError("只有 .docx 或 .xlsx 能转 PDF")
    html = _html_for_blocks(blocks, Path(src_path).stem)
    if not _html_to_pdf_once(html, out_path):
        raise ConvertError(
            "转 PDF 失败：既没能调用 Office，也没找到可用的 Edge / Chrome。"
            "装一个 Microsoft Edge 或 Google Chrome 就能走备用通路。")
