# -*- coding: utf-8 -*-
"""项目管理后端：从原型搬过来，去掉独立 HTTP 服务的部分。

安全边界保持不变：
  · 只认登记表里的项目 id，不接受任意路径
  · 所有路径校验必须落在项目目录内
  · 调 git 一律参数列表
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import git_compat
from .snapshots import Repo, RepoError

USE_REAL_GIT = os.environ.get("MT_USE_GIT", "") == "1"

REGISTRY: Path = Path("projects.json")   # 由 init() 指定实际位置
MT_DIR = ".mt"
LARGE_LIMIT = int(os.environ.get("MT_LARGE_MB", "5")) * 1024 * 1024   # 超过就只记变化，不存内容
GIT = shutil.which("git") or "git"



WATCHERS: dict = {}          # project_id -> ProjectWatch
WATCH_ERROR = ""             # 监听线程自身最近一次故障
# graph.json 会被多个入口读写：界面保存、删除节点、挂文件、监听器的摘要同步。
# 没有这把锁时，摘要同步的「读-改-写」会覆盖掉用户刚保存的结构（实测踩过）。
GRAPH_LOCK = threading.RLock()
WATCH_LOCK = threading.RLock()



# ------------------------------------------------------------------ 监听器（来自原型 watcher.py）

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
    """判断文件是否被别的程序占用（Windows）。

    做法：用 CreateFileW 以「独占、不共享」的方式试着打开一次。
    如果别的程序（Word / WPS / 记事本 / 其它编辑器）正开着它，
    这个调用会失败，我们就知道不能提交。

    注意：以前这里用 Restart Manager（ctypes 回调 + rstrtmgr.dll），
    在 64 位下会偶发访问冲突直接把进程打崩，所以改成这种一次性的简单调用：
    没有回调、没有注册，失败最多是判断不出来，不会带走整个进程。
    """
    if os.name != "nt":
        return []
    try:
        if not path.exists() or path.is_dir():
            return []
    except OSError:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID = ctypes.c_void_p(-1).value
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.HANDLE]
        handle = kernel32.CreateFileW(str(path), GENERIC_READ | GENERIC_WRITE, 0,
                                      None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if handle == INVALID or handle is None:
            err = kernel32.GetLastError()
            # 32 = 共享冲突（被打开）；5 = 拒绝访问（只读或权限），不算"被占用"
            if err == 32:
                return [(None, "被其他程序占用")]
            return []
        kernel32.CloseHandle(handle)
        return []
    except Exception:
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


class ApiError(Exception):
    pass


def watcher_for(item: dict) -> ProjectWatch:
    """每个项目一个监听器，惰性创建。"""
    with WATCH_LOCK:
        pw = WATCHERS.get(item["id"])
        if pw is None:
            pw = ProjectWatch(project_dir(item))
            WATCHERS[item["id"]] = pw
        return pw


def load_registry() -> list:
    if not REGISTRY.exists():
        return []
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_registry(items: list) -> None:
    REGISTRY.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def find_project(pid: str) -> dict:
    for item in load_registry():
        if item.get("id") == pid:
            return item
    raise ApiError("找不到这个项目")


def project_dir(item: dict) -> Path:
    path = Path(item["path"]).resolve()
    if not path.is_dir():
        raise ApiError("项目目录不存在了：" + str(path))
    return path


def run_git(cwd: Path, *args: str) -> str:
    """版本历史的后端。

    默认用 SQLite 快照（.mt/history.db），用户不需要装 git；
    设置环境变量 MT_USE_GIT=1 时仍然调用系统 git。
    """
    if USE_REAL_GIT:
        return _run_real_git(cwd, *args)
    try:
        return git_compat.run(cwd, *args)
    except git_compat.GitCompatError as exc:
        raise ApiError(str(exc)) from None
    except Exception as exc:
        raise ApiError("版本记录失败：" + str(exc)) from None


def _run_real_git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            [GIT, "-c", "core.quotepath=false", *args],
            cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, env=env,
        )
    except FileNotFoundError:
        raise ApiError("没找到 git") from None
    except subprocess.TimeoutExpired:
        raise ApiError("git 命令超时") from None
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ApiError("git 失败：" + (msg[0] if msg else "未知原因"))
    return proc.stdout
def ensure_repo(path: Path) -> None:
    if not (path / ".git").exists():
        run_git(path, "init")
        run_git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    try:
        run_git(path, "config", "--get", "user.name")
    except ApiError:
        run_git(path, "config", "user.name", "Makise Toolkit")
        run_git(path, "config", "user.email", "mt@localhost")


def touch_known_head(path: Path) -> None:
    """我们自己提交完就把 HEAD 记下来。

    不记的话，下一次打开项目会拿旧的 known_head 和新的 HEAD 比对，
    于是自己刚刚的提交会被当成"被外部 git 修改过"，横幅一直亮着。
    """
    try:
        head = run_git(path, "rev-parse", "HEAD").strip()
    except ApiError:
        return
    items = load_registry()
    changed = False
    for entry in items:
        try:
            if Path(entry.get("path", "")).resolve() == path:
                entry["known_head"] = head
                changed = True
        except Exception:
            continue
    if changed:
        save_registry(items)


def has_changes(path: Path) -> bool:
    return bool(run_git(path, "status", "--porcelain").strip())


def commit_paths(path: Path, message: str, rels: list) -> dict:
    """只提交指定路径。用于「删除节点」这类必须留一个可撤销点的操作。

    不写 -A：用户可能正开着别的文件在改，不能顺手提交掉。
    """
    seen = []
    for rel in rels:
        if not rel:
            continue
        rel = str(rel).replace("\\", "/")
        if rel not in seen:
            seen.append(rel)
    if not seen:
        return {"committed": False, "reason": "没有要提交的路径"}
    # 只把"存在的"和"git 认识的（用来记录删除）"交给 git，
    # 否则删掉一个从没提交过的文件会报 pathspec did not match
    usable = []
    for rel in seen:
        try:
            if (path / rel).exists():
                usable.append(rel)
            elif run_git(path, "ls-files", "--", rel).strip():
                usable.append(rel)
        except ApiError:
            continue
    if not usable:
        return {"committed": False, "reason": "这些路径 git 里都没有"}
    # -A 允许记录删除；路径限定在 usable 里
    run_git(path, "add", "-A", "--", *usable)
    staged = run_git(path, "diff", "--cached", "--name-only").strip()
    if not staged:
        return {"committed": False, "reason": "这些路径没有变化"}
    run_git(path, "commit", "-m", message)
    touch_known_head(path)
    return {"committed": True, "sha": run_git(path, "rev-parse", "HEAD").strip(),
            "files": staged.splitlines()}


def commit(path: Path, message: str) -> dict:
    if not has_changes(path):
        return {"committed": False, "reason": "没有改动"}
    run_git(path, "add", "-A")
    run_git(path, "commit", "-m", message or "mt: 保存")
    touch_known_head(path)
    return {"committed": True, "sha": run_git(path, "rev-parse", "HEAD").strip()}


def settings_path(path: Path) -> Path:
    d = path / MT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "settings.json"


def read_settings(path: Path) -> dict:
    p = settings_path(path)
    if not p.exists():
        return {"auto_summary": True}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"auto_summary": True, **data}
    except Exception:
        return {"auto_summary": True}


def write_settings(path: Path, data: dict) -> dict:
    current = read_settings(path)
    current.update(data)
    settings_path(path).write_text(json.dumps(current, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    return current


def graph_path(path: Path) -> Path:
    d = path / MT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "graph.json"


def write_graph(path: Path, graph: dict) -> None:
    with GRAPH_LOCK:
        graph_path(path).write_text(json.dumps(graph, ensure_ascii=False, indent=1),
                                    encoding="utf-8")


def locked_read_graph(path: Path) -> dict:
    with GRAPH_LOCK:
        gp = graph_path(path)
        if not gp.exists():
            return {"trunk": {"id": "t1", "title": path.name}, "nodes": [], "staged": []}
        try:
            return json.loads(gp.read_text(encoding="utf-8"))
        except Exception:
            return {"trunk": {"id": "t1", "title": path.name}, "nodes": [], "staged": []}


BINARY_SUFFIX = (".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx", ".ppt",
                 ".png", ".jpg", ".jpeg", ".gif", ".zip", ".rar", ".7z",
                 ".mp4", ".mp3", ".csv", ".bin", ".txt")


def safe_filename(title: str) -> str:
    """把节点标题变成一个安全的文件名。

    标题如果本身是个文件名（例如从附件拖出来的「示波器报告.docx」），
    要把扩展名去掉，否则会生成「示波器报告.docx.md」这种怪东西。
    """
    bad = '\\/:*?"<>|'
    name = "".join("_" if ch in bad else ch for ch in (title or "未命名")).strip()
    name = name.strip(". ") or "未命名"
    lower = name.lower()
    for suffix in BINARY_SUFFIX:
        if lower.endswith(suffix):
            name = name[: -len(suffix)].strip(". ") or name
            break
    if lower.endswith(".md"):
        name = name[:-3].strip(". ") or name
    return name[:60]


def unique_path(path: Path, base: str) -> str:
    """重名就加 -2、-3，不覆盖已有文件。"""
    rel = base + ".md"
    n = 2
    while (path / rel).exists():
        rel = base + "-" + str(n) + ".md"
        n += 1
    return rel


def large_manifest_path(path: Path) -> Path:
    d = path / MT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "large.json"


def read_large(path: Path) -> dict:
    p = large_manifest_path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_large(path: Path, data: dict) -> None:
    large_manifest_path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                         encoding="utf-8")


def update_gitignore(path: Path, large_rel: list) -> None:
    """把大文件写进 .gitignore 的托管区块，其它行保持不动。"""
    gi = path / ".gitignore"
    begin, end = "# --- Makise Toolkit 大文件（不进 git）---", "# --- /Makise Toolkit ---"
    body = []
    if gi.exists():
        text = gi.read_text(encoding="utf-8", errors="replace")
        if begin in text and end in text:
            head, rest = text.split(begin, 1)
            _, tail = rest.split(end, 1)
            lines = (head + tail).strip("\n")
            if lines:
                body.append(lines)
        else:
            body.append(text.strip("\n"))
    block = [begin] + [r.replace("\\", "/") for r in large_rel] + [end]
    out = "\n".join([x for x in body if x] + ["\n".join(block)]).strip() + "\n"
    gi.write_text(out, encoding="utf-8")


def ensure_inside(path: Path, rel: str) -> Path:
    target = (path / rel).resolve()
    if target != path and path not in target.parents:
        raise ApiError("路径不在项目目录内")
    return target


def project_summary(item: dict) -> dict:
    commits, head, branch = 0, "", ""
    try:
        path = project_dir(item)
        commits = int(run_git(path, "rev-list", "--count", "HEAD").strip() or 0)
        head = run_git(path, "rev-parse", "HEAD").strip()
        branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except ApiError:
        pass
    return {**item, "commits": commits, "head": head, "branch": branch}



def api_create(payload: dict) -> dict:
    raw = str(payload.get("path") or "").strip()
    name = str(payload.get("name") or "").strip() or Path(raw).name
    if not raw:
        raise ApiError("没有指定文件夹")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ApiError("请填绝对路径，例如 D:/我的项目/论文")
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ApiError("这个路径不是文件夹")
    leftovers = [p.name for p in path.iterdir() if p.name != ".git"]
    if leftovers:
        raise ApiError("文件夹不是空的，请选一个空文件夹（已有：" + "、".join(leftovers[:3]) + "）")

    ensure_repo(path)
    write_graph(path, {"trunk": {"id": "t1", "title": name}, "nodes": [], "staged": []})
    readme = path / "PROJECT.md"
    if not readme.exists():
        readme.write_text("# " + name + "\n\n由 Makise Toolkit 管理。\n"
                          "目录 .mt/ 里是结构数据，其余是正文与素材。\n", encoding="utf-8")
    commit(path, "mt: 创建项目 " + name)

    items = load_registry()
    for existing in items:
        if Path(existing["path"]).resolve() == path:
            return project_summary(existing)
    item = {"id": "p" + str(abs(hash(str(path))) % 100000), "name": name, "path": str(path)}
    items.insert(0, item)
    save_registry(items)
    return project_summary(item)


def api_project_rename(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError("名字不能为空")
    path = project_dir(item)
    graph = locked_read_graph(path)
    if graph.get("trunk"):
        graph["trunk"]["title"] = name
        write_graph(path, graph)
    items = load_registry()
    for entry in items:
        if entry.get("id") == item["id"]:
            entry["name"] = name
    save_registry(items)
    return {"name": name}


def api_project_delete(payload: dict) -> dict:
    """从列表移除。delete_folder 才真的删目录。

    顺序很重要：先把目录删掉，成功了才注销。
    反过来的话，目录删不掉时用户会看到项目凭空消失、文件夹却还在磁盘上。
    """
    import shutil as _shutil
    import time as _time

    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    removed = False

    if payload.get("delete_folder"):
        with WATCH_LOCK:
            WATCHERS.pop(item["id"], None)
        # 巡检线程每 2 秒会扫一遍目录，扫描期间 Windows 不允许删目录
        # （"另一个程序正在使用此文件"）。删之前先让它停下来。
        pause_watcher()
        items = [e for e in load_registry() if e.get("id") != item["id"]]
        save_registry(items)
        try:
            import gc as _gc
            last = None
            for _ in range(20):
                git_compat.forget(path)      # 关掉快照库连接
                _gc.collect()                # 让 SQLite 真正释放 WAL 句柄
                try:
                    _shutil.rmtree(path)
                    removed = True
                    last = None
                    break
                except OSError as exc:
                    last = exc
                    _time.sleep(0.25)
            if last is not None:
                raise ApiError("目录删不掉（可能里面还有文件开着）：" + str(last)[:80]) from None
        except ApiError:
            save_registry(load_registry() + [item])     # 失败就把登记放回去
            raise
        finally:
            resume_watcher()
        return {"removed": True}

    items = [e for e in load_registry() if e.get("id") != item["id"]]
    save_registry(items)
    with WATCH_LOCK:
        WATCHERS.pop(item["id"], None)
    return {"removed": False}


def api_list(_payload: dict) -> dict:
    return {"projects": [project_summary(i) for i in load_registry()]}


def api_load_graph(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    gp = graph_path(path)
    if not gp.exists():
        return {"graph": {}, "empty": True}
    try:
        graph = json.loads(gp.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ApiError("结构文件损坏：" + str(exc)[:80]) from None
    return {"graph": graph, "empty": False, "project": project_summary(item)}


def api_save_graph(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    graph = payload.get("graph") or {}
    if not isinstance(graph, dict):
        raise ApiError("graph 格式不对")
    write_graph(path, graph)
    return {"ok": True, "dirty": has_changes(path)}


def api_commit(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    if payload.get("graph"):
        write_graph(path, payload["graph"])
    result = commit(path, str(payload.get("message") or "mt: 保存"))
    return {**result, "project": project_summary(item)}


def api_status(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    raw = run_git(path, "status", "--porcelain")
    changed = [line[3:].strip() for line in raw.splitlines() if line.strip()]
    # 有哪些文件正被别的程序独占打开 —— 这些是提交不了的，得如实告诉用户
    locked = []
    for rel in changed:
        try:
            target = path / rel
        except Exception:
            continue
        try:
            if target.is_file() and _holders(target):
                locked.append(rel)
        except Exception:
            continue
    pw = None
    try:
        pw = watcher_for(item).status()
    except Exception:
        pw = None
    return {"dirty": bool(changed), "changed": changed[:50],
            "locked": locked, "pending": (pw or {}).get("pending", []),
            "merging": merge_in_progress(path),
            "project": project_summary(item)}


def api_log(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    limit = str(int(payload.get("limit") or 50))
    raw = run_git(path, "log", "--pretty=format:%x01%H\x1f%h\x1f%an\x1f%at\x1f%ad\x1f%s",
                  "--date=format:%m-%d %H:%M", "--name-only", "-n", limit)
    commits = []
    current = None
    for line in raw.splitlines():
        if line.startswith("\x01"):
            if current:
                commits.append(current)
            p = line[1:].split("\x1f")
            if len(p) == 6:
                current = {"sha": p[0], "short": p[1], "author": p[2],
                           "ts": int(p[3] or 0), "date": p[4], "subject": p[5],
                           "files": []}
        elif line.strip() and current is not None:
            current["files"].append(line.strip())
    if current:
        commits.append(current)
    return {"commits": commits}


def api_restore(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    sha = str(payload.get("commit") or "HEAD~1")
    files = payload.get("files") or []
    if not USE_REAL_GIT:
        # 快照库能告诉我们哪些文件因为太大没存内容、回滚时被跳过
        repo = git_compat.repo_for(path)
        try:
            real_sha = git_compat.resolve_rev(repo, sha)
            result = repo.restore(real_sha, files or None)
        except (RepoError, git_compat.GitCompatError) as exc:
            raise ApiError(str(exc)) from None
        _reload_after_restore(item)
        return {"ok": True, "restored": result.get("restored") or [],
                "skipped": result.get("skipped") or []}

    if files:
        safe = []
        for rel in files:
            target = ensure_inside(path, str(rel))
            safe.append(str(target.relative_to(path)).replace("\\", "/"))
        run_git(path, "checkout", sha, "--", *safe)
        return {"ok": True, "restored": safe, "skipped": []}
    run_git(path, "checkout", sha, "--", ".")
    return {"ok": True, "restored": "全部文件", "skipped": []}


def _reload_after_restore(item: dict) -> None:
    """回滚会改动磁盘文件，监听器必须重新认识当前状态，否则会把差异当成新改动再提交一次。"""
    try:
        pw = watcher_for(item)
        pw.snapshot()
        pw.pending.clear()
    except Exception:
        pass


def api_head(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    head = run_git(path, "rev-parse", "HEAD").strip()
    return {"head": head, "known": item.get("known_head", "")}


def api_remember(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    head = run_git(path, "rev-parse", "HEAD").strip()
    items = load_registry()
    for entry in items:
        if entry.get("id") == item["id"]:
            entry["known_head"] = head
    save_registry(items)
    return {"ok": True, "head": head}


def track_large(item: dict, pw: "ProjectWatch") -> list:
    """扫描大文件：变化了就更新 .mt/large.json（这个文件进 git）。

    大文件本身被写进 .gitignore，所以 git 里只会留下「某文件在某时刻变成多大」
    这样一条记录，仓库不会被撑爆。
    """
    path = project_dir(item)
    manifest = read_large(path)
    large_rel = []
    changed_any = False
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(path)
        if any(part in (".git", ".mt") for part in rel.parts):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size <= LARGE_LIMIT:
            continue
        key = str(rel).replace("\\", "/")
        large_rel.append(key)
        entry = manifest.get(key) or {}
        if entry.get("size") != st.st_size or int(entry.get("mtime", 0)) != int(st.st_mtime):
            manifest[key] = {
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "sha256": entry.get("sha256", ""),
                "note": "超过 %d MB，内容不进 git" % (LARGE_LIMIT // 1048576),
            }
            changed_any = True
    removed = [k for k in manifest if k not in large_rel]
    for k in removed:
        del manifest[k]
    if changed_any or removed:
        write_large(path, manifest)
        update_gitignore(path, sorted(large_rel))
        if has_changes(path):
            run_git(path, "add", "--", MT_DIR + "/large.json", ".gitignore")
    return large_rel


def sync_summaries(item: dict, pw: ProjectWatch, changed) -> None:
    """把改动过的正文文件同步成节点的摘要。

    只在项目开着「摘要自动同步」时做；关闭后用户点「同步摘要」才更新。
    """
    path = project_dir(item)
    cfg = read_settings(path)
    if not cfg.get("auto_summary", True):
        return
    with GRAPH_LOCK:
        graph = locked_read_graph(path)
    nodes = graph.get("nodes") or []
    if not nodes:
        return
    by_file = {}
    for node in nodes:
        rel = str(node.get("file") or "")
        if rel:
            by_file[rel] = node
    changed_norm = {str(c).replace("\\", "/") for c in changed}
    touched = False
    for rel, node in by_file.items():
        if rel not in changed_norm:
            continue
        target = path / rel
        if not target.exists():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new_summary = summarize(text)
        if node.get("summary") != new_summary:
            node["summary"] = new_summary
            touched = True
    if touched:
        write_graph(path, graph)


def merge_in_progress(path: Path) -> bool:
    """合并/变基/拣选进行中：工作区里有冲突标记，绝不能提交。"""
    git_dir = path / ".git"
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if (git_dir / marker).exists():
            return True
    return False


def has_conflict_markers(path: Path, rels: list) -> list:
    """兜底：即便没有 MERGE_HEAD，也不提交带冲突标记的文本文件。"""
    bad = []
    for rel in rels:
        target = path / rel
        try:
            if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<<<<<<< " in text and ">>>>>>> " in text:
            bad.append(str(rel))
    return bad

def commit_pending(item: dict, pw: ProjectWatch) -> None:
    """把待提交的文件逐个确认「没人占用」后提交一次。"""
    path = project_dir(item)
    if merge_in_progress(path):
        pw.paused = True
        pw.pause_reason = "正在合并，先把冲突处理完"
        return
    names = []
    for rel in sorted(pw.pending):
        target = path / rel
        if not target.exists():
            continue
        if _holders(target) and (time.time() - pw.first_change) < MAX_WAIT_LOCK:
            return           # 还有人开着，下一轮再试
        names.append(str(rel).replace("\\", "/"))
    if not names:
        pw.pending.clear()
        return
    bad = has_conflict_markers(path, names)
    if bad:
        pw.paused = True
        pw.pause_reason = "有文件还留着冲突标记：" + bad[0]
        return
    graph_rel = MT_DIR + "/graph.json"
    if (path / MT_DIR / "graph.json").exists():
        names = list(dict.fromkeys(names + [graph_rel]))
    try:
        run_git(path, "add", "--", *names)
        msg = "mt: 自动保存\n\n" + "\n".join("  " + n for n in names[:30])
        run_git(path, "commit", "-m", msg)
        touch_known_head(path)
        pw.pending.clear()
        pw.commits += 1
        pw.last_commit = time.time()
        pw.last_error = ""
    except ApiError as exc:
        pw.last_error = str(exc)


def watch_loop(stop_event: threading.Event) -> None:
    """每 2 秒扫一遍所有已登记项目。"""
    while not stop_event.is_set():
        try:
            for item in load_registry():
                try:
                    path = project_dir(item)
                except ApiError:
                    continue
                pw = watcher_for(item)
                if pw.path != path:
                    with WATCH_LOCK:
                        pw = ProjectWatch(path)
                        WATCHERS[item["id"]] = pw

                # 分支变了就先暂停，避免提交到用户没预期的分支。
                # 注意：项目还没有任何版本时 rev-parse HEAD 会失败，
                # 这里绝不能 continue —— 否则永远扫不到改动、也就永远建不出第一个版本。
                try:
                    head = run_git(path, "rev-parse", "HEAD").strip()
                except ApiError:
                    head = ""
                try:
                    branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
                except ApiError:
                    branch = "main"
                if pw.last_branch and branch != pw.last_branch and not pw.paused:
                    pw.paused = True
                    pw.pause_reason = "分支从 " + pw.last_branch + " 切到了 " + branch
                pw.last_head, pw.last_branch = head, branch

                merging_now = merge_in_progress(path)
                if merging_now and not pw.paused:
                    pw.paused = True
                    pw.pause_reason = '正在合并，先把冲突处理完'
                elif not merging_now and pw.paused and pw.pause_reason.startswith('正在合并'):
                    pw.paused = False
                    pw.pause_reason = ''
                try:
                    track_large(item, pw)
                except Exception as exc:
                    pw.last_error = "大文件登记失败：" + str(exc)[:100]
                try:
                    changed = pw.scan()
                except Exception as exc:
                    pw.last_error = type(exc).__name__ + ": " + str(exc)[:120]
                    continue
                if changed:
                    pw.pending.update(changed)
                    pw.last_change = time.time()
                    if not pw.first_change:
                        pw.first_change = pw.last_change

                if changed:
                    try:
                        sync_summaries(item, pw, changed)
                    except Exception as exc:
                        pw.last_error = "摘要同步失败：" + str(exc)[:100]

                if pw.pending and not pw.paused:
                    if time.time() - pw.last_change >= QUIET_SECONDS:
                        try:
                            commit_pending(item, pw)
                        except Exception as exc:
                            # 不能静默吞掉：否则界面上永远"等待中"，看不出发生了什么
                            pw.last_error = type(exc).__name__ + ": " + str(exc)[:120]
                        pw.first_change = 0.0
        except Exception as exc:
            # 兜底也不能静默：让状态接口能看到监听线程自己的故障
            global WATCH_ERROR
            WATCH_ERROR = type(exc).__name__ + ": " + str(exc)[:160]
        stop_event.wait(2.0)


def api_watch_status(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    st = watcher_for(item).status()
    if WATCH_ERROR and not st.get("last_error"):
        st["last_error"] = "监听线程：" + WATCH_ERROR
    return {"watch": st}


def api_watch_pause(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    pw = watcher_for(item)
    want = payload.get("paused")
    if want is None:
        pw.paused = not pw.paused
    else:
        pw.paused = bool(want)
    pw.pause_reason = "" if not pw.paused else str(payload.get("reason") or "手动暂停")
    return {"watch": pw.status()}


def api_commit_now(payload: dict) -> dict:
    """立即提交：把所有改动都提交（含 .mt 下的结构文件）。

    注意 .mt/ 在监听的忽略列表里 —— 自动提交不会碰它，
    但「立即提交」必须带上，否则结构改动会一直躺在工作区，
    后面删除节点时就无法单独回滚到删除之前。
    """
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    raw = run_git(path, "status", "--porcelain")
    changed = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        rel = line[3:].strip().strip('"')
        if rel.startswith(".git/"):
            continue
        changed.append(rel)
    if not changed:
        return {"committed": False, "reason": "没有改动"}
    if merge_in_progress(path):
        raise ApiError("正在合并，先把冲突处理完再提交")
    bad = has_conflict_markers(path, changed)
    if bad:
        raise ApiError("这些文件还有冲突标记，不能用提交糊弄过去：" + "、".join(bad[:3]))

    busy = [r for r in changed if _holders(path / r)]
    if busy:
        raise ApiError("这些文件还开着：" + "、".join(busy[:3]))

    info = commit_paths(path, str(payload.get("message") or "mt: 立即提交"), changed)
    pw = watcher_for(item)
    pw.snapshot()
    pw.pending.clear()
    return {"committed": bool(info.get("committed")), **info,
            "watch": pw.status(), "project": project_summary(item)}


def summarize(text: str, limit: int = 46) -> str:
    """正文前两行压成一句做摘要，顺手去掉 Markdown 标记（#、*、-、> 等）。"""
    import re as _re
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _re.sub(r"^#{1,6}\s*", "", line)          # 标题符号
        line = _re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)  # 列表符号
        line = _re.sub(r"^>\s*", "", line)               # 引用
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = _re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)   # 链接/图片
        line = line.strip()
        if line:
            lines.append(line)
        if len(lines) >= 2:
            break
    return " ".join(lines)[:limit]


