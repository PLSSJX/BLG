# -*- coding: utf-8 -*-
"""把 git 的命令行用法翻译成快照库操作。

现有的业务代码是按 git 写的（add / commit / log / checkout …），
直接重写每个接口风险太大，所以这里做一层薄薄的兼容层：
同样的调用、同样的输出格式，底层换成 .mt/history.db。

需要真 git 时（导出仓库）走 snapshots.Repo.export_git()。
"""
from __future__ import annotations

import time
from pathlib import Path

from .snapshots import Repo, RepoError

_REPOS: dict = {}


def repo_for(root: Path) -> Repo:
    key = str(Path(root).resolve())
    r = _REPOS.get(key)
    if r is None:
        r = Repo(Path(root))
        _REPOS[key] = r
    return r


class GitCompatError(Exception):
    pass


def resolve_rev(r: Repo, name: str) -> str:
    """把 HEAD / HEAD~1 / 分支名 / sha 前缀 解析成完整 sha。"""
    name = (name or "HEAD").strip()
    conn = r._conn()
    if name == "HEAD":
        head = r.head()
        if not head:
            raise GitCompatError("还没有任何版本")
        return head["sha"]
    if name.startswith("HEAD~"):
        try:
            steps = int(name[5:] or "1")
        except ValueError:
            steps = 1
        head = r.head()
        if not head:
            raise GitCompatError("还没有任何版本")
        vid = head["id"]
        for _ in range(steps):
            row = conn.execute("SELECT parent FROM versions WHERE id=?", (vid,)).fetchone()
            if not row or not row["parent"]:
                raise GitCompatError("已经是第一个版本了")
            vid = row["parent"]
        row = conn.execute("SELECT sha FROM versions WHERE id=?", (vid,)).fetchone()
        return row["sha"]
    row = conn.execute("SELECT version_id FROM branches WHERE name=?", (name,)).fetchone()
    if row and row["version_id"]:
        v = conn.execute("SELECT sha FROM versions WHERE id=?", (row["version_id"],)).fetchone()
        if v:
            return v["sha"]
    row = conn.execute("SELECT sha FROM versions WHERE sha=? OR sha LIKE ?", (name, name + "%")).fetchone()
    if row:
        return row["sha"]
    raise GitCompatError("找不到这个版本：%s" % name)


def _short(sha: str) -> str:
    return (sha or "")[:7]


def _author() -> str:
    return "Makise Toolkit"


def run(root: Path, *args) -> str:
    """模拟 git 的输出。不认识的命令一律当成空操作，避免把界面弄崩。"""
    r = repo_for(root)
    a = [str(x) for x in args]
    if not a:
        return ""
    cmd = a[0]

    # ---------------- 初始化 / 配置
    if cmd == "init":
        return "Initialized"
    if cmd in ("symbolic-ref", "config", "gc"):
        return ""

    # ---------------- 读取
    if cmd == "rev-parse":
        if len(a) >= 2 and a[1] == "--abbrev-ref":
            return r.current_branch()
        head = r.head()
        if not head:
            raise GitCompatError("还没有任何版本")
        return head["sha"]

    if cmd == "rev-list":
        return str(len(r.log(limit=100000)))

    if cmd == "status":
        paths = []
        if "--" in a:
            paths = a[a.index("--") + 1:]
        changed = r.status()
        if paths:
            changed = [p for p in changed if p in set(paths)]
        return "\n".join(" M " + p for p in changed)

    if cmd == "ls-files":
        tracked = r.head_files()
        if "--" in a:
            want = a[a.index("--") + 1:]
            return "\n".join(p for p in want if p in tracked)
        return "\n".join(sorted(tracked))

    if cmd == "diff":
        # 只用来判断"有没有暂存内容"，用工作区差异代替
        return "\n".join(r.status())

    if cmd == "log":
        limit = 100
        if "-n" in a:
            try:
                limit = int(a[a.index("-n") + 1])
            except Exception:
                pass
        lines = []
        for item in r.log(limit=limit):
            ts = item["ts"] or 0
            when = time.strftime("%m-%d %H:%M", time.localtime(ts))
            lines.append("%s%s\x1f%s\x1f%s\x1f%d\x1f%s\x1f%s" % (
                "\x01", item["sha"], _short(item["sha"]), _author(), ts, when, item["message"]))
            for f in item.get("files") or []:
                lines.append(f)
        return "\n".join(lines)

    if cmd == "show":
        if "--name-status" in a:
            sha = next((x for x in a[1:] if not x.startswith("-") and x not in ("--name-status", "--pretty=format:", "HEAD")), "HEAD")
            target = resolve_rev(r, sha)
            try:
                files = r.version_files(target)
            except RepoError:
                return ""
            return "\n".join("M\t" + p for p in sorted(files))
        return "\n".join(sorted(r.head_files()))

    # ---------------- 写入
    if cmd == "add":
        return ""      # commit 时直接扫工作区，不需要暂存区

    if cmd == "commit":
        msg = "mt: 保存"
        if "-m" in a:
            msg = a[a.index("-m") + 1]
        r.commit(msg)
        return ""

    if cmd == "revert":
        # 回到上一个版本
        head = r.head()
        if not head:
            raise GitCompatError("还没有任何版本")
        parent_id = head.get("parent")
        versions = r.log(limit=100000)
        if parent_id:
            for item in versions:
                if r.head_files() and item["sha"] != head["sha"]:
                    pass
            # 用 parent 的清单回滚
            conn = r._conn()
            row = conn.execute("SELECT sha FROM versions WHERE id=?", (parent_id,)).fetchone()
            if row:
                r.restore(row["sha"])
        return ""

    if cmd == "checkout":
        rest = [x for x in a[1:] if x != "--"]
        if "-b" in a:
            r.branch_create(rest[rest.index("-b") + 1] if "-b" in rest else rest[-1])
            return ""
        if "--" in a:
            idx = a.index("--")
            target = resolve_rev(r, a[idx - 1])
            paths = a[idx + 1:]
            if paths == ["."]:
                r.restore(target)
            else:
                r.restore(target, paths)
            return ""
        if rest:
            r.branch_switch(rest[-1])
        return ""

    if cmd == "branch":
        if "-D" in a:
            r.branch_delete(a[a.index("-D") + 1])
            return ""
        if "--no-merged" in a:
            return ""
        names, _cur = r.branch_list()
        return "\n".join(names)

    if cmd == "merge":
        if "--abort" in a:
            return ""
        raise GitCompatError("快照模式下暂不支持合分支，请改用「导出为 Git 仓库」后合并")

    # ---------------- 其它一律空操作
    return ""
