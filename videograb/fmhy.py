# -*- coding: utf-8 -*-
"""FMHY 资源目录：联网优先搜索，本地索引兜底。

数据源是 fmhy.net 的整页 Markdown（约 1 MB，一次请求拿全），
解析成「分类 / 二级分类 / 站点」三层结构。

  · 搜索默认先联网取最新目录（10 分钟内复用同一次下载），失败退回本地索引
  · 更新目录：把当前目录落盘成 data/fmhy/index.json，之后没网也能搜
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from .config import DATA_DIR

ROOT = DATA_DIR / "fmhy"
INDEX = ROOT / "index.json"

SOURCES = [
    "https://fmhy.net/single-page.md",
    "https://api.fmhy.net/single-page",
]
UA = "MakiseToolkit/4.0 (local resource directory)"
ONLINE_TTL = 600          # 10 分钟内复用一次联网结果
ONLINE_TIMEOUT = 30

_LOCK = threading.RLock()
_STATE = {"running": False, "done": 0, "total": 0, "error": ""}
_ONLINE = {"at": 0.0, "items": None}


def _ctx():
    # 这台机器的证书链拿不到吊销列表，和 curl 的 --ssl-no-revoke 是一回事
    ctx = ssl.create_default_context()
    try:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    except Exception:
        pass
    return ctx


def _fetch(url, timeout=ONLINE_TIMEOUT):
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/markdown,text/plain,*/*"})
    with urlopen(req, timeout=timeout, context=_ctx()) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig", "replace")


# ---------------------------------------------------------------- 解析
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _clean(text):
    text = re.sub(r"\*\*|__|`", "", text or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    return text.strip(" -·—")


def parse(md):
    """把整页 Markdown 解析成条目列表。"""
    items = []
    seen = set()
    top = ""
    sub = ""
    for line in md.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            top = _clean(stripped.lstrip("#"))
            sub = ""
            continue
        if stripped.startswith("## "):
            sub = _clean(stripped.lstrip("#"))
            continue
        if not stripped.startswith(("*", "-")):
            continue
        links = _LINK.findall(stripped)
        if not links:
            continue
        title, url = links[0]
        title = _clean(title)
        if len(title) < 2:
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        after = stripped.split("](" + url + ")", 1)
        desc = _clean(after[1]) if len(after) > 1 else ""
        desc = re.sub(r"^[\s*]*[-–—:：]+", "", desc)[:180]
        items.append({
            "t": title[:80],
            "u": url,
            "s": (top + " / " + sub).strip(" /")[:80],
            "d": desc,
        })
    return items


# ---------------------------------------------------------------- 联网
def _online_items(force=False):
    with _LOCK:
        age = time.time() - _ONLINE["at"]
        if not force and _ONLINE["items"] and age < ONLINE_TTL:
            return _ONLINE["items"]
    last = None
    for url in SOURCES:
        try:
            md = _fetch(url)
            items = parse(md)
            if items:
                with _LOCK:
                    _ONLINE["at"] = time.time()
                    _ONLINE["items"] = items
                return items
        except Exception as exc:
            last = exc
    raise RuntimeError(str(last)[:120] if last else "取不到线上目录")


# ---------------------------------------------------------------- 本地
def _local_items():
    if not INDEX.exists():
        return []
    try:
        return json.loads(INDEX.read_text(encoding="utf-8")).get("items") or []
    except Exception:
        return []


def status():
    with _LOCK:
        st = dict(_STATE)
        online_age = time.time() - _ONLINE["at"] if _ONLINE["at"] else None
    st["online_cached"] = bool(_ONLINE["items"])
    st["online_age"] = int(online_age) if online_age is not None else None
    st["entries"] = 0
    st["sections"] = 0
    st["updated"] = 0
    st["ready"] = INDEX.exists()
    if st["ready"]:
        try:
            data = json.loads(INDEX.read_text(encoding="utf-8"))
            st["entries"] = len(data.get("items") or [])
            st["updated"] = data.get("updated") or 0
            st["sections"] = data.get("sections") or 0
        except Exception:
            st["ready"] = False
    return st


def _do_refresh():
    try:
        items = _online_items(force=True)
        sections = len({it.get("s") for it in items})
        ROOT.mkdir(parents=True, exist_ok=True)
        payload = {"updated": int(time.time()), "sections": sections, "items": items}
        tmp = INDEX.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(INDEX)
        with _LOCK:
            _STATE.update({"running": False, "done": 1, "error": ""})
    except Exception as exc:
        with _LOCK:
            _STATE.update({"running": False, "error": str(exc)[:200]})


def refresh():
    with _LOCK:
        if _STATE.get("running"):
            return {"ok": True, "started": False}
        _STATE.update({"running": True, "done": 0, "total": 1, "error": ""})
    threading.Thread(target=_do_refresh, daemon=True).start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- 搜索
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text):
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9][a-z0-9._+-]*", text)
    cjk = "".join(_CJK.findall(text))
    grams = [cjk[i:i + 2] for i in range(len(cjk) - 1)] if len(cjk) > 1 else ([cjk] if cjk else [])
    return words + grams


def _score(items, query, limit):
    tokens = _tokens(query)
    if not tokens:
        return {"items": [], "total": 0}
    scored = []
    for it in items:
        title = (it.get("t") or "").lower()
        section = (it.get("s") or "").lower()
        desc = (it.get("d") or "").lower()
        score = 0
        for tok in tokens:
            if tok in title:
                score += 6
            elif tok in section:
                score += 3
            elif tok in desc:
                score += 1
        if score:
            scored.append((score - len(title) * 0.01, it))
    scored.sort(key=lambda row: row[0], reverse=True)
    cap = max(1, min(int(limit or 60), 200))
    return {"items": [row[1] for row in scored[:cap]], "total": len(scored)}


def search(query, limit=60, online=True):
    """联网优先，失败退回本地索引；返回里的 source 标明数据来源。"""
    query = (query or "").strip()
    if not query:
        return {"ok": True, "items": [], "total": 0, "source": "none"}
    if online:
        try:
            items = _online_items()
            result = _score(items, query, limit)
            return {"ok": True, "source": "online", **result}
        except Exception as exc:
            local = _local_items()
            if local:
                result = _score(local, query, limit)
                return {"ok": True, "source": "local",
                        "note": "联网失败，用的是本地目录：" + str(exc)[:60], **result}
            return {"ok": False, "source": "none",
                    "error": "联网搜不了，本地也还没目录。先更新一次目录：" + str(exc)[:80],
                    "items": []}
    local = _local_items()
    if not local:
        return {"ok": False, "source": "none", "error": "还没有本地目录，先点「更新目录」", "items": []}
    return {"ok": True, "source": "local", **_score(local, query, limit)}
