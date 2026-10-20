# -*- coding: utf-8 -*-
"""外部编辑 → 自动提交的监听器。

判断「可以提交」要同时满足三条：
  ① 内容真的变了（size/mtime 变了，且 hash 不同）
  ② 没有进程持有它（Windows Restart Manager 查不到持有者）
  ③ 静默窗口已过（最后一次变化后又安静了 QUIET_SECONDS 秒）

另外：
  · 忽略编辑器临时文件（~$xxx、*.swp、*.tmp…）和 .git/、.mt/
  · 检测到 HEAD 分支变化 → 暂停并提示（否则可能提交到错的分支）
  · 支持手动暂停 / 恢复
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

QUIET_SECONDS = float(os.environ.get("MT_QUIET_SECONDS", "30"))   # 静默窗口（环境变量可覆盖，方便测试）
POLL_SECONDS = 2.0          # 轮询间隔
MAX_WAIT_LOCK = 120.0       # 一直有进程持有最多等这么久，之后强制提交
HASH_LIMIT = 4 * 1024 * 1024  # 超过这个大小不算 hash，只比 size+mtime

JUNK_SUFFIX = (".tmp", ".swp", ".swo", ".bak", ".part", ".crdownload", ".lock")
JUNK_DIRS = {".git", ".mt", "node_modules", "__pycache__"}


# ------------------------------------------------------------------ 锁检测
def _holders(path: Path) -> list:
    """判断文件是否被别的程序独占打开。

    早先这里用 Windows Restart Manager（ctypes 调 rstrtmgr.dll），
    在 64 位下会偶发访问冲突，直接把整个服务进程打崩（无 Python 异常可捕获）。
    改成一个纯 Python 的探测：独占打开时 os.open 会失败。

    注意：VS Code / Typora 这类编辑器根本不锁文件，这里查不到占用是正常的，
    它们靠「内容变化 + 静默窗口」判断，不依赖这个。
    被占用时返回 [(None, "占用中")]，否则返回 []。
    """
    if os.name != "nt":
        return []
    try:
        if not path.exists() or path.is_dir():
            return []
    except OSError:
        return []
    try:
        fd = os.open(str(path), os.O_RDWR)
    except PermissionError:
        return [(None, "被其他程序占用")]
    except OSError:
        return []          # 其它原因（只读、权限等）：当作可以提交
    else:
        os.close(fd)
        return []


def _digest(path: Path):
    """(size, mtime, hash)。大文件不做 hash，返回 None。"""
    st = path.stat()
    if st.st_size > HASH_LIMIT:
        return (st.st_size, int(st.st_mtime), None)
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return None                          # 读不了（正被写），当作没变
    return (st.st_size, int(st.st_mtime), h.hexdigest())


def _is_junk(rel: Path) -> bool:
    if any(part in JUNK_DIRS for part in rel.parts):
        return True
    name = rel.name
    if name.startswith("~$"):                # Office / WPS 的锁文件
        return True
    return name.lower().endswith(JUNK_SUFFIX)


# ------------------------------------------------------------------ 监听器
class ProjectWatch:
    def __init__(self, path: Path):
        self.path = path
        self.seen = {}           # rel -> (size, mtime, hash)
        self.pending = set()     # 等待提交的相对路径
        self.first_change = 0.0
        self.last_change = 0.0
        self.snapshot()          # 先记住当前状态，避免启动时就当成"全改了"
        self.last_head = ""
        self.last_branch = ""
        self.paused = False
        self.pause_reason = ""
        self.last_commit = 0.0
        self.last_error = ""
        self.commits = 0


    def files(self):
        out = []
        for p in self.path.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.path)
            if _is_junk(rel):
                continue
            out.append(rel)
        return out


    def snapshot(self):
        for rel in self.files():
            try:
                d = _digest(self.path / rel)
            except OSError:
                continue
            if d:
                self.seen[rel] = d


    def scan(self):
        """返回这次新变化的相对路径列表。"""
        changed = []
        current = set()
        for rel in self.files():
            current.add(rel)
            try:
                d = _digest(self.path / rel)
            except OSError:
                continue
            if d is None:
                continue
            old = self.seen.get(rel)
            if old != d:
                self.seen[rel] = d
                if old is not None:
                    changed.append(rel)
                else:
                    # 新文件也算改动（但首次快照时不报）
                    changed.append(rel)
        for rel in list(self.seen):
            if rel not in current:
                del self.seen[rel]
                changed.append(rel)
        return changed


    def status(self) -> dict:
        if self.paused:
            state = "paused"
        elif not self.pending:
            state = "idle"
        else:
            wait_left = max(0.0, QUIET_SECONDS - (time.time() - self.last_change))
            state = "waiting" if wait_left > 0 else "ready"
        return {
            "state": state,
            "pending": sorted(str(r).replace("\\", "/") for r in self.pending)[:20],
            "pending_count": len(self.pending),
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "seconds_left": round(max(0.0, QUIET_SECONDS - (time.time() - self.last_change)), 1),
            "commits": self.commits,
            "last_error": self.last_error,
        }