def api_node_write(payload: dict) -> dict:
    """把节点正文写到项目里的 .md 文件。节点没有文件就先分配一个。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    node = payload.get("node") or {}
    if not isinstance(node, dict):
        raise ApiError("node 格式不对")
    body = str(payload.get("body") or "")

    rel = str(node.get("file") or "").strip()
    if not rel:
        rel = unique_path(path, safe_filename(str(node.get("title") or "未命名")))
    target = ensure_inside(path, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 按字节写：用 write_text 会被 Windows 的换行转换改成 CRLF，
    # 导致"同样的内容"和编辑器里看到的不一致，历史里也会误判成改动。
    target.write_bytes(body.encode("utf-8"))
    # 保存完立刻更新这一篇的检索索引（问 AI 才能问到现在写的内容）
    try:
        from . import ai as _ai
        _ai.index_path(str(target), str(item.get("name") or path.name), str(node.get("title") or target.stem))
    except Exception:
        pass
    return {"file": str(target.relative_to(path)).replace("\\", "/"),
            "summary": summarize(body)}


def api_node_read(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("file") or "").strip()
    if not rel:
        raise ApiError("没有指定文件")
    target = ensure_inside(path, rel)
    if not target.exists():
        return {"file": rel, "body": "", "missing": True}
    return {"file": rel, "body": target.read_text(encoding="utf-8", errors="replace")}


def api_settings(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    if payload.get("patch"):
        return {"settings": write_settings(path, payload["patch"])}
    return {"settings": read_settings(path)}


def api_branches(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    raw = run_git(path, "branch", "--format=%(refname:short)")
    current = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return {"branches": [b.strip() for b in raw.splitlines() if b.strip()], "current": current}


def api_checkout(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    branch = str(payload.get("branch") or "").strip()
    if not branch:
        raise ApiError("没有指定分支")
    exists = branch in [b.strip() for b in
                        run_git(path, "branch", "--format=%(refname:short)").splitlines()]
    if exists:
        run_git(path, "checkout", branch)
    else:
        run_git(path, "checkout", "-b", branch)
    # 切分支后监听器要重新认识文件，否则会把整体差异当成"改动"
    pw = watcher_for(item)
    pw.snapshot()
    pw.pending.clear()
    pw.paused = False
    pw.pause_reason = ""
    pw.last_branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return {"current": pw.last_branch}


def api_merge(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    branch = str(payload.get("branch") or "").strip()
    if not branch:
        raise ApiError("没有指定要合并的分支")

    if not USE_REAL_GIT:
        # 快照库自己做三方合并
        repo = git_compat.repo_for(path)
        try:
            result = repo.merge_from(branch)
        except RepoError as exc:
            raise ApiError(str(exc)) from None
        _reload_after_restore(item)
        return result

    try:
        run_git(path, "merge", "--no-edit", branch)
        return {"merged": True, "conflicts": []}
    except ApiError as exc:
        raw = run_git(path, "status", "--porcelain") if True else ""
        conflicts = []
        for line in raw.splitlines():
            code, _, rest = line[:2], "", line[3:]
            if "U" in code or code in ("AA", "DD"):
                conflicts.append({"path": rest.strip(), "status": code})
        if not conflicts:
            raise ApiError(str(exc)) from None
        return {"merged": False, "conflicts": conflicts, "message": "有冲突，需要先处理"}


def api_resolve(payload: dict) -> dict:
    """用「我的」或「对方的」版本解决单个冲突文件。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("file") or "").strip()
    side = str(payload.get("side") or "ours")
    if not rel:
        raise ApiError("没有指定文件")
    target = ensure_inside(path, rel)
    flag = "--ours" if side == "ours" else "--theirs"
    run_git(path, "checkout", flag, "--", str(target.relative_to(path)).replace("\\", "/"))
    run_git(path, "add", "--", str(target.relative_to(path)).replace("\\", "/"))
    remaining = [ln[3:] for ln in run_git(path, "status", "--porcelain").splitlines()
                 if "U" in ln[:2] or ln[:2] in ("AA", "DD")]
    return {"resolved": rel, "remaining": remaining}


