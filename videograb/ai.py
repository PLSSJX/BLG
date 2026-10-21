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
def _migrate(st) -> list:
    """老版本只能配一个端点，第一次升级时把它变成一条助手记录。"""
    items = list(getattr(st, "ai_assistants", []) or [])
    if items:
        return items
    base = (getattr(st, "ai_base_url", "") or "").strip()
    key = (getattr(st, "ai_api_key", "") or "").strip()
    model = (getattr(st, "ai_model", "") or "").strip()
    # 只填了默认地址和模型、没填 key 的，不算"配置过"，别凭空造一条空助手
    if not (base and model and key):
        return []
    item = {
        "id": "default",
        "name": model or "默认助手",
        "base_url": base.rstrip("/"),
        "api_key": key,
        "model": model,
    }
    st.ai_assistants = [item]
    if not getattr(st, "ai_active", ""):
        st.ai_active = item["id"]
    try:
        from .config import Context
        Context.__dict__  # 仅用于触发导入，不写盘
    except Exception:
        pass
    return [item]


def assistants() -> list:
    st = load_settings()
    items = _migrate(st)
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append({
            "id": item.get("id") or "",
            "name": item.get("name") or item.get("model") or "未命名",
            "base_url": item.get("base_url") or "",
            "model": item.get("model") or "",
            "has_key": bool(item.get("api_key")),
        })
    return out


def _assistant(assistant_id: str = "") -> dict:
    st = load_settings()
    items = _migrate(st)
    want = assistant_id or getattr(st, "ai_active", "") or ""
    chosen = None
    for item in items:
        if item.get("id") == want:
            chosen = item
            break
    if chosen is None and items:
        chosen = items[0]
    if chosen is None:
        return {}
    return {
        "id": chosen.get("id") or "",
        "name": chosen.get("name") or "",
        "base_url": (chosen.get("base_url") or "").rstrip("/"),
        "api_key": chosen.get("api_key") or "",
        "model": chosen.get("model") or "",
    }


def config(assistant_id: str = "") -> dict:
    item = _assistant(assistant_id)
    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "base_url": item.get("base_url", ""),
        "api_key": item.get("api_key", ""),
        "model": item.get("model", ""),
    }


def ready(assistant_id: str = "") -> bool:
    cfg = config(assistant_id)
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


def save_assistants(items: list, active: str = "") -> dict:
    """整表保存。api_key 传空字符串表示"不修改"，避免界面回显时把 key 抹掉。"""
    st = load_settings()
    old = {x.get("id"): x for x in (getattr(st, "ai_assistants", []) or []) if isinstance(x, dict)}
    clean = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or "").strip() or ("a" + str(abs(hash(str(item.get("model") or "x"))) % 100000))
        key = item.get("api_key")
        if key is None or key == "":
            key = (old.get(ident) or {}).get("api_key", "")
        clean.append({
            "id": ident,
            "name": str(item.get("name") or item.get("model") or "助手").strip(),
            "base_url": str(item.get("base_url") or "").strip().rstrip("/"),
            "api_key": str(key or ""),
            "model": str(item.get("model") or "").strip(),
        })
    st.ai_assistants = clean
    want = active or getattr(st, "ai_active", "") or ""
    ids = [x["id"] for x in clean]
    st.ai_active = want if want in ids else (ids[0] if ids else "")
    from .config import Context
    ctx = Context()
    ctx.settings = st         # 关键：把改好的对象交给它，否则写盘的是另一份
    ctx.save()
    return {"assistants": assistants(), "active": st.ai_active}


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


def index_path(path: str, project: str = "", node: str = "") -> int:
    """只更新一个文件的索引，不重建整库。

    单节点保存、OCR 出结果之后调它 —— 全量重建要扫几百个文件，
    为一次保存等两秒不值当。
    """
    target = Path(str(path))
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    conn = _db()
    try:
        conn.execute("DELETE FROM chunks WHERE path = ?", (str(target),))
        count = 0
        for piece in _chunks(text):
            conn.execute("INSERT INTO chunks(project, node, path, text) VALUES (?,?,?,?)",
                         (project or "", node or target.name, str(target), piece))
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


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
def chat(messages: list, temperature: float = 0.3, timeout: int = 90, assistant_id: str = "") -> dict:
    """调用用户配置的 OpenAI 兼容端点。"""
    cfg = config(assistant_id)
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
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


def ask(question: str, project: str = "", use_context: bool = True, assistant_id: str = "") -> dict:
    """检索 + 生成。返回 {answer, hits, context_chars, model}。"""
    hits = search(question, limit=6, project=project) if use_context else []
    ctx = context_block(hits)
    messages = [{"role": "system", "content": SYSTEM}]
    if ctx:
        messages.append({"role": "user", "content":
                         "下面是检索到的资料（来自用户本机）：\n\n" + ctx})
        messages.append({"role": "assistant", "content": "好的，我会只根据这些资料回答。"})
    messages.append({"role": "user", "content": question})
    result = chat(messages, assistant_id=assistant_id)
    cfg = config(assistant_id)
    return {"ok": True, "answer": result["text"], "model": result["model"], "assistant": cfg.get("name") or "",
            "usage": result.get("usage") or {},
            "hits": [{"project": h["project"], "node": h["node"], "score": h["score"],
                      "path": h["path"]} for h in hits],
            "context_chars": len(ctx), "at": time.strftime("%H:%M:%S")}


# ---------------------------------------------------------------- 聊天记录
CHAT_PATH = DATA_DIR / "ai" / "chat.json"


def chat_history() -> list:
    try:
        data = json.loads(CHAT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("messages") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _save_chat(items: list) -> None:
    CHAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHAT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"messages": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CHAT_PATH)


def chat_append(role: str, text: str, meta: dict | None = None) -> dict:
    import uuid
    items = chat_history()
    message = {
        "id": uuid.uuid4().hex[:12],
        "role": "user" if role == "user" else "assistant",
        "text": str(text or ""),
        "at": time.strftime("%H:%M"),
    }
    if meta:
        message.update({k: v for k, v in meta.items() if k in ("assistant", "model", "hits")})
    items.append(message)
    _save_chat(items[-400:])          # 只留最近 400 条，别让文件无限长
    return message


def chat_delete(message_id: str) -> bool:
    items = chat_history()
    left = [x for x in items if x.get("id") != message_id]
    if len(left) == len(items):
        return False
    _save_chat(left)
    return True


def chat_clear() -> int:
    items = chat_history()
    _save_chat([])
    return len(items)
