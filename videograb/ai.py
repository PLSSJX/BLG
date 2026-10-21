# -*- coding: utf-8 -*-
"""本地检索 + 用户自带模型的问答。

设计上刻意分成两半：

  · **检索完全在本地**：把项目节点正文、文献库里的 OCR 文本切成块存进 SQLite，
    用关键词打分排序。不联网、不上传、断网也能用。
  · **生成才调模型**：只有当用户主动点「问 AI」时，才把**检索到的片段**
    发到用户自己填的 OpenAI 兼容端点。没填 key 就只用检索，不报错。

不用向量嵌入，是因为把 embedding 模型打进安装包要加 2 GB 的 torch，
和"一个 20 MB 的桌面小工具"这个前提冲突。关键词检索对论文场景够用：
写论文时想找的通常是"我哪一节写过这个词"。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DATA_DIR, load_settings

DB_PATH = DATA_DIR / "ai" / "index.db"
CHUNK = 700
OVERLAP = 120
MAX_CONTEXT_CHARS = 6000

_CJK = re.compile("[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+\-.]*")


class AIError(RuntimeError):
    pass


# ---------------------------------------------------------------- 配置
def config() -> dict:
    st = load_settings()
    return {
        "base_url": (getattr(st, "ai_base_url", "") or "").rstrip("/"),
        "api_key": getattr(st, "ai_api_key", "") or "",
        "model": getattr(st, "ai_model", "") or "",
    }


def ready() -> bool:
    cfg = config()
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


# ---------------------------------------------------------------- 分词/打分
def terms(query: str) -> list:
    """把查询拆成"能匹配中文"的词。

    中文没有空格，整串当一个词是匹配不上的，所以取相邻两字（bigram）；
    英文按单词切。这样"低功耗时钟"能匹配到"……低功耗的时钟门控……"。
    """
    text = (query or "").strip()
    out = []
    for run in re.findall("[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    out.extend(w.lower() for w in _ASCII_WORD.findall(text))
    seen = set()
    keep = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        keep.append(t)
    return keep[:24]


def _score(text: str, words: list) -> int:
    low = text.lower()
    return sum(low.count(w) for w in words)


# ---------------------------------------------------------------- 索引
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS chunks ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 "project TEXT, node TEXT, path TEXT, text TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project)")
    return conn


def _chunks(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    out = []
    start = 0
    while start < len(text):
        piece = text[start:start + CHUNK]
        out.append(piece)
        if start + CHUNK >= len(text):
            break
        start += CHUNK - OVERLAP
    return out


def _project_texts() -> list:
    """遍历所有项目：节点正文 + 文献库里的 OCR 文本。"""
    from . import projects_api as P
    items = []
    try:
        registry = P.load_registry()
    except Exception:
        return items
    for item in registry:
        try:
            base = P.project_dir(item)
            graph = P.locked_read_graph(base)
        except Exception:
            continue
        name = item.get("name") or "未命名项目"
        nodes = list(graph.get("nodes") or [])
        trunk = graph.get("trunk")
        if trunk:
            nodes.append(trunk)
        for node in nodes:
            rel = node.get("file") or ""
            if not rel:
                continue
            path = base / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            items.append({"project": name, "node": node.get("title") or rel,
                          "path": str(path), "text": text})
            ocr_file = Path(str(path) + ".ocr.txt")
            if ocr_file.is_file():
                try:
                    ocr_text = ocr_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    ocr_text = ""
                if ocr_text.strip():
                    items.append({"project": name, "node": (node.get("title") or rel) + "（OCR）",
                                  "path": str(ocr_file), "text": ocr_text})
    # 文献库：PDF 的 OCR 文本也算资料
    try:
        from . import library as lib
        for entry in lib.load().get("items") or []:
            file_path = entry.get("file") or ""
            if not file_path:
                continue
            ocr_file = Path(file_path + ".ocr.txt")
            if not ocr_file.is_file():
                continue
            try:
                text = ocr_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if text.strip():
                items.append({"project": "文献库", "node": entry.get("title") or "",
                              "path": str(ocr_file), "text": text})
    except Exception:
        pass
    return items


def rebuild(progress=None) -> dict:
    """重建索引。整库重建，几百个文件也就一两秒。"""
    sources = _project_texts()
    conn = _db()
    try:
        conn.execute("DELETE FROM chunks")
        total = 0
        for index, item in enumerate(sources):
            for piece in _chunks(item["text"]):
                conn.execute("INSERT INTO chunks(project, node, path, text) VALUES (?,?,?,?)",
                             (item["project"], item["node"], item["path"], piece))
                total += 1
            if progress and index % 20 == 0:
                progress(min(0.95, (index + 1) / max(1, len(sources))), "已索引 %d/%d" % (index + 1, len(sources)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "files": len(sources), "chunks": total, "db": str(DB_PATH)}


def stats() -> dict:
    if not DB_PATH.exists():
        return {"chunks": 0, "files": 0, "db": str(DB_PATH)}
    conn = _db()
    try:
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = conn.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
    finally:
        conn.close()
    return {"chunks": chunks, "files": files, "db": str(DB_PATH)}


# ---------------------------------------------------------------- 检索
def search(query: str, limit: int = 6, project: str = "") -> list:
    """本地关键词检索。返回 [{project, node, path, text, score}]。"""
    words = terms(query)
    if not words:
        return []
    if not DB_PATH.exists():
        return []
    conn = _db()
    try:
        sql = "SELECT project, node, path, text FROM chunks"
        params = []
        if project:
            sql += " WHERE project = ?"
            params.append(project)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    scored = []
    for proj, node, path, text in rows:
        score = _score(text or "", words)
        if score:
            scored.append({"project": proj, "node": node, "path": path,
                           "text": text or "", "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def context_block(hits: list, budget: int = MAX_CONTEXT_CHARS) -> str:
    parts = []
    used = 0
    for hit in hits:
        piece = "【%s / %s】\n%s" % (hit["project"], hit["node"], hit["text"])
        if used + len(piece) > budget:
            piece = piece[:max(0, budget - used)]
        if not piece.strip():
            break
        parts.append(piece)
        used += len(piece)
        if used >= budget:
            break
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 调模型
def chat(messages: list, temperature: float = 0.3, timeout: int = 90) -> dict:
    """调用用户配置的 OpenAI 兼容端点。"""
    cfg = config()
    if not ready():
        raise AIError("还没配置 AI 端点（地址 / API Key / 模型名）")
    url = cfg["base_url"] + "/chat/completions"
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
        "User-Agent": "MakiseToolkit/3.7",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise AIError("接口返回 %s：%s" % (exc.code, detail or exc.reason)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AIError("连不上 AI 端点：" + str(exc)[:160]) from None
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError("接口返回的格式不认识：" + json.dumps(payload)[:200]) from None
    usage = payload.get("usage") or {}
    return {"text": str(content).strip(), "model": payload.get("model") or cfg["model"],
            "usage": usage}


SYSTEM = (
    "你是一个论文写作助手。基于给出的资料回答，不要编造资料里没有的内容；"
    "资料不足时直接说「资料里没有相关内容」。回答用中文，简洁、分点。"
)


def ask(question: str, project: str = "", use_context: bool = True) -> dict:
    """检索 + 生成。返回 {answer, hits, context_chars, model}。"""
    hits = search(question, limit=6, project=project) if use_context else []
    ctx = context_block(hits)
    messages = [{"role": "system", "content": SYSTEM}]
    if ctx:
        messages.append({"role": "user", "content":
                         "下面是检索到的资料（来自用户本机）：\n\n" + ctx})
        messages.append({"role": "assistant", "content": "好的，我会只根据这些资料回答。"})
    messages.append({"role": "user", "content": question})
    result = chat(messages)
    return {"ok": True, "answer": result["text"], "model": result["model"],
            "usage": result.get("usage") or {},
            "hits": [{"project": h["project"], "node": h["node"], "score": h["score"],
                      "path": h["path"]} for h in hits],
            "context_chars": len(ctx), "at": time.strftime("%H:%M:%S")}
