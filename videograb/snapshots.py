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
# .mt 里只有这两个文件值得跟随版本：节点结构和设置。
# history.db 本身当然不能进版本库，否则每存一版就把整个库再存一遍。
VERSIONED_META = {".mt/graph.json", ".mt/settings.json"}
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
    posix = rel.as_posix()
    if posix in VERSIONED_META:
        return False
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
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                # 库坏了：必须先关上这个句柄，否则 Windows 下文件被占用，
                # 后面想把坏库改名留档会直接失败（rename 返回 false）。
                try:
                    conn.close()
                except Exception:
                    pass
                raise
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

    def _quarantine_broken(self) -> bool:
        """history.db 读不出来时把它改名留档，然后新建一个空库。

        真实场景：断电写到一半、杀毒软件截断、用户手滑覆盖。
        这时最要紧的是**正文不能丢**、项目还能继续存版本；
        历史确实没了，但要把坏库留成 .broken-<时间> 供人工抢救，
        而不是让项目从此打不开、永远提交失败。
        """
        broken = self.db_path.with_name(
            self.db_path.name + ".broken-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            for suffix in ("", "-wal", "-shm"):
                src = self.db_path.with_name(self.db_path.name + suffix)
                if src.exists():
                    src.replace(broken.with_name(broken.name + suffix))
        except OSError:
            return False
        try:
            self._local.conn = None
        except Exception:
            pass
        try:
            self._conns = []
        except Exception:
            pass
        return True

    def init(self) -> None:
        with _LOCK:
            try:
                conn = self._conn()
                conn.executescript(SCHEMA)
            except sqlite3.DatabaseError:
                # 库坏了：留档 → 新建 → 再来一次。正文不受影响（正文是普通文件）。
                if not self._quarantine_broken():
                    raise
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

    # ---------------- 合并分支
    def _version_by_sha(self, sha: str):
        conn = self._conn()
        return conn.execute("SELECT * FROM versions WHERE sha=? OR sha LIKE ?", (sha, sha + "%")).fetchone()

    def _files_of(self, vid: int):
        conn = self._conn()
        return {r["path"]: r["hash"] for r in
                conn.execute("SELECT path, hash FROM files WHERE version_id=?", (vid,))}

    def _ancestors(self, vid: int) -> list:
        """从自己往上追父版本，返回 [自己, 父, 祖父, ...]。"""
        conn = self._conn()
        chain = []
        seen = set()
        while vid and vid not in seen:
            seen.add(vid)
            chain.append(vid)
            row = conn.execute("SELECT parent FROM versions WHERE id=?", (vid,)).fetchone()
            vid = row["parent"] if row else None
        return chain

    def merge_from(self, branch: str, message: str = "") -> dict:
        """把 branch 合进当前分支（三方合并）。

        没有公共祖先、或者两边改同一段文本时，会如实报告冲突。
        文本文件用 <<<<<<< ======= >>>>>>> 标记，二进制文件保留"我的"。
        """
        conn = self._conn()
        target = conn.execute("SELECT version_id FROM branches WHERE name=?", (branch,)).fetchone()
        if target is None:
            raise RepoError("没有这个分支：%s" % branch)
        if branch == self.current_branch():
            raise RepoError("不能把分支合进它自己")
        mine = self.head()
        if mine is None:
            raise RepoError("当前分支还没有任何版本")
        if target["version_id"] is None:
            return {"merged": False, "conflicts": [], "message": "对方分支还没有版本，没东西可合"}
        theirs_id = target["version_id"]

        # 找公共祖先
        mine_chain = self._ancestors(mine["id"])
        theirs_set = set(self._ancestors(theirs_id))
        base_id = next((v for v in mine_chain if v in theirs_set), None)

        ours = self._files_of(mine["id"])
        theirs = self._files_of(theirs_id)
        base = self._files_of(base_id) if base_id else {}

        merged, conflicts = {}, []
        for path in sorted(set(ours) | set(theirs)):
            o = ours.get(path)
            t = theirs.get(path)
            b = base.get(path)
            if o == t:
                chosen = o
            elif o == b:
                chosen = t                      # 只有对方改了
            elif t == b:
                chosen = o                      # 只有我改了
            else:
                # 两边都改过同一个文件 → 冲突，文本试着合并
                merged_text, ok = self._try_merge_text(path, b, o, t)
                if ok:
                    merged[path] = merged_text
                    continue
                conflicts.append({"path": path, "reason": "两边都改过这个文件"})
                chosen = o                      # 先保留"我的"
            if chosen is None:
                continue                        # 对方删了、我没动 → 跟着删
            merged[path] = chosen

        # 写回工作区
        for path, payload in merged.items():
            dest = self.root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, bytes):
                dest.write_bytes(payload)
            else:
                data = self._get_blob(conn, payload)
                if data is None:
                    continue
                if isinstance(data, str):
                    data = data.encode("utf-8")
                dest.write_bytes(data)
        for path in ours:
            if path not in merged:
                try:
                    (self.root / path).unlink()
                except OSError:
                    pass

        msg = message or ("mt: 合并分支 %s" % branch)
        result = self.commit(msg)
        return {"merged": True, "conflicts": conflicts,
                "sha": (result or {}).get("sha") or (mine or {}).get("sha"),
                "message": "已合并 %s" % branch}

    def _try_merge_text(self, path: str, base, ours, theirs):
        """三个版本都是文本时尝试行级合并；否则返回 (None, False)。"""
        import difflib
        conn = self._conn()

        def load(h):
            if h is None:
                return None
            data = self._get_blob(conn, h)
            if data is None:
                return None
            try:
                return data.decode("utf-8")
            except Exception:
                return None

        b_txt, o_txt, t_txt = load(base), load(ours), load(theirs)
        if o_txt is None or t_txt is None:
            return None, False
        b_lines = (b_txt or "").splitlines(keepends=True)
        o_lines = o_txt.splitlines(keepends=True)
        t_lines = t_txt.splitlines(keepends=True)
        if not b_lines:
            # 没有公共祖先：两边各自新增，直接拼接会乱，算冲突
            return None, False
        merged = list(difflib.unified_diff([], [], lineterm="")) if False else None
        # 简单策略：把两边对 base 的改动应用上去；同一行被两边改动则冲突
        out = []
        sm_o = difflib.SequenceMatcher(None, b_lines, o_lines)
        sm_t = difflib.SequenceMatcher(None, b_lines, t_lines)
        changed_o = {}
        for tag, i1, i2, j1, j2 in sm_o.get_opcodes():
            if tag != "equal":
                for i in range(i1, i2):
                    changed_o[i] = o_lines[j1:j2]
        changed_t = {}
        for tag, i1, i2, j1, j2 in sm_t.get_opcodes():
            if tag != "equal":
                for i in range(i1, i2):
                    changed_t[i] = t_lines[j1:j2]
        overlap = set(changed_o) & set(changed_t)
        if overlap:
            return None, False
        i = 0
        while i < len(b_lines):
            if i in changed_o:
                out.extend(changed_o[i]); i += 1; continue
            if i in changed_t:
                out.extend(changed_t[i]); i += 1; continue
            out.append(b_lines[i]); i += 1
        # 末尾新增
        tail_o = o_lines[len(b_lines):]
        tail_t = t_lines[len(b_lines):]
        if tail_o and tail_t and tail_o != tail_t:
            return None, False
        out.extend(tail_o or tail_t or [])
        return "".join(out).encode("utf-8"), True

    # ---------------- 去重清理
    def gc(self, keep_recent: int = 0) -> dict:
        """瘦身：删掉"没有任何分支还能走到"的版本，再删没人引用的内容块。

        什么情况下会产生这种版本？
          · 删掉了一个分支 —— 它上面的版本从任何分支都走不到了
          · keep_recent > 0 时，每个分支只保留最近 N 个版本，更早的会被丢掉
        返回 {removed_versions, removed_blocks}。
        """
        with _LOCK:
            conn = self._conn()
            keep = set()
            for row in conn.execute("SELECT version_id FROM branches WHERE version_id IS NOT NULL"):
                vid = row["version_id"]
                while vid and vid not in keep:
                    keep.add(vid)
                    parent = conn.execute("SELECT parent FROM versions WHERE id=?", (vid,)).fetchone()
                    vid = parent["parent"] if parent else None
            if keep_recent and keep_recent > 0:
                # 只保留每个分支最近的 N 个版本
                trimmed = set()
                for row in conn.execute("SELECT version_id FROM branches WHERE version_id IS NOT NULL"):
                    vid = row["version_id"]
                    for _ in range(keep_recent):
                        if not vid:
                            break
                        trimmed.add(vid)
                        parent = conn.execute("SELECT parent FROM versions WHERE id=?", (vid,)).fetchone()
                        vid = parent["parent"] if parent else None
                keep &= trimmed

            removed_versions = 0
            if keep:
                placeholders = ",".join("?" * len(keep))
                cur = conn.execute("DELETE FROM versions WHERE id NOT IN (%s)" % placeholders, tuple(keep))
                removed_versions = cur.rowcount or 0
                conn.execute("DELETE FROM files WHERE version_id NOT IN (SELECT id FROM versions)")
            cur = conn.execute(
                "DELETE FROM blobs WHERE hash NOT IN (SELECT DISTINCT hash FROM files)")
            removed_blocks = cur.rowcount or 0
            conn.execute("VACUUM")
            return {"removed_versions": removed_versions, "removed_blocks": removed_blocks}

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
