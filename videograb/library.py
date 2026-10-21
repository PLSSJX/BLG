# -*- coding: utf-8 -*-
"""文献库：下载过的文献按 DOI 去重存成条目，能生成中英文引用格式。

和 papers.py 分开是因为这两件事节奏不同：papers.py 负责"找"和"下"，
这里负责"存"和"引用"——找的源会被封、会变，库的格式要稳定。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

_CJK = re.compile("[\u4e00-\u9fff]")


def library_path() -> Path:
    from .config import DATA_DIR
    return DATA_DIR / "papers" / "library.json"


def _norm_doi(value) -> str:
    text = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def load() -> dict:
    path = library_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def save(data: dict) -> None:
    path = library_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def add(item: dict) -> dict:
    """入库。有 DOI 就按 DOI 去重，没有就按"标题 + 年份"。"""
    data = load()
    items = data.setdefault("items", [])
    doi = _norm_doi(item.get("doi"))
    title = str(item.get("title") or "").strip()
    year = str(item.get("year") or "").strip()
    for exist in items:
        same_doi = doi and _norm_doi(exist.get("doi")) == doi
        same_title = (not doi and title and exist.get("title") == title
                      and str(exist.get("year") or "") == year)
        if same_doi or same_title:
            exist.update({k: v for k, v in item.items() if v})
            save(data)
            return exist
    record = dict(item)
    record["doi"] = doi
    record["doi_display"] = str(item.get("doi_display") or item.get("doi") or "").strip()
    seed = doi or (title + year) or str(time.time())
    record.setdefault("id", hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12])
    record.setdefault("added_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    items.insert(0, record)
    save(data)
    return record


def remove(entry_id: str) -> bool:
    data = load()
    items = data.get("items") or []
    left = [x for x in items if x.get("id") != entry_id]
    if len(left) == len(items):
        return False
    data["items"] = left
    save(data)
    return True


def find(entry_id: str):
    for item in load().get("items") or []:
        if item.get("id") == entry_id:
            return item
    return None


def is_chinese(entry: dict) -> bool:
    text = str(entry.get("title") or "") + str(entry.get("authors") or entry.get("author") or "")
    return bool(_CJK.search(text))


def _split_authors(raw) -> list:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw or "")
    if not text.strip():
        return []
    parts = re.split(";|；|,|，|\band\b|&", text)
    return [p.strip() for p in parts if p.strip()]


def authors_text(entry: dict, style: str = "gbt7714") -> str:
    names = _split_authors(entry.get("authors") or entry.get("author"))
    if not names:
        return ""
    if is_chinese(entry):
        if len(names) > 3:
            return "，".join(names[:3]) + "，等"
        return "，".join(names)
    if style in ("apa", "mla"):
        if len(names) > 7:
            return ", ".join(names[:6]) + ", et al."
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + ", & " + names[-1]
    if len(names) > 3:
        return ", ".join(names[:3]) + ", et al."
    return ", ".join(names)


STYLES = {
    "gbt7714": "GB/T 7714（中文论文常用）",
    "apa": "APA（第 7 版）",
    "ieee": "IEEE",
    "mla": "MLA（第 9 版）",
}


def _display_doi(entry: dict) -> str:
    """DOI 比对要归一化，显示要保留原样（大小写是出版社的写法）。"""
    raw = str(entry.get("doi_display") or entry.get("doi") or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    return raw.strip()


def _dot(text: str) -> str:
    """补句点，但已经有了就不重复（"et al." 后面不能再加一个）。"""
    text = (text or "").strip()
    if not text:
        return ""
    return text if text.endswith((".", "。", "!", "?", "！", "？")) else text + "."


def citation(entry: dict, style: str = "gbt7714") -> str:
    """生成一条可直接粘进参考文献的字符串。

    中英文都要能用：中文条目用全角标点、"等"、第 X 卷/第 X 期；
    英文条目用半角、"et al."、vol./no./pp.。
    """
    style = (style or "gbt7714").lower()
    title = str(entry.get("title") or "").strip().rstrip(".")
    journal = str(entry.get("venue") or entry.get("journal") or "").strip()
    year = str(entry.get("year") or "").strip()
    volume = str(entry.get("volume") or "").strip()
    issue = str(entry.get("issue") or "").strip()
    pages = str(entry.get("pages") or "").strip()
    doi = _display_doi(entry)
    who = authors_text(entry, style)
    chinese = is_chinese(entry)

    if style == "gbt7714":
        sep = "，" if chinese else ", "
        head = (_dot(who) + " ") if who else ""
        kind = "[J]." if journal else "[EB/OL]."
        tail = ""
        if journal:
            tail = journal + (sep + year if year else "")
            if volume:
                tail += sep + volume
            if issue:
                tail += "(" + issue + ")"
            if pages:
                tail += ": " + pages
            tail += "."
        elif year:
            tail = year + "."
        link = (" DOI: " + doi + ".") if doi else ""
        return (head + title + kind + " " + tail + link).strip()

    if style == "apa":
        head = (_dot(who) + " ") if who else ""
        stamp = "(" + year + "). " if year else "(n.d.). "
        tail = ""
        if journal:
            tail = journal
            if volume:
                tail += ", " + volume
            if issue:
                tail += "(" + issue + ")"
            if pages:
                tail += ", " + pages
            tail += "."
        elif pages or volume:
            tail = ".".join(x for x in [volume, pages] if x) + "."
        link = (" https://doi.org/" + doi) if doi else ""
        return (head + stamp + _dot(title) + " " + tail + link).strip()

    if style == "mla":
        head = (_dot(who) + " ") if who else ""
        body = ("\"" + _dot(title) + "\" ") if title else ""
        if chinese:
            rest = journal
            if volume:
                rest += "，第 " + volume + " 卷"
            if issue:
                rest += "，第 " + issue + " 期"
            if year:
                rest += "，" + year + " 年"
            if pages:
                rest += "，第 " + pages + " 页"
            return (head + body + rest + ".").strip()
        rest = journal
        if volume:
            rest += ", vol. " + volume
        if year:
            rest += ", " + year
        if pages:
            rest += ", pp. " + pages
        link = (", doi:" + doi) if doi else ""
        return (head + body + rest + link + ".").strip()

    # IEEE
    head = (who + ", ") if who else ""
    body = ("\"" + title + ",\" ") if title else ""
    if chinese:
        rest = journal
        if volume:
            rest += "，第 " + volume + " 卷"
        if issue:
            rest += "，第 " + issue + " 期"
        if pages:
            rest += "，第 " + pages + " 页"
        if year:
            rest += "，" + year
        return (head + body + rest + ".").strip()
    rest = journal
    if volume:
        rest += ", vol. " + volume
    if issue:
        rest += ", no. " + issue
    if pages:
        rest += ", pp. " + pages
    if year:
        rest += ", " + year
    link = (", doi: " + doi) if doi else ""
    return (head + body + rest + link + ".").strip()


# ---------------------------------------------------------------- 导出
# BibTeX / RIS 是参考文献管理器的通用交换格式：Zotero、EndNote、JabRef 都能吃。
# 这两段纯字符串拼接，不引第三方库。

def _bib_escape(text) -> str:
    out = str(text or "")
    for a, b in (("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"), ("_", r"\_")):
        out = out.replace(a, b)
    return out


def _split_pages(pages) -> tuple:
    text = str(pages or "").strip().replace("–", "-")
    if not text:
        return "", ""
    if "-" in text:
        a, _, b = text.partition("-")
        return a.strip(), b.strip()
    return text, ""


def _bib_authors(entry: dict) -> str:
    names = _split_authors(entry.get("authors") or entry.get("author"))
    if not names:
        return ""
    # 不颠倒姓名顺序：中文拼音（Zhang San）和西文（San Zhang）从字符串上分不清，
    # 硬转只会把中文作者搞成 "San, Zhang"。原样交给 Zotero/JabRef 更安全。
    return " and ".join(names)


def to_bibtex(entries: list) -> str:
    """导出 .bib。key 用 作者姓+年份+标题首词，重复时自动加后缀。"""
    blocks = []
    used = set()
    for entry in entries:
        names = _split_authors(entry.get("authors") or entry.get("author"))
        first = (names[0].split()[-1] if names else "anon")
        year = str(entry.get("year") or "nd")
        word = "".join(c for c in str(entry.get("title") or "").split(" ")[0] if c.isalnum()) or "paper"
        key = (first + year + word)[:40]
        base = key
        n = 1
        while key in used:
            n += 1
            key = base + chr(96 + n)
        used.add(key)

        fields = [
            ("author", _bib_authors(entry)),
            ("title", _bib_escape(entry.get("title"))),
            ("journal", _bib_escape(entry.get("venue") or entry.get("journal"))),
            ("year", year if year != "nd" else ""),
            ("volume", entry.get("volume")),
            ("number", entry.get("issue")),
            ("pages", str(entry.get("pages") or "").replace("–", "--")),
            ("doi", _display_doi(entry)),
            ("url", entry.get("url")),
        ]
        lines = ["  " + name + " = {" + str(value) + "},"
                 for name, value in fields if str(value or "").strip()]
        kind = "article" if (entry.get("venue") or entry.get("journal")) else "misc"
        blocks.append("@" + kind + "{" + key + ",\n" + "\n".join(lines) + "\n}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_ris(entries: list) -> str:
    """导出 .ris。"""
    blocks = []
    for entry in entries:
        lines = ["TY  - " + ("JOUR" if (entry.get("venue") or entry.get("journal")) else "GEN")]
        for name in _split_authors(entry.get("authors") or entry.get("author")):
            lines.append("AU  - " + name)
        if entry.get("title"):
            lines.append("TI  - " + str(entry.get("title")))
        if entry.get("venue") or entry.get("journal"):
            lines.append("JO  - " + str(entry.get("venue") or entry.get("journal")))
        if entry.get("year"):
            lines.append("PY  - " + str(entry.get("year")))
        if entry.get("volume"):
            lines.append("VL  - " + str(entry.get("volume")))
        if entry.get("issue"):
            lines.append("IS  - " + str(entry.get("issue")))
        if entry.get("pages"):
            start, end = _split_pages(entry.get("pages"))
            lines.append("SP  - " + start)
            if end:
                lines.append("EP  - " + end)
        doi = _display_doi(entry)
        if doi:
            lines.append("DO  - " + doi)
        if entry.get("url"):
            lines.append("UR  - " + str(entry.get("url")))
        lines.append("ER  - ")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
