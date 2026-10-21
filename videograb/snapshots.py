# -*- coding: utf-8 -*-
"""本地版本历史：SQLite + 内容寻址快照，不依赖 git。

为什么不用 git
--------------
用户是写论文的人，不该为了"能回滚"先去装 Git。这里用一个库文件自己记账：

    .mt/history.db
      blobs(hash, data)                 # 内容寻址，zlib 压缩，天然去重
      versions(id, sha, ts, message, branch, parent)
      files(version_id, path, hash)     # 每个版本 = 一份"路径→内容"清单
      branches(name, version_id)

每次保存把变了的文件算哈希、存新块、写一条版本；回滚就是把清单里的内容写回去。
同一个内容只存一份，所以改十次标点也不会把硬盘撑爆。

对外仍然用 git 的词汇（sha / commit / checkout），这样界面和 API 不用改，
需要时再导出成真正的 git 仓库。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from pathlib import Path

MT_DIR = ".mt"
DB_NAME = "history.db"
LARGE_LIMIT = int(os.environ.get("MT_LARGE_MB", "5")) * 1024 * 1024
SKIP_DIRS = {".git", ".mt", "node_modules", "__pycache__", ".idea", ".vscode"}
SKIP_SUFFIX = (".tmp", ".swp", ".swo", ".bak", ".part", ".crdownload", ".lock")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS blobs(
  hash TEXT PRIMARY KEY,
  data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha TEXT UNIQUE NOT NULL,
  ts INTEGER NOT NULL,
  message TEXT NOT NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  parent INTEGER
);
CREATE TABLE IF NOT EXISTS files(
  version_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  hash TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(version_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE TABLE IF NOT EXISTS branches(
  name TEXT PRIMARY KEY,
  version_id INTEGER
);
"""

_LOCK = threading.RLock()


class RepoError(Exception):
    pass


def _is_junk(rel: Path) -> bool:
    if any(part in SKIP_DIRS for part in rel.parts):
        return True
    name = rel.name
    if name.startswith("~$"):
        return True
    return name.lower().endswith(SKIP_SUFFIX)


