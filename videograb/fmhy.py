# -*- coding: utf-8 -*-
"""FMHY 资源索引：把 FreeMediaHeckYeah 的目录抓下来，存成本地索引，离线搜索。

它本身只是一份"链接目录"，不托管任何文件。这里做的是：
  · 从 fmhy.net 的 sitemap 拿到全部页面
  · 每页解析出「分组 → 标题 → 链接」
  · 存成 data/fmhy/index.json，之后搜索完全离线
点结果用系统浏览器打开，程序不碰第三方内容。
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

from .config import DATA_DIR

ROOT = DATA_DIR / "fmhy"
INDEX = ROOT / "index.json"
SITEMAP = "https://fmhy.net/sitemap.xml"
UA = "MakiseToolkit/4.0 (local resource index)"
MAX_PAGES = 120

_LOCK = threading.RLock()
_STATE = {"running": False, "done": 0, "total": 0, "error": "", "updated": 0}


def _ctx():
    # 这台机器上 schannel 拿不到吊销列表，和 curl 的 --ssl-no-revoke 是一回事
    ctx = ssl.create_default_context()
    try:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    except Exception:
        pass
    return ctx


def _get(url: str, timeout: int = 25) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml"})
    with urlopen(req, timeout=timeout, context=_ctx()) as resp:
        return resp.read()


class _Parser(HTMLParser):
    """只收 <main> 里的内容；标题当分组，<a> 当条目。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_main = False
        self.heading = None
        self.heading_text = []
        self.section = ""
        self.link = None
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.in_main = True
            return
        if not self.in_main:
            return
        if tag in ("h2", "h3"):
            self.heading = tag
            self.heading_text = []
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href.startswith("http"):
                self.link = {"href": href, "text": []}

    def handle_endtag(self, tag):
        if not self.in_main:
            return
        if tag == "main":
            self.in_main = False
        elif tag in ("h2", "h3"):
            text = "".join(self.heading_text).strip()
            if text:
                self.section = text
            self.heading = None
            self.heading_text = []
        elif tag == "a" and self.link:
            title = "".join(self.link["text"]).strip()
            # 标题里的零宽字符、图标字符都清掉
            title = re.sub(r"[\u200b-\u200f\ufeff]", "", title).strip()
            if len(title) > 1:
                self.items.append((self.section, title, self.link["href"]))
            self.link = None

    def handle_data(self, data):
        if not self.in_main:
            return
        if self.link is not None:
            self.link["text"].append(data)
        elif self.heading:
            self.heading_text.append(data)


def _pages() -> list:
    xml = _get(SITEMAP).decode("utf-8", "replace")
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    out = []
    for url in urls:
        if "/assets/" in url or url.endswith((".png", ".svg", ".ico")):
            continue
        out.append(url.strip())
    return out[:MAX_PAGES]


def status() -> dict:
    with _LOCK:
        st = dict(_STATE)
    st['entries'] = 0
    st['pages'] = 0
    st['ready'] = INDEX.exists()
    if st['ready']:
        try:
            data = json.loads(INDEX.read_text(encoding="utf-8"))
            st['entries'] = len(data.get('items') or [])
            st['pages'] = len(data.get('pages') or [])
            st['updated'] = data.get('updated') or 0
        except Exception:
            st['ready'] = False
    return st


def _do_refresh():
    try:
        pages = _pages()
        with _LOCK:
            _STATE.update({"total": len(pages), "done": 0, "error": ""})
        seen = set()
        items = []
        ok_pages = []
        for i, page in enumerate(pages, 1):
            with _LOCK:
                _STATE["done"] = i
            try:
                html = _get(page).decode("utf-8", "replace")
            except Exception:
                continue                     # 单页失败不影响整体
            parser = _Parser()
            try:
                parser.feed(html)
            except Exception:
                continue
            slug = page.rstrip("/").rsplit("/", 1)[-1] or "home"
            for section, title, href in parser.items:
                key = href.rstrip('/').lower()
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "t": title,
                    "u": href,
                    "s": section[:60],
                    "p": slug,
                })
            ok_pages.append(page)
        ROOT.mkdir(parents=True, exist_ok=True)
        payload = {"updated": int(time.time()), "pages": ok_pages, "items": items}
        tmp = INDEX.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(INDEX)
        with _LOCK:
            _STATE.update({"running": False, "error": ""})
    except Exception as exc:
        with _LOCK:
            _STATE.update({"running": False, "error": str(exc)[:200]})


def refresh() -> dict:
    """后台抓取。已经在跑就直接返回当前进度。"""
    with _LOCK:
        if _STATE.get("running"):
            return {"ok": True, "started": False, "status": dict(_STATE)}
        _STATE.update({"running": True, "done": 0, "total": 0, "error": ""})
    threading.Thread(target=_do_refresh, daemon=True).start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- 搜索
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> list:
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9][a-z0-9._+-]*", text)
    cjk = "".join(_CJK.findall(text))
    grams = [cjk[i:i + 2] for i in range(len(cjk) - 1)] if len(cjk) > 1 else ([cjk] if cjk else [])
    return words + grams


def search(query: str, limit: int = 60) -> dict:
    if not INDEX.exists():
        return {"ok": False, "error": "还没有索引，先点一次「更新目录」", "items": []}
    query = (query or '').strip()
    if not query:
        return {"ok": True, "items": [], "total": 0}
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "索引读不出来：" + str(exc)[:80], "items": []}
    items = data.get('items') or []
    tokens = _tokens(query)
    scored = []
    for it in items:
        title = (it.get('t') or '')
        section = (it.get('s') or '')
        page = (it.get('p') or '')
        hay = (title + ' ' + section + ' ' + page).lower()
        score = 0
        for tok in tokens:
            if tok in title.lower():
                score += 6
            elif tok in hay:
                score += 2
        if score:
            # 标题越短，命中越精确，排前面
            scored.append((score - len(title) * 0.01, it))
    scored.sort(key=lambda row: row[0], reverse=True)
    top = [row[1] for row in scored[:max(1, min(int(limit or 60), 200))]]
    return {"ok": True, "items": top, "total": len(scored)}