def api_merge_state(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    if not USE_REAL_GIT:
        # 快照模式：冲突在合并时一次性报出来，不存在"合到一半卡住"的状态
        return {"merging": False, "conflicts": [], "snapshot": True}
    raw = run_git(path, "status", "--porcelain")
    conflicts = [ln[3:].strip() for ln in raw.splitlines()
                 if "U" in ln[:2] or ln[:2] in ("AA", "DD")]
    merging = (path / ".git" / "MERGE_HEAD").exists()
    return {"merging": merging, "conflicts": conflicts}


def api_merge_abort(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    if not USE_REAL_GIT:
        return {"aborted": False, "message": "快照模式不会卡在合并中，无需中止"}
    run_git(path, "merge", "--abort")
    return {"aborted": True}


def collect_subtree(nodes: list, root_id: str) -> list:
    """收集要一起删掉的节点 id（含所有层级的子孙）。

    用 visited 防环：数据被外部改坏时不能死循环。
    主干本身永远不参与删除。
    """
    children = {}
    for node in nodes:
        parent = node.get("parent")
        if parent:
            children.setdefault(parent, []).append(node.get("id"))
    out, stack, visited = [], [root_id], set()
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


def api_node_delete(payload: dict) -> dict:
    """删除节点及其所有子孙。文件默认保留，显式要求才删。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    node_id = str(payload.get("nodeId") or "").strip()
    if not node_id:
        raise ApiError("没有指定节点")
    graph = locked_read_graph(path)
    trunk_id = (graph.get("trunk") or {}).get("id")
    if node_id == trunk_id:
        raise ApiError("主干不能删除")
    nodes = graph.get("nodes") or []
    if not any(n.get("id") == node_id for n in nodes):
        raise ApiError("找不到这个节点")

    doomed = set(collect_subtree(nodes, node_id))
    doomed.discard(trunk_id)
    files = [n.get("file") for n in nodes if n.get("id") in doomed and n.get("file")]
    asset_files = []
    for n in nodes:
        if n.get("id") in doomed:
            for a in (n.get("assets") or []):
                if a.get("path"):
                    asset_files.append(a["path"])
    graph["nodes"] = [n for n in nodes if n.get("id") not in doomed]
    # 关联线也要清理，否则会留下指向已删节点的悬空引用
    if graph.get("links"):
        before_links = len(graph["links"])
        graph["links"] = [l for l in graph["links"]
                          if l.get("from") not in doomed and l.get("to") not in doomed]
        dropped_links = before_links - len(graph["links"])
    else:
        dropped_links = 0
    write_graph(path, graph)

    removed_files, failed_files = [], []
    orphans = []
    if payload.get("delete_files"):
        for rel in list(files) + list(asset_files):
            try:
                target = ensure_inside(path, rel)
            except ApiError:
                continue
            if not target.is_file():
                continue
            try:
                target.unlink()
                removed_files.append(rel)
            except OSError as exc:
                # 文件正被编辑器占用时删不掉：不能静默，要报给界面
                failed_files.append({"path": rel, "reason": str(exc)[:80]})
    else:
        orphans = list(asset_files)

    # 收尾：把因此变空的 assets/<节点名> 目录删掉，别留一堆空壳
    for n in nodes:
        if n.get("id") not in doomed:
            continue
        folder_name = safe_filename(str(n.get("title") or "未命名"))
        folder = path / "assets" / folder_name
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass

    # 立刻提交：否则这次删除只躺在工作区里，用历史面板根本撤不回来
    touched = [MT_DIR + "/graph.json"] + list(files) + list(asset_files)
    try:
        node_title = payload.get("title")
        if not node_title:
            node_title = next((n.get("title") for n in nodes if n.get("id") == node_id), node_id)
        info = commit_paths(path, "mt: 删除节点「" + str(node_title)
                            + "」（" + str(len(doomed)) + " 个节点）", touched)
    except ApiError as exc:
        info = {"committed": False, "error": str(exc)[:100]}

    return {"removed": len(doomed), "dropped_links": dropped_links,
            "kept_files": len(files) + len(asset_files) - len(removed_files),
            "removed_files": removed_files, "failed_files": failed_files,
            "orphan_assets": orphans, "commit": info, "graph": graph}


def api_node_merge(payload: dict) -> dict:
    """把「本节点 + 所有子孙」并进父节点，变成父节点的挂件。

    前提（任何一条不满足都拒绝，避免把结构搞乱）：
      · 节点不是主干
      · 有父节点
      · 本节点有对应的 .md 文件
      · 所有子孙也都有对应的 .md 文件
    并进去之后：这些文件挂到父节点的 assets 上，节点本身从画布移除。
    """
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    node_id = str(payload.get("nodeId") or "").strip()
    graph = locked_read_graph(path)
    trunk_id = (graph.get("trunk") or {}).get("id")
    nodes = graph.get("nodes") or []
    node = next((n for n in nodes if n.get("id") == node_id), None)
    if node is None:
        raise ApiError("找不到这个节点")
    if node_id == trunk_id:
        raise ApiError("主干不能并入别的节点")
    parent_id = node.get("parent")
    if not parent_id:
        raise ApiError("这个节点没有父节点，无法并入")
    parent = next((n for n in nodes if n.get("id") == parent_id), None)
    if parent is None:
        raise ApiError("找不到父节点")

    doomed = set(collect_subtree(nodes, node_id))
    doomed.discard(trunk_id)
    members = [n for n in nodes if n.get("id") in doomed]
    # 有正文文件 或 已经挂了文件 都算"有内容"，否则并进去没意义
    missing = [n.get("title") for n in members
               if not n.get("file") and not (n.get("assets") or [])]
    if missing:
        raise ApiError("这些节点既没有正文也没有挂文件，不能作为挂件：" + "、".join(missing[:3]))

    assets = parent.get("assets") or []
    moved = 0
    for n in members:
        # 正文文件
        rel = str(n.get("file") or "")
        if rel:
            target = ensure_inside(path, rel)
            if target.is_file():
                assets.append({
                    "name": target.name,
                    "path": rel.replace("\\", "/"),
                    "size": target.stat().st_size,
                    "source": "",
                })
                moved += 1
        # 这个节点上已经挂着的文件
        for a in (n.get("assets") or []):
            if a.get("path"):
                assets.append(a)
                moved += 1
    parent["assets"] = assets

    graph["nodes"] = [n for n in nodes if n.get("id") not in doomed]
    if graph.get("links"):
        graph["links"] = [l for l in graph["links"]
                          if l.get("from") not in doomed and l.get("to") not in doomed]
    write_graph(path, graph)

    touched = [MT_DIR + "/graph.json"]
    try:
        info = commit_paths(path, "mt: 把 " + str(node.get("title") or node_id)
                            + " 等 " + str(moved) + " 个节点并入「" + str(parent.get("title") or "") + "」",
                            touched)
    except ApiError as exc:
        info = {"committed": False, "error": str(exc)[:100]}
    return {"merged": moved, "parent": parent.get("id"), "commit": info, "graph": graph}


def api_staged_remove(payload: dict) -> dict:
    """暂存区里的条目删除（只动结构，不碰磁盘）。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    staged_id = str(payload.get("stagedId") or "").strip()
    graph = locked_read_graph(path)
    before = len(graph.get("staged") or [])
    graph["staged"] = [s for s in (graph.get("staged") or []) if s.get("id") != staged_id]
    write_graph(path, graph)
    return {"removed": before - len(graph["staged"]), "graph": graph}


def api_branch_delete(payload: dict) -> dict:
    """删除一个分支。当前分支、以及还没合并的分支都要拦住。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    branch = str(payload.get("branch") or "").strip()
    if not branch:
        raise ApiError("没有指定分支")
    current = run_git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == current:
        raise ApiError("不能删除当前所在的分支，先切到别的分支")
    exists = branch in [b.strip() for b in
                        run_git(path, "branch", "--format=%(refname:short)").splitlines()]
    if not exists:
        raise ApiError("没有这个分支")
    merged = True
    try:
        merged = not bool(run_git(path, "branch", "--no-merged", current).splitlines())
    except ApiError:
        merged = False
    if not merged and not payload.get("force"):
        raise ApiError("这个分支还有没合并的提交，确认要删请勾选强制删除")
    run_git(path, "branch", "-D" if payload.get("force") else "-d", branch)
    return {"deleted": branch}


PICK_STATE = {"running": False, "picked": None, "error": None}


def _run_picker(script: str) -> None:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, creationflags=0x08000000,
        )
        PICK_STATE["picked"] = (proc.stdout or "").strip()
    except Exception as exc:
        PICK_STATE["error"] = str(exc)[:100]
    finally:
        PICK_STATE["running"] = False


def api_pick_file(payload: dict) -> dict:
    """启动文件选择框（后台线程），立刻返回。前端轮询 /pick-file/poll 拿结果。"""
    if os.name != "nt":
        raise ApiError("原型只在 Windows 上实现了文件选择框")
    if PICK_STATE["running"]:
        return {"running": True}
    PICK_STATE.update({"running": True, "picked": None, "error": None})
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner = New-Object System.Windows.Forms.Form;"
        "$owner.TopMost = $true; $owner.ShowInTaskbar = $false;"
        "$owner.Width = 1; $owner.Height = 1; $owner.StartPosition = 'CenterScreen';"
        "$f = New-Object System.Windows.Forms.OpenFileDialog;"
        "$f.Title = '选择要挂到项目的文件';"
        "$f.Filter = '所有文件 (*.*)|*.*';"
        "$f.Multiselect = $false;"
        "if ($f.ShowDialog($owner) -eq 'OK') { Write-Output $f.FileName };"
        "$owner.Dispose()"
    )
    threading.Thread(target=_run_picker, args=(script,), daemon=True).start()
    return {"running": True}


def api_pick_poll(_payload: dict) -> dict:
    return {"running": PICK_STATE["running"],
            "picked": PICK_STATE["picked"] or "",
            "error": PICK_STATE["error"] or ""}


def api_pick_legacy(payload: dict) -> dict:
    """弹系统文件选择框，返回选中的路径。

    原型跑在本机，直接借 PowerShell 的 WinForms 对话框。
    HTTP 请求会等对话框关闭（最多 3 分钟），前端显示"等待选择"即可。
    """
    if os.name != "nt":
        raise ApiError("原型只在 Windows 上实现了文件选择框")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner = New-Object System.Windows.Forms.Form;"
        "$owner.TopMost = $true; $owner.ShowInTaskbar = $false;"
        "$owner.Width = 1; $owner.Height = 1; $owner.StartPosition = 'CenterScreen';"
        "$f = New-Object System.Windows.Forms.OpenFileDialog;"
        "$f.Title = '选择要挂到项目的文件';"
        "$f.Filter = '所有文件 (*.*)|*.*';"
        "$f.Multiselect = $false;"
        "if ($f.ShowDialog($owner) -eq 'OK') { Write-Output $f.FileName };"
        "$owner.Dispose()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, creationflags=0x08000000,   # CREATE_NO_WINDOW
        )
    except subprocess.TimeoutExpired:
        raise ApiError("等了 3 分钟没选择，已取消") from None
    picked = (proc.stdout or "").strip()
    if not picked:
        return {"picked": ""}
    return {"picked": picked}


def api_staged_add_file(payload: dict) -> dict:
    """把选中的文件复制进项目的 files/ 目录，返回项目内相对路径。

    暂存区里存的是"已经进项目"的文件，拖到画布上时不用再复制一次。
    """
    import shutil as _shutil
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    source = str(payload.get("source") or "").strip()
    if not source:
        raise ApiError("没有选择文件")
    src = Path(source).expanduser()
    if not src.is_file():
        raise ApiError("找不到这个文件：" + str(src))

    folder = path / "files"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    n = 2
    while dest.exists():
        dest = folder / (src.stem + "-" + str(n) + src.suffix)
        n += 1
    try:
        _shutil.copy2(str(src), str(dest))
    except OSError as exc:
        raise ApiError("复制失败：" + str(exc)[:80]) from None

    rel = str(dest.relative_to(path)).replace("\\", "/")
    return {"name": dest.name, "path": rel, "size": dest.stat().st_size}


def api_staged_attach(payload: dict) -> dict:
    """把已经在项目里的文件挂到某个节点上（暂存区拖进来时用）。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    node_id = str(payload.get("nodeId") or "").strip()
    rel = str(payload.get("path") or "").strip()
    graph = locked_read_graph(path)
    pool = list(graph.get("nodes") or [])
    if graph.get("trunk"):
        pool.append(graph["trunk"])       # 主干不在 nodes 列表里，但它也能挂文件
    node = next((n for n in pool if n.get("id") == node_id), None)
    if node is None:
        raise ApiError("找不到这个节点")
    target = ensure_inside(path, rel)
    if not target.is_file():
        raise ApiError("文件不在项目里了")
    assets = node.get("assets") or []
    assets.append({"name": target.name, "path": rel.replace("\\", "/"),
                   "size": target.stat().st_size, "source": ""})
    node["assets"] = assets
    write_graph(path, graph)
    return {"node": node, "graph": graph}


def api_attach(payload: dict) -> dict:
    """把外部文件复制一份到项目里（镜像），源文件不动。

    之后所有编辑都针对镜像；大文件会被 .gitignore 规则自动排除。
    """
    import shutil as _shutil
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    node_id = str(payload.get("nodeId") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not source:
        raise ApiError("没有指定要挂的文件")
    src = Path(source).expanduser()
    if not src.is_file():
        raise ApiError("找不到这个文件：" + str(src))

    graph = locked_read_graph(path)
    pool = list(graph.get("nodes") or [])
    if graph.get("trunk"):
        pool.append(graph["trunk"])     # 主干也能挂文件
    node = next((n for n in pool if n.get("id") == node_id), None)
    if node is None:
        raise ApiError("找不到这个节点")

    folder = safe_filename(str(node.get("title") or "未命名"))
    assets_dir = path / "assets" / folder
    assets_dir.mkdir(parents=True, exist_ok=True)
    name = src.name
    dest = assets_dir / name
    n = 2
    while dest.exists():
        dest = assets_dir / (src.stem + "-" + str(n) + src.suffix)
        n += 1
    try:
        _shutil.copy2(str(src), str(dest))
    except OSError as exc:
        raise ApiError("复制失败：" + str(exc)[:100]) from None

    rel = str(dest.relative_to(path)).replace("\\", "/")
    assets = node.get("assets") or []
    assets.append({"name": dest.name, "path": rel,
                   "size": dest.stat().st_size, "source": str(src)})
    node["assets"] = assets
    write_graph(path, graph)
    return {"asset": assets[-1], "node": node, "graph": graph}


def api_asset_remove(payload: dict) -> dict:
    """移除挂载：默认只去掉记录，delete_file 才删镜像文件。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    node_id = str(payload.get("nodeId") or "").strip()
    rel = str(payload.get("path") or "").strip()
    graph = locked_read_graph(path)
    pool = list(graph.get("nodes") or [])
    if graph.get("trunk"):
        pool.append(graph["trunk"])
    node = next((n for n in pool if n.get("id") == node_id), None)
    if node is None:
        raise ApiError("找不到这个节点")
    node["assets"] = [a for a in (node.get("assets") or []) if a.get("path") != rel]
    removed = False
    if payload.get("delete_file"):
        try:
            target = ensure_inside(path, rel)
            if target.is_file():
                target.unlink()
                removed = True
        except ApiError:
            pass
    write_graph(path, graph)
    return {"removed_file": removed, "node": node, "graph": graph}


TEXT_SUFFIX = (".md", ".txt", ".json", ".csv", ".yml", ".yaml", ".py", ".js", ".css", ".html")


def api_asset_read(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("path") or "").strip()
    target = ensure_inside(path, rel)
    if not target.is_file():
        raise ApiError("文件不存在")
    if target.suffix.lower() not in TEXT_SUFFIX:
        raise ApiError("这种文件不能在应用内编辑，用「打开」交给系统程序")
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ApiError("文件太大（超过 2MB），用系统程序打开更合适")
    return {"path": rel, "text": target.read_text(encoding="utf-8", errors="replace")}


def api_asset_write(payload: dict) -> dict:
    """写入项目内的文本文件。文件被删掉了也能重建（撤销要用）。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("path") or "").strip()
    if not rel:
        raise ApiError("没有指定文件")
    target = ensure_inside(path, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(str(payload.get("text") or "").encode("utf-8"))
    return {"path": rel, "saved": True, "created": True}


def api_open_file(payload: dict) -> dict:
    """用系统默认程序打开项目里的文件 —— 方便验证「外部编辑」流程。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("path") or "").strip()
    target = ensure_inside(path, rel)
    if not target.is_file():
        raise ApiError("文件不存在")
    if os.name != "nt":
        raise ApiError("原型只在 Windows 上实现了打开文件")
    os.startfile(str(target))          # noqa: S606 - 原型，路径已校验在项目内
    return {"opened": str(target)}


def api_hash(payload: dict) -> dict:
    """按需算某个文件的 SHA-256（大文件平时只记 size+mtime，这个用来验内容）。"""
    import hashlib as _hashlib
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("path") or "").strip()
    if not rel:
        raise ApiError("没有指定文件")
    target = ensure_inside(path, rel)
    if not target.is_file():
        raise ApiError("文件不存在")
    h = _hashlib.sha256()
    size = 0
    with target.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    digest = h.hexdigest()
    # 顺手写回大文件清单，之后判断变化更快
    manifest = read_large(path)
    key = str(target.relative_to(path)).replace("\\", "/")
    if key in manifest:
        manifest[key]["sha256"] = digest
        write_large(path, manifest)
    return {"path": key, "size": size, "sha256": digest}


def api_undo(payload: dict) -> dict:
    """撤销最近一次提交（git revert，保留历史，不丢任何提交）。

    只看「这次提交动过的文件」是否干净 —— 用户可能同时在改别的文件，
    那些不该拦住撤销。
    """
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    raw = run_git(path, "show", "--name-only", "--pretty=format:", "HEAD")
    touched = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not touched:
        raise ApiError("最近一次提交没有文件变化")
    dirty = run_git(path, "status", "--porcelain", "--", *touched).strip()
    if dirty:
        raise ApiError("这些文件还有未提交的改动，先处理它们：" + dirty.splitlines()[0][3:][:60])
    try:
        run_git(path, "revert", "--no-edit", "HEAD")
    except ApiError as exc:
        raise ApiError("撤销失败：" + str(exc)[:120]) from None
    touch_known_head(path)
    pw = watcher_for(item)
    pw.snapshot()
    pw.pending.clear()
    return {"reverted": True, "graph": read_graph_file(path),
            "project": project_summary(item)}


def read_graph_file(path: Path) -> dict:
    gp = graph_path(path)
    if not gp.exists():
        return {}
    try:
        return json.loads(gp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_conflicts(text: str) -> list:
    """把带冲突标记的文本拆成「普通段」和「冲突段」。"""
    lines = text.splitlines()
    out, buf = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("<<<<<<<"):
            if buf:
                out.append({"type": "text", "lines": buf})
                buf = []
            ours, theirs, base = [], [], []
            i += 1
            # 有 common ancestor 时会出现 ||||||| 分隔
            mode = "ours"
            while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                cur = lines[i]
                if cur.startswith("|||||||"):
                    mode = "base"
                elif cur.startswith("======="):
                    mode = "theirs"
                elif mode == "ours":
                    ours.append(cur)
                elif mode == "base":
                    base.append(cur)
                else:
                    theirs.append(cur)
                i += 1
            out.append({"type": "conflict", "ours": ours, "theirs": theirs, "base": base})
            i += 1
            continue
        buf.append(line)
        i += 1
    if buf:
        out.append({"type": "text", "lines": buf})
    return out


def api_conflict_read(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("file") or "").strip()
    target = ensure_inside(path, rel)
    if not target.is_file():
        raise ApiError("文件不存在")
    text = target.read_text(encoding="utf-8", errors="replace")
    blocks = parse_conflicts(text)
    return {"file": rel, "text": text, "blocks": blocks,
            "conflicts": sum(1 for b in blocks if b["type"] == "conflict")}


def api_conflict_write(payload: dict) -> dict:
    """写入解决后的内容并 git add；resolve=True 表示标记为已解决。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    rel = str(payload.get("file") or "").strip()
    target = ensure_inside(path, rel)
    if not target.exists():
        raise ApiError("文件不存在")
    content = payload.get("content")
    if content is None:
        raise ApiError("没有内容")
    if "<<<<<<<" in str(content):
        raise ApiError("内容里还有冲突标记，先处理掉")
    target.write_text(str(content), encoding="utf-8")
    rel_posix = str(target.relative_to(path)).replace("\\", "/")
    if payload.get("resolve", True):
        run_git(path, "add", "--", rel_posix)
    raw = run_git(path, "status", "--porcelain")
    conflicts = [ln[3:].strip() for ln in raw.splitlines()
                 if "U" in ln[:2] or ln[:2] in ("AA", "DD")]
    return {"file": rel_posix, "remaining": conflicts}


def api_merge_finish(payload: dict) -> dict:
    """冲突都处理完了，收尾这次合并。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    if not (path / ".git" / "MERGE_HEAD").exists():
        raise ApiError("现在不在合并过程中")
    raw = run_git(path, "status", "--porcelain")
    left = [ln[3:].strip() for ln in raw.splitlines()
            if "U" in ln[:2] or ln[:2] in ("AA", "DD")]
    if left:
        raise ApiError("还有 " + str(len(left)) + " 个文件没解决")
    run_git(path, "commit", "--no-edit")
    touch_known_head(path)
    pw = watcher_for(item)
    pw.snapshot()
    pw.pending.clear()
    return {"merged": True, "graph": read_graph_file(path), "project": project_summary(item)}


def api_show(payload: dict) -> dict:
    """某次提交改了哪些文件（供历史面板展开）。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    sha = str(payload.get("sha") or "HEAD")
    raw = run_git(path, "show", "--name-status", "--pretty=format:", sha)
    files = []
    for line in raw.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0][:1], "path": parts[-1]})
    return {"files": files}


