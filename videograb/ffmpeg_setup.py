"""ffmpeg 首次使用时下载。

安装包不再内置 ffmpeg（那样会多 60–80 MB）。用户第一次需要转码/剪辑时才下载，
放到 exe 同级的 bin/ 目录里 —— muxer.find_ffmpeg() 本来就优先看那里。

下载的是第三方构建的静态可执行文件。程序只做「下载 + 解压 + 放进 bin/」，
不修改它，也不静默执行。用户随时可以把别的版本丢进 bin/ 覆盖。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .config import BIN_DIR

USER_AGENT = "MakiseToolkit/2.5"

# 实测大小（2026-10）：gyan.dev 的 essentials 包 115 MB，BtbN 的完整包 200 MB。
# 下载耗时取决于网速，慢的话可能要几分钟 —— 界面上会一直显示进度。
SOURCES = [
    ("gyan.dev（essentials，约 115 MB）",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("BtbN / GitHub（完整构建，约 200 MB）",
     "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"),
]

# zip 里真正的可执行文件路径形如 ffmpeg-<ver>-essentials_build/bin/ffmpeg.exe
WANTED = ("ffmpeg.exe", "ffprobe.exe")


class FfmpegError(RuntimeError):
    pass


def _candidate_paths() -> list:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    return [BIN_DIR / exe, BIN_DIR / "ffmpeg.exe", BIN_DIR / "ffmpeg"]


def found_path() -> str:
    for path in _candidate_paths():
        if path.is_file():
            return str(path)
    # 再看系统 PATH
    return shutil.which("ffmpeg") or ""


def status() -> dict:
    path = found_path()
    return {"found": bool(path), "path": path, "bin_dir": str(BIN_DIR),
            "in_bin": bool(path) and str(BIN_DIR).lower() in path.lower()}


def _download(url: str, target: Path, progress=None) -> str:
    """下载并返回整包的 SHA-256。"""
    import hashlib
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp, open(target, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                done += len(block)
                if progress and total:
                    progress(min(0.99, done / total), "已下载 %.1f / %.1f MB" % (done / 1048576, total / 1048576))
                elif progress:
                    progress(0.5, "已下载 %.1f MB" % (done / 1048576))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise FfmpegError("下载失败：" + str(exc)[:140]) from None
    return digest.hexdigest()


def _extract(zip_path: Path, progress=None) -> list:
    """从压缩包里挑出 ffmpeg.exe / ffprobe.exe 放进 bin/，返回放进去的文件名。"""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    placed: list = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            for want in WANTED:
                hit = next((n for n in names if Path(n).name.lower() == want.lower()), None)
                if not hit:
                    continue
                dest = BIN_DIR / Path(hit).name
                with archive.open(hit) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                try:
                    os.chmod(dest, 0o755)
                except OSError:
                    pass
                placed.append(dest.name)
                if progress:
                    progress(0.99, "已解压 " + dest.name)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FfmpegError("解压失败：" + str(exc)[:140]) from None
    if not placed:
        raise FfmpegError("压缩包里没有找到 ffmpeg.exe，可能下载到了错误的内容")
    return placed


def install(progress=None) -> dict:
    """依次尝试各个源，成功即止。返回 {ok, source, files, path}。"""
    if found_path():
        return {"ok": True, "already": True, "path": found_path(), "files": []}

    errors: list = []
    for label, url in SOURCES:
        tmp_dir = Path(tempfile.mkdtemp(prefix="vg-ffmpeg-"))
        try:
            if progress:
                progress(0.02, "正在从 " + label + " 下载…")
            zip_path = tmp_dir / "ffmpeg.zip"
            sha = _download(url, zip_path, progress)
            # 记下整包哈希：现在没有官方公布的哈希可供比对，
            # 至少把这次下载的指纹留在返回里，事后可以复核是不是同一个包。
            files = _extract(zip_path, progress)
            if progress:
                progress(1.0, "完成")
            return {"ok": True, "source": label, "files": files,
                    "path": found_path(), "sha256": sha}
        except FfmpegError as exc:
            errors.append(label + "：" + str(exc))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    raise FfmpegError("所有下载源都失败了：\n" + "\n".join(errors))
