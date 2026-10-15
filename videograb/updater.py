"""自动更新与自检修复。

自检修复的思路：仓库里放一份 update/manifest.json，列出所有界面文件的
sha256。程序启动或用户手动触发时，把本地文件逐个算一遍哈希去比对，
对不上或者缺失的就从更新源重新拉一份下来覆盖。

更新源按可达性排序：jsDelivr 优先。实测在部分网络下
raw.githubusercontent.com 完全不可达，而 jsDelivr 一直可用，
所以不能只依赖 raw。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import BASE_DIR, RESOURCE_DIR

REPO = "PLSSJX/BLG"
BRANCH = "main"
MANIFEST_PATH = "update/manifest.json"

# 顺序即优先级。三个源的可用性在不同的网络环境下差别很大，实测过：
#   - jsDelivr 最稳，但仓库刚从私有转公开时会有几分钟的传播延迟（期间 404）
#   - raw.githubusercontent.com 在部分网络下完全不可达
#   - GitHub 官方 API 走的是 api.github.com，前面两个都挂时它往往还活着
# 所以三个都留着，任何一个能通就能完成自检修复。
SOURCES = [
    {"kind": "raw", "label": "jsDelivr", "base": "https://cdn.jsdelivr.net/gh/" + REPO + "@" + BRANCH + "/"},
    {"kind": "raw", "label": "raw.githubusercontent", "base": "https://raw.githubusercontent.com/" + REPO + "/" + BRANCH + "/"},
    {"kind": "api", "label": "GitHub API", "base": "https://api.github.com/repos/" + REPO + "/contents/"},
]
API_SUFFIX = "?ref=" + BRANCH

RELEASE_PAGE = "https://github.com/" + REPO + "/releases/latest"
USER_AGENT = "Video-Grabing/" + __version__ + " (+https://github.com/" + REPO + ")"


class UpdaterError(Exception):
    pass


# ---------------------------------------------------------------- 基础
def _fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        # jsDelivr 会对旧版本做缓存，这里显式要最新
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(relative: str) -> str | None:
    """校验清单里的相对路径，挡掉绝对路径和 .. 穿越。

    清单是从网上下载的，如果它被替换成恶意的，没有这道校验就可能在程序目录
    之外覆盖任意文件。
    """
    text = (relative or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ":" in text.split("/")[0]:
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def resolve_local(relative: str) -> Path | None:
    """先看 exe 同级（可写、可修复），再看打包进去的只读资源。"""
    for root in (BASE_DIR, RESOURCE_DIR):
        candidate = root / relative
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------- 清单
def fetch_from(source: dict, relative: str, timeout: int = 30) -> bytes:
    """从某个更新源取一个文件。两种源的返回格式不一样，这里统一成字节。"""
    if source.get("kind") == "api":
        # GitHub contents 接口回的是 JSON，内容在 base64 字段里
        payload = json.loads(_fetch(source["base"] + relative + API_SUFFIX, timeout).decode("utf-8"))
        if isinstance(payload, list):          # 目录
            raise UpdaterError("该路径是目录")
        content = payload.get("content") or ""
        import base64 as _base64
        return _base64.b64decode(content)
    return _fetch(source["base"] + relative, timeout)


def source_base(source: dict) -> str:
    """给界面显示用的源地址。"""
    return source["base"]


def fetch_manifest() -> tuple[dict, str]:
    """把所有更新源都问一遍，挑版本号最高的那份清单。

    jsDelivr 对 @main 有缓存：刚发布的版本，它可能还在发上一版清单。
    以前只认"第一个能连上的源"，于是缓存没刷新时"检查更新"会一直说
    已是最新 —— 用户永远收不到新版本。这里改成全部问一遍取最新，
    连不上或格式不对的源只记一笔错误，不影响结论。
    """
    errors = []
    best: tuple | None = None
    for source in SOURCES:
        try:
            raw = fetch_from(source, MANIFEST_PATH)
            payload = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError, UpdaterError) as exc:
            errors.append(source["label"] + "：" + str(exc)[:60])
            continue
        if not isinstance(payload, dict) or "files" not in payload:
            errors.append(source["label"] + "：清单格式不对")
            continue
        key = _version_tuple(str(payload.get("version") or ""))
        if best is None or key > best[0]:
            best = (key, payload, source_base(source))
    if best is None:
        raise UpdaterError("所有更新源都不可用 —— " + "；".join(errors))
    return best[1], best[2]


def bundled_manifest() -> dict | None:
    """随包发布的那份清单。断网时也能拿它做基线自检。"""
    path = RESOURCE_DIR / MANIFEST_PATH
    if not path.exists():
        path = BASE_DIR / MANIFEST_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_manifest(prefer_remote: bool = True) -> tuple[dict, str]:
    if prefer_remote:
        try:
            return fetch_manifest()
        except UpdaterError:
            pass
    local = bundled_manifest()
    if local:
        # 随包的那份是构建时打进去的，里面的安装包哈希必然比发布出来的旧。
        # 离线时也下载不了安装包，所以直接去掉这段，免得哈希校验误报。
        local = dict(local)
        local.pop("installer", None)
        return local, "随包基线"
    raise UpdaterError("既连不上更新源，包里也没有基线清单")


# ---------------------------------------------------------------- 自检
def _entries(manifest: dict) -> list[dict]:
    """要校验的条目。

    files（界面资源）在任何形态下都要校验；
    source_files（Python 源码）只在以源码方式运行时才有意义 —— exe 里的代码
    封在解包里改不了，校验它只会误导用户。
    """
    from .config import is_frozen

    items = list(manifest.get("files") or [])
    if not is_frozen():
        items += list(manifest.get("source_files") or [])
    return items


def verify(prefer_remote: bool = False) -> dict:
    """逐个文件比对哈希。

    默认拿**随包基线**当参照，而不是远端清单。原因：jsDelivr 会对 @main
    做缓存，刚发完一版时远端清单可能还是旧的，拿它比对会把正常的本地文件
    误报成"被改过"。随包基线是构建时打进去的，精确对应你装的这一版。
    远端清单只用来判断"有没有新版本"。
    """
    manifest, source = load_manifest(prefer_remote)
    results = []
    ok_count = 0
    for entry in _entries(manifest):
        relative = entry.get("path") or ""
        expected = (entry.get("sha256") or "").lower()
        if not relative or not expected:
            continue
        local = resolve_local(relative)
        item = {"path": relative, "expected": expected, "size": entry.get("size") or 0}
        if local is None:
            item.update(status="missing", detail="文件不存在")
        else:
            try:
                actual = sha256_file(local)
            except OSError as exc:
                item.update(status="unreadable", detail=str(exc)[:80])
            else:
                if actual == expected:
                    item.update(status="ok", detail="", actual=actual)
                else:
                    item.update(status="modified", detail="内容与基线不一致", actual=actual)
        if item["status"] == "ok":
            ok_count += 1
        results.append(item)

    bad = [item for item in results if item["status"] != "ok"]
    return {
        "ok": not bad,
        "source": source,
        "checked": len(results),
        "ok_count": ok_count,
        "bad": bad,
        "files": results,
    }


# ---------------------------------------------------------------- 修复
def _needs_whole_web(bad: list[dict]) -> bool:
    return any(item["path"].startswith("web/") for item in bad)


def repair(prefer_remote: bool = True, progress=None) -> dict:
    """把缺失 / 损坏的文件重新下载回来。

    界面文件一旦有一个不对，就把整个 web/ 一起重下 —— 因为 exe 同级的 web/
    是一个"覆盖层"，它必须自洽，不能一半新一半旧。

    期望哈希取自**随包基线**：下载回来的内容必须和"用户装的这一版"对得上。
    如果远端已经发新版，下载回来的文件会哈希不符，这时如实报错并提示先更新，
    而不是把一堆版本错配的文件悄悄盖上去。
    """
    baseline = bundled_manifest()
    if baseline is not None:
        manifest, source = dict(baseline), "随包基线"
        manifest.pop("installer", None)
    else:
        manifest, source = load_manifest(prefer_remote)

    bad = _verify_against(manifest)

    # 远端有没有比本地更新的版本？有的话先提示，别做无意义的覆盖
    remote_note = ""
    if prefer_remote:
        try:
            remote, _ = fetch_manifest()
            if _version_tuple(remote.get("version")) > _version_tuple(manifest.get("version")):
                remote_note = ("远端已经是 v%s，比本地的 v%s 新，"
                               "建议先在「更新与自检」里检查更新并安装新版本。"
                               % (remote.get("version"), manifest.get("version")))
        except UpdaterError:
            pass

    entries = {entry["path"]: entry for entry in _entries(manifest)}
    targets: list[str] = [item["path"] for item in bad]
    if _needs_whole_web(bad):
        targets = sorted({path for path in entries if path.startswith("web/")})

    # 命中的源排在最前面，其余源作为兜底。
    # 这一步很关键：jsDelivr 对 @main 有缓存，刚推上去的提交它可能还在发旧内容。
    # 只认第一个能连上的源是不够的 —— 必须逐个试到「内容哈希对得上」为止。
    ordered = list(SOURCES)
    if source != "随包基线":
        for index, candidate in enumerate(ordered):
            if source_base(candidate) == source:
                ordered.insert(0, ordered.pop(index))
                break
    if not ordered:
        ordered = []

    repaired, failed = [], []
    for index, relative in enumerate(targets, start=1):
        if progress:
            progress(index, len(targets), relative)
        if not ordered:
            failed.append({"path": relative, "error": "没有可用的更新源"})
            continue

        expected = (entries[relative].get("sha256") or "").lower()
        data = None
        last_error = "所有更新源都没能提供正确版本的文件"
        for candidate in ordered:
            try:
                blob = fetch_from(candidate, relative, timeout=60)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError, UpdaterError) as exc:
                last_error = candidate["label"] + "：" + str(exc)[:60]
                continue
            if expected and sha256_bytes(blob) != expected:
                # 这个源给的是旧内容（CDN 缓存），换下一个
                last_error = candidate["label"] + "：内容与基线不符（可能是 CDN 缓存未更新）"
                continue
            data = blob
            break
        if data is None:
            failed.append({"path": relative, "error": last_error})
            continue

        safe = safe_relative(relative)
        if not safe:
            failed.append({"path": relative, "error": "清单里的路径不合法，已跳过"})
            continue
        destination = (BASE_DIR / safe).resolve()
        try:
            destination.relative_to(BASE_DIR.resolve())
        except ValueError:
            failed.append({"path": relative, "error": "路径越出程序目录，已跳过"})
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        except OSError as exc:
            failed.append({"path": relative, "error": "写入失败：" + str(exc)[:70]})
            continue
        repaired.append(relative)

    if remote_note and not repaired:
        failed.append({"path": "(版本)", "error": remote_note})

    return {
        "ok": not failed,
        "source": source,
        "repaired": repaired,
        "failed": failed,
        "targets": targets,
        # 界面文件已经落到 exe 同级的 web/ 覆盖层，重启后生效
        "need_restart": bool(repaired),
        "mode": "web-overlay" if _needs_whole_web(bad) else "file",
    }


def _verify_against(manifest: dict) -> list[dict]:
    bad = []
    for entry in _entries(manifest):
        relative = entry.get("path") or ""
        expected = (entry.get("sha256") or "").lower()
        local = resolve_local(relative)
        if local is None:
            bad.append({"path": relative, "status": "missing", "detail": "文件不存在"})
            continue
        try:
            if sha256_file(local) != expected:
                bad.append({"path": relative, "status": "modified", "detail": "内容与基线不一致"})
        except OSError:
            bad.append({"path": relative, "status": "unreadable", "detail": "读取失败"})
    return bad


# ---------------------------------------------------------------- 更新
def _version_tuple(text: str) -> tuple:
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0, 0, 0])[:4]


def check_update(prefer_remote: bool = True) -> dict:
    manifest, source = load_manifest(prefer_remote)
    latest = str(manifest.get("version") or "").strip()
    current = __version__
    has_update = bool(latest) and _version_tuple(latest) > _version_tuple(current)
    installer = manifest.get("installer") or {}
    return {
        "ok": True,
        "source": source,
        "current": current,
        "latest": latest or current,
        "has_update": has_update,
        "notes": manifest.get("notes") or "",
        "generated": manifest.get("generated") or "",
        "release_page": manifest.get("release_page") or RELEASE_PAGE,
        "installer": {
            "name": installer.get("name") or "",
            "url": installer.get("url") or "",
            "sha256": (installer.get("sha256") or "").lower(),
            "size": installer.get("size") or 0,
        },
    }


def download_installer(info: dict, progress=None) -> dict:
    """下载新版安装包，校验哈希后放进临时目录，等用户决定什么时候装。"""
    installer = (info or {}).get("installer") or {}
    url = installer.get("url") or ""
    if not url:
        raise UpdaterError("清单里没有提供安装包地址，请到发布页手动下载：" + RELEASE_PAGE)

    target_dir = update_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = installer.get("name") or ("Video-Grabing-Setup-" + str(info.get("latest") or "") + ".exe")
    target = target_dir / name

    if progress:
        progress("正在下载 " + name + " …")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=120) as response, open(target, "wb") as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                digest.update(block)
                handle.write(block)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdaterError("下载失败：" + str(exc)[:120] + "（也可以到发布页手动下载）") from None

    expected = installer.get("sha256") or ""
    actual = digest.hexdigest()
    if expected and actual != expected:
        try:
            target.unlink()
        except OSError:
            pass
        raise UpdaterError("安装包哈希校验失败，已丢弃。请到发布页手动下载。")

    return {"ok": True, "path": str(target), "size": target.stat().st_size, "sha256": actual}


def update_dir() -> Path:
    return Path(tempfile.gettempdir()) / "Video-Grabing-update"


def launch_installer(path: str) -> dict:
    """启动安装包。安装程序会自己处理"替换正在运行的程序"。

    只允许运行下载目录里的 .exe —— 这个接口是对本地 HTTP 暴露的，
    不限制路径等于给"任意程序执行"开了个后门。
    """
    try:
        target = Path(path).resolve()
    except OSError:
        raise UpdaterError("安装包路径无效") from None
    try:
        target.relative_to(update_dir().resolve())
    except ValueError:
        raise UpdaterError("只能运行由更新功能下载到 " + str(update_dir()) + " 的安装包") from None
    if not target.exists() or target.suffix.lower() != ".exe":
        raise UpdaterError("安装包不存在或不是可执行文件")
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen([str(target)])
    except OSError as exc:
        raise UpdaterError("启动安装包失败：" + str(exc)[:120]) from None
    return {"ok": True, "detail": "已启动安装程序，按提示完成后重新打开即可"}