def api_export_git(payload: dict) -> dict:
    """把快照历史重放成一个真正的 git 仓库（需要系统里有 git）。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    raw = str(payload.get("target") or "").strip()
    target = Path(raw).expanduser() if raw else (path.parent / (path.name + "-git"))
    if target.exists() and any(target.iterdir()):
        raise ApiError("目标文件夹不是空的：" + str(target))
    try:
        result = git_compat.repo_for(path).export_git(target)
    except RepoError as exc:
        raise ApiError(str(exc)) from None
    return result


def api_targets(_payload: dict) -> dict:
    """给「存到项目」选择器用：一次返回所有项目和它们的节点树。

    节点树按父子顺序展开，带 depth，前端直接平铺成下拉列表就行。
    """
    out = []
    for item in load_registry():
        try:
            path = project_dir(item)
            graph = locked_read_graph(path)
        except Exception:
            continue     # 目录没了 / 结构坏了就跳过这个项目，不要让整个选择器挂掉
        nodes = graph.get("nodes") or []
        trunk = graph.get("trunk") or {}
        flat = []
        if trunk:
            flat.append({"id": trunk.get("id") or "t1",
                         "title": trunk.get("title") or item.get("name") or "主干",
                         "depth": 0, "is_trunk": True})
        # 广度优先展开，保证父节点一定排在子节点前面
        children = {}
        for node in nodes:
            children.setdefault(node.get("parent") or "", []).append(node)
        queue = [(trunk.get("id") or "t1", 1)]
        seen = set()
        while queue:
            parent_id, depth = queue.pop(0)
            for child in children.get(parent_id, []):
                cid = child.get("id")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                flat.append({"id": cid, "title": child.get("title") or "未命名",
                             "depth": depth, "is_trunk": False})
                queue.append((cid, depth + 1))
        # 浮空节点（没有父节点的）也要能选
        for node in nodes:
            nid = node.get("id")
            if nid and nid not in seen:
                flat.append({"id": nid, "title": node.get("title") or "未命名",
                             "depth": 1, "is_trunk": False})
        out.append({"id": item.get("id"), "name": item.get("name") or "未命名项目",
                    "path": str(item.get("path") or ""), "nodes": flat})
    return {"projects": out}


def api_gc(payload: dict) -> dict:
    """清理没有被任何版本引用的内容块，顺便报一下前后占用。"""
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    repo = git_compat.repo_for(path)
    before = repo.stats()
    keep_recent = int(payload.get("keep_recent") or 0)
    result = repo.gc(keep_recent=keep_recent)
    after = repo.stats()
    return {"removed_versions": result["removed_versions"],
            "removed_blocks": result["removed_blocks"],
            "before": before, "after": after}


def api_history_stats(payload: dict) -> dict:
    item = find_project(str(payload.get("id") or ""))
    path = project_dir(item)
    repo = git_compat.repo_for(path)
    return {"stats": repo.stats(), "size": path_size(repo.db_path)}


def path_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


ROUTES = {
    "/api/projects": api_list,
    "/api/project/create": api_create,
    "/api/project/graph": api_save_graph,
    "/api/project/load": api_load_graph,
    "/api/project/commit": api_commit,
    "/api/project/status": api_status,
    "/api/project/log": api_log,
    "/api/project/restore": api_restore,
    "/api/project/head": api_head,
    "/api/project/remember": api_remember,
    "/api/project/watch": api_watch_status,
    "/api/project/pause": api_watch_pause,
    "/api/project/commit-now": api_commit_now,
    "/api/project/show": api_show,
    "/api/project/node/write": api_node_write,
    "/api/project/node/read": api_node_read,
    "/api/project/settings": api_settings,
    "/api/project/branches": api_branches,
    "/api/project/checkout": api_checkout,
    "/api/project/merge": api_merge,
    "/api/project/resolve": api_resolve,
    "/api/project/merge-state": api_merge_state,
    "/api/project/merge-abort": api_merge_abort,
    "/api/project/node/delete": api_node_delete,
    "/api/project/node/merge": api_node_merge,
    "/api/project/staged/remove": api_staged_remove,
    "/api/project/branch/delete": api_branch_delete,
    "/api/project/attach": api_attach,
    "/api/project/staged/add-file": api_staged_add_file,
    "/api/project/staged/attach": api_staged_attach,
    "/api/project/pick-file": api_pick_file,
    "/api/project/pick-file/poll": api_pick_poll,
    "/api/project/rename": api_project_rename,
    "/api/project/delete": api_project_delete,
    "/api/project/asset/remove": api_asset_remove,
    "/api/project/asset/read": api_asset_read,
    "/api/project/asset/write": api_asset_write,
    "/api/project/open": api_open_file,
    "/api/project/hash": api_hash,
    "/api/project/undo": api_undo,
    "/api/project/conflict/read": api_conflict_read,
    "/api/project/conflict/write": api_conflict_write,
    "/api/project/merge-finish": api_merge_finish,
    "/api/project/export-git": api_export_git,
    "/api/project/history-stats": api_history_stats,
    "/api/project/gc": api_gc,
    "/api/project/targets": api_targets,
}




# ------------------------------------------------------------------ 对外接口
_stop = threading.Event()
_watch_thread = None


def init(data_dir) -> None:
    """指定登记表位置，并启动自动提交的巡检线程。"""
    global REGISTRY
    REGISTRY = Path(data_dir) / "projects.json"
    _start_watcher()


def _start_watcher() -> None:
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return
    _stop.clear()
    _watch_thread = threading.Thread(target=watch_loop, args=(_stop,), daemon=True,
                                     name="project-watcher")
    _watch_thread.start()


def pause_watcher(timeout: float = 3.0) -> None:
    """临时停掉巡检（删项目时用：扫描期间删不掉目录）。"""
    _stop.set()
    thread = _watch_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def resume_watcher() -> None:
    _start_watcher()


def shutdown() -> None:
    _stop.set()


def dispatch(route: str, payload: dict) -> dict:
    """给 server.py 用：返回可直接 JSON 化的 dict，失败抛 ApiError。"""
    handler = ROUTES.get(route)
    if handler is None:
        raise ApiError("未知接口")
    if not isinstance(payload, dict):
        raise ApiError("请求体必须是 JSON 对象")
    return handler(payload)