class Repo:
    """一个项目一份历史库。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.db_path = self.root / MT_DIR / DB_NAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns = []          # 所有线程开过的连接，删项目时要一并关掉
        self.init()

    # ---------------- 底层
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            self._conns.append(conn)
        return conn

    def close(self) -> None:
        """关掉这个项目所有线程的连接（Windows 下文件被占用就删不掉目录）。"""
        with _LOCK:
            for conn in list(getattr(self, "_conns", [])):
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns = []
            try:
                if getattr(self._local, "conn", None):
                    self._local.conn = None
            except Exception:
                pass
            # Windows 上 SQLite 的 WAL 映射要等对象被回收才真正释放句柄，
            # 不主动跑一次 GC，删项目目录时会一直报"另一个程序正在使用此文件"。
            try:
                import gc
                gc.collect()
            except Exception:
                pass

    def init(self) -> None:
        with _LOCK:
            conn = self._conn()
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('format', '1')")
            row = conn.execute("SELECT 1 FROM branches WHERE name='main'").fetchone()
            if row is None:
                conn.execute("INSERT INTO branches(name, version_id) VALUES('main', NULL)")

    # ---------------- 工作区扫描
    def scan(self):
        """返回 {相对路径: (hash, size, 是否大文件)}"""
        out = {}
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(self.root)
            except ValueError:
                continue
            if _is_junk(rel):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > LARGE_LIMIT:
                # 大文件只记指纹，不存内容（docx/视频这类）
                stamp = "%s:%s:%s" % (path, size, path.stat().st_mtime)
                h = "L:" + hashlib.sha1(stamp.encode("utf-8")).hexdigest()
                out[rel.as_posix()] = (h, size, True)
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            out[rel.as_posix()] = ("S:" + hashlib.sha256(data).hexdigest(), size, False)
        return out

    def _put_blob(self, conn, data: bytes) -> str:
        h = "S:" + hashlib.sha256(data).hexdigest()
        conn.execute("INSERT OR IGNORE INTO blobs(hash, data) VALUES(?, ?)", (h, zlib.compress(data, 6)))
        return h

    def _get_blob(self, conn, h: str):
        row = conn.execute("SELECT data FROM blobs WHERE hash=?", (h,)).fetchone()
        if row is None:
            return None
        return zlib.decompress(row["data"])

    # ---------------- 版本
    def head(self):
        """当前分支的最新版本。"""
        conn = self._conn()
        branch = self.current_branch()
        row = conn.execute(
            "SELECT v.* FROM versions v WHERE v.id = "
            "(SELECT version_id FROM branches WHERE name=?)", (branch,)
        ).fetchone()
        return dict(row) if row else None

    def current_branch(self) -> str:
        conn = self._conn()
        row = conn.execute("SELECT v FROM meta WHERE k='current_branch'").fetchone()
        return row["v"] if row else "main"

    def set_branch(self, name: str) -> None:
        self._conn().execute("INSERT INTO meta(k, v) VALUES('current_branch', ?) "
                             "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (name,))

    def head_files(self):
        head = self.head()
        if not head:
            return {}
        conn = self._conn()
        rows = conn.execute("SELECT path, hash, size FROM files WHERE version_id=?", (head["id"],))
        return {r["path"]: (r["hash"], r["size"]) for r in rows}

    def version_files(self, sha: str):
        conn = self._conn()
        row = conn.execute("SELECT id FROM versions WHERE sha=? OR sha LIKE ?", (sha, sha + "%")).fetchone()
        if row is None:
            raise RepoError("找不到这个版本：%s" % sha)
        rows = conn.execute("SELECT path, hash, size FROM files WHERE version_id=?", (row["id"],))
        return {r["path"]: (r["hash"], r["size"]) for r in rows}

    def commit(self, message: str, only=None):
        """把工作区当前状态记成一个版本；没变化就返回 None。"""
        with _LOCK:
            conn = self._conn()
            branch = self.current_branch()
            head = self.head()
            head_files = self.head_files()
            work = self.scan()
            if only:
                keep = {p for p in work if p in set(only)} | {p: None for p in only if p not in work}
                subset = {}
                for p in only:
                    if p in work:
                        subset[p] = work[p]
                work = subset

            changed = {}
            for p, (h, size, big) in work.items():
                old = head_files.get(p)
                if old is None or old[0] != h:
                    changed[p] = (h, size, big)
            removed = [p for p in head_files if p not in work]
            if only:
                removed = [p for p in removed if p in set(only)]
            if not changed and not removed:
                return None

            sha = hashlib.sha1(("%s|%s|%s" % (time.time(), message, sorted(changed))).encode()).hexdigest()
            cur = conn.execute(
                "INSERT INTO versions(sha, ts, message, branch, parent) VALUES(?,?,?,?,?)",
                (sha, int(time.time()), message, branch, head["id"] if head else None),
            )
            vid = cur.lastrowid
            for p, (h, size, big) in changed.items():
                if not big:
                    data = (self.root / p).read_bytes()
                    self._put_blob(conn, data)
                conn.execute("INSERT OR REPLACE INTO files(version_id, path, hash, size) VALUES(?,?,?,?)",
                             (vid, p, h, size))
            for p in head_files:
                if p in changed:
                    continue
                if only and p not in set(only):
                    old = head_files[p]
                    conn.execute("INSERT OR REPLACE INTO files(version_id, path, hash, size) VALUES(?,?,?,?)",
                                 (vid, p, old[0], old[1]))
                elif p in work:
                    h, size, _big = work[p]
                    conn.execute("INSERT OR REPLACE INTO files(version_id, path, hash, size) VALUES(?,?,?,?)",
                                 (vid, p, h, size))
            conn.execute("UPDATE branches SET version_id=? WHERE name=?", (vid, branch))
            return {"sha": sha, "files": sorted(changed) + ["(删除) " + p for p in removed]}

    # ---------------- 状态 / 读取
    def status(self):
        """哪些文件跟最新版本不一样（含未跟踪）。"""
        work = self.scan()
        head = self.head_files()
        changed = []
        for p, (h, size, big) in work.items():
            if p not in head or head[p][0] != h:
                changed.append(p)
        for p in head:
            if p not in work:
                changed.append(p)
        return sorted(set(changed))

    def log(self, limit: int = 100):
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, sha, ts, message, branch FROM versions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            files = [x["path"] for x in conn.execute(
                "SELECT f.path FROM files f WHERE f.version_id=? "
                "AND NOT EXISTS (SELECT 1 FROM files p WHERE p.version_id=(SELECT parent FROM versions WHERE id=?) "
                "AND p.path=f.path AND p.hash=f.hash)", (r["id"], r["id"]))]
            out.append({"sha": r["sha"], "ts": r["ts"], "message": r["message"],
                        "branch": r["branch"], "files": files})
        return out

    def restore(self, sha: str, paths=None) -> dict:
        """把指定版本的内容写回工作区，返回恢复的文件列表。"""
        with _LOCK:
            conn = self._conn()
            targets = self.version_files(sha)
            if paths:
                wanted = {str(p).replace("\\", "/") for p in paths}
                targets = {p: v for p, v in targets.items() if p in wanted}
            restored, skipped = [], []
            # 整份回滚：目标版本里没有的文件要删掉，否则"回到某个版本"会留下后来的文件
            if not paths:
                for p in list(self.scan()):
                    if p not in targets:
                        try:
                            (self.root / p).unlink()
                        except OSError:
                            pass
            for p, (h, size) in targets.items():
                if h.startswith("L:"):
                    skipped.append(p)      # 大文件当初没存内容
                    continue
                data = self._get_blob(conn, h)
                if data is None:
                    skipped.append(p)
                    continue
                target = self.root / p
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                restored.append(p)
            return {"restored": restored, "skipped": skipped}

    def file_history(self, path: str):
        conn = self._conn()
        rows = conn.execute(
            "SELECT v.sha, v.ts, v.message, f.hash FROM files f JOIN versions v ON v.id=f.version_id "
            "WHERE f.path=? ORDER BY v.id DESC LIMIT 50", (path,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 分支（只是指针，够用）
    def branch_list(self):
        conn = self._conn()
        rows = conn.execute("SELECT name FROM branches ORDER BY name").fetchall()
        return [r["name"] for r in rows], self.current_branch()

    def branch_create(self, name: str) -> str:
        conn = self._conn()
        if conn.execute("SELECT 1 FROM branches WHERE name=?", (name,)).fetchone():
            raise RepoError("分支已经存在：%s" % name)
        head = self.head()
        conn.execute("INSERT INTO branches(name, version_id) VALUES(?, ?)",
                     (name, head["id"] if head else None))
        self.set_branch(name)
        return name

    def branch_switch(self, name: str) -> None:
        conn = self._conn()
        row = conn.execute("SELECT version_id FROM branches WHERE name=?", (name,)).fetchone()
        if row is None:
            raise RepoError("没有这个分支：%s" % name)
        self.set_branch(name)
        if row["version_id"]:
            sha = conn.execute("SELECT sha FROM versions WHERE id=?", (row["version_id"],)).fetchone()["sha"]
            self.restore(sha)

    def branch_delete(self, name: str) -> None:
        if name == self.current_branch():
            raise RepoError("不能删掉当前分支")
        conn = self._conn()
        conn.execute("DELETE FROM branches WHERE name=?", (name,))

    # ---------------- 去重清理
    def gc(self) -> int:
        """删掉没有被任何版本引用的内容块，返回释放的块数。"""
        with _LOCK:
            conn = self._conn()
            cur = conn.execute(
                "DELETE FROM blobs WHERE hash NOT IN (SELECT DISTINCT hash FROM files)")
            removed = cur.rowcount or 0
            conn.execute("VACUUM")
            return removed

    def stats(self) -> dict:
        conn = self._conn()
        versions = conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"]
        blobs = conn.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
        raw = conn.execute("SELECT COALESCE(SUM(LENGTH(data)),0) s FROM blobs").fetchone()["s"]
        return {"versions": versions, "blobs": blobs, "bytes": raw}

    # ---------------- 导出成真 git 仓库
    def export_git(self, target: Path, message_chunk: int = 200) -> dict:
        """把历史重放成真正的 git 仓库（需要系统里装了 git）。"""
        import shutil
        import subprocess
        git = shutil.which("git")
        if not git:
            raise RepoError("没找到 git，装一个再导出")
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")

        def run(*args):
            p = subprocess.run([git, *args], cwd=str(target), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
            if p.returncode != 0:
                raise RepoError("git 失败：" + ((p.stderr or p.stdout or "").strip().splitlines() or ["未知"])[0])
            return p.stdout

        if not (target / ".git").exists():
            run("init")
            run("config", "user.name", "Makise Toolkit")
            run("config", "user.email", "mt@localhost")
        conn = self._conn()
        versions = conn.execute("SELECT id, sha, ts, message FROM versions ORDER BY id ASC").fetchall()
        exported = 0
        for v in versions[-message_chunk:]:
            files = conn.execute("SELECT path, hash FROM files WHERE version_id=?", (v["id"],)).fetchall()
            keep = set()
            for f in files:
                data = self._get_blob(conn, f["hash"])
                if data is None:
                    continue
                dst = target / f["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
                keep.add(f["path"])
            for old in target.rglob("*"):
                if old.is_file():
                    rel = old.relative_to(target).as_posix()
                    if rel.startswith(".git/"):
                        continue
                    if rel not in keep:
                        old.unlink()
            run("add", "-A")
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["ts"]))
            run("commit", "-m", "%s (@%s)" % (v["message"], stamp), "--allow-empty")
            exported += 1
        return {"exported": exported, "target": str(target)}
