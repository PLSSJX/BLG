"""通道 10：外部下载器（yt-dlp / lux / you-get / BBDown / N_m3u8DL-RE）。

这条通道把本机已有的成熟下载器当作解析器用：yt-dlp 自带一套独立维护的
B 站 extractor，等于免费获得一份第三方实现作为交叉验证。
没装也没关系 —— 界面上的「一键安装 yt-dlp」会把它装进项目自带的 .deps 目录，
不污染系统环境。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import BASE_DIR, BIN_DIR, DEPS_DIR, is_frozen
from ..models import Durl, PlayInfo, Stream, VideoInfo
from ..quality import rank_from_resolution
from .base import Engine, EngineUnavailable
from .e1_web_wbi import VIEW, WebWbiEngine

TOOL_CANDIDATES = [
    ("yt-dlp", "yt-dlp"),
    ("lux", "lux"),
    ("you-get", "you-get"),
    ("BBDown", "BBDown"),
    ("N_m3u8DL-RE", "N_m3u8DL-RE"),
    ("annie", "annie"),
]

PROJECT_ROOT = BASE_DIR


def _extra_dirs() -> list[Path]:
    from ..util import home_dir

    # bin/ 是随包发布的位置，.deps/ 是运行时一键安装的位置
    dirs = [BIN_DIR, DEPS_DIR / "bin", DEPS_DIR / "Scripts"]
    home = home_dir()
    if home:
        dirs += [
            home / "scoop" / "shims",
            home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links",
        ]
    return dirs

_EXT = ".exe" if sys.platform == "win32" else ""


def ensure_deps_path() -> None:
    """把 .deps 挂到 sys.path 上，这样 yt_dlp 模块就能被导入。"""
    if DEPS_DIR.exists():
        entry = str(DEPS_DIR)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def load_inprocess_ytdlp():
    """把 yt-dlp 作为普通模块导入，成功则返回模块对象。

    打包成 exe 之后 sys.executable 是 BiliGrab.exe 而不是 python，
    "python -m yt_dlp" 这种调用方式就失效了。所以内置版走这条进程内路径：
    直接调它的 Python API，不需要任何外部解释器。
    """
    ensure_deps_path()
    try:
        import yt_dlp
        return yt_dlp
    except Exception:
        return None


def find_tools() -> dict[str, list[str]]:
    """返回 {展示名: 启动命令列表}，只包含能找到独立可执行文件的工具。"""
    found: dict[str, list[str]] = {}
    for name, label in TOOL_CANDIDATES:
        path = shutil.which(name)
        if not path:
            for directory in _extra_dirs():
                candidate = directory / (name + _EXT)
                try:
                    if candidate.exists():
                        path = str(candidate)
                        break
                except OSError:
                    continue
        if path:
            found[label] = [path]
    return found


def _ytdlp_command() -> list[str] | None:
    """优先用独立可执行文件；未打包时才退回到 "python -m yt_dlp"。"""
    for label, command in find_tools().items():
        if label.startswith("yt-dlp"):
            return command
    if not is_frozen() and load_inprocess_ytdlp() is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def extract_with_ytdlp(url: str, timeout: int = 180) -> dict:
    """统一的 yt-dlp 调用入口：有独立 exe 就用 exe，否则在进程内直接调 API。"""
    command = _ytdlp_command()
    if command:
        proc = subprocess.run(
            [*command, "-J", "--no-warnings", "--no-playlist",
             "--socket-timeout", "20", url],
            capture_output=True, timeout=timeout, check=False,
        )
        if proc.returncode != 0:
            lines = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise EngineUnavailable("yt-dlp 解析失败：" + (lines[-1] if lines else "未知错误"))
        try:
            return json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EngineUnavailable(f"yt-dlp 输出无法解析：{exc}") from None

    module = load_inprocess_ytdlp()
    if module is None:
        raise EngineUnavailable("没有可用的 yt-dlp")
    options = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 20,
        "extract_flat": False, "nocheckcertificate": True,
    }
    try:
        with module.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info)
    except Exception as exc:
        raise EngineUnavailable(f"yt-dlp 解析失败：{str(exc)[:160]}") from None


class ExternalToolEngine(Engine):
    id = "external_tool"
    name = "外部下载器（yt-dlp / lux …）"
    desc = "调用本机已有的成熟下载器当作解析器。yt-dlp 自带独立维护的 B 站 extractor，可以和官方接口相互印证；界面里可以一键把它装进项目自带的 .deps 目录。"
    kind = "external"
    badge = "可选"

    def available(self, ctx) -> tuple[bool, str]:
        tools = find_tools()
        names = list(tools.keys())
        if load_inprocess_ytdlp() is not None:
            names.append("yt-dlp（内置模块）")
        if not names:
            hint = ("未检测到外部下载器。把 yt-dlp.exe 放进程序目录下的 bin 文件夹即可启用"
                    if is_frozen() else
                    "未检测到 yt-dlp / lux / you-get / BBDown，可在「我的 → 外部工具」一键安装 yt-dlp")
            return False, hint
        return True, "已检测到：" + "、".join(names)

    def fetch_info(self, ctx, ref) -> VideoInfo:
        self.require_video_ref(ref)
        data = ctx.net.api(f"{VIEW}?{self.ident(ref)}")
        info = WebWbiEngine._to_info(data, ref)
        info.engine = self.id
        return info

    def _url(self, ref, page: int) -> str:
        url = f"https://www.bilibili.com/video/{ref.bvid or ('av' + str(ref.aid))}"
        if page and page > 1:
            url += f"?p={page}"
        return url

    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        self.require_video_ref(ref)
        if _ytdlp_command() is None and load_inprocess_ytdlp() is None:
            raise EngineUnavailable("未找到 yt-dlp，可以在「我的 → 外部工具」里一键安装")

        try:
            meta = extract_with_ytdlp(self._url(ref, page))
        except (OSError, subprocess.SubprocessError) as exc:
            raise EngineUnavailable(f"调用 yt-dlp 失败：{exc}") from None

        play = PlayInfo(engine=self.id, engine_name=self.name,
                        duration=int(meta.get("duration") or 0))
        formats = meta.get("formats") or []

        # 已封装音轨的格式，直接当单文件用
        muxed = [f for f in formats
                 if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
        muxed.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
        for index, fmt in enumerate(muxed):
            play.durl.append(Durl(
                order=index + 1,
                url=fmt.get("url") or "",
                size=int(fmt.get("filesize") or fmt.get("filesize_approx") or 0),
                length=int(fmt.get("duration") or play.duration or 0),
            ))
        if muxed:
            play.accept_quality = sorted({
                rank_from_resolution(int(f.get("width") or 0), int(f.get("height") or 0),
                                      float(f.get("fps") or 0)) for f in muxed
            }, reverse=True)
            play.accept_desc = [f.get("format_note") or f.get("format_id") or "" for f in muxed]
            play.note = f"yt-dlp 解析：{len(muxed)} 个已封装音视频的格式（免合并）"
            play.raw = {"extractor": meta.get("extractor"), "formats": len(formats)}
            return play

        # 只有分离流：转成 Stream，交给内建下载器 + 合并器
        for fmt in formats:
            vcodec = fmt.get("vcodec") or "none"
            acodec = fmt.get("acodec") or "none"
            if vcodec == "none" and acodec == "none":
                continue
            height = int(fmt.get("height") or 0)
            width = int(fmt.get("width") or 0)
            approx_qn = rank_from_resolution(width, height, float(fmt.get("fps") or 0))
            size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
            url = fmt.get("url") or ""
            label = fmt.get("format_note") or fmt.get("format_id") or ""
            if vcodec != "none":
                play.videos.append(Stream(
                    id=height, qn=approx_qn, label=label, width=width, height=height,
                    codecs=vcodec, bandwidth=int((fmt.get("tbr") or 0) * 1000),
                    size=size, url=url, kind="video", mime=fmt.get("ext") or "mp4",
                    fps=float(fmt.get("fps") or 0),
                ))
            else:
                play.audios.append(Stream(
                    id=int(fmt.get("abr") or 0), qn=int(fmt.get("abr") or 0), label=label,
                    codecs=acodec, bandwidth=int((fmt.get("abr") or 0) * 1000),
                    size=size, url=url, kind="audio", mime=fmt.get("ext") or "m4a",
                ))
        if not play.videos:
            raise EngineUnavailable("yt-dlp 没有返回可用的视频格式")

        play.videos.sort(key=lambda s: (s.height, s.bandwidth), reverse=True)
        play.audios.sort(key=lambda s: s.bandwidth, reverse=True)
        play.accept_quality = sorted({s.qn for s in play.videos}, reverse=True)
        play.accept_desc = [f"{s.height}P" for s in play.videos]
        play.note = "yt-dlp 解析：只有音视频分离流，需要合并"
        play.raw = {"extractor": meta.get("extractor"), "formats": len(formats)}
        return play

    def download_command(self, ref, page: int, qn: int, out_dir: Path,
                         template: str) -> list[str] | None:
        """给任务下载器用的原生命令。"""
        command = _ytdlp_command()
        if not command:
            return None
        heights = {"127": 4320, "126": 2160, "125": 2160, "120": 2160, "116": 1080,
                   "112": 1080, "100": 1080, "80": 1080, "74": 720, "64": 720,
                   "32": 480, "16": 360}
        height = heights.get(str(qn), 0)
        selector = (f"bv*[height<={height}]+ba/b[height<={height}]/b" if height else "bv*+ba/b")
        return [
            *command, "--no-warnings", "--no-playlist", "--newline",
            "-f", selector, "--merge-output-format", "mp4",
            "-o", str(Path(out_dir) / (template + ".%(ext)s")),
            self._url(ref, page),
        ]


def install_ytdlp(progress=None) -> dict:
    """把 yt-dlp 装进项目自带的 .deps 目录（不写系统环境）。"""
    if is_frozen():
        # 打包后的 exe 里没有 pip，装不了包；内置版应当已经把 yt_dlp 打了进去
        if load_inprocess_ytdlp() is not None:
            return {"ok": True, "returncode": 0, "deps_dir": str(DEPS_DIR),
                    "tail": "当前版本已内置 yt-dlp，无需安装。"}
        return {
            "ok": False, "returncode": -1, "deps_dir": str(BIN_DIR),
            "tail": ("打包版无法在线安装。请到 yt-dlp 的发布页下载 yt-dlp.exe，"
                     "放进程序目录下的 bin\\ 文件夹后重新打开本程序即可。"),
        }
    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-warn-script-location",
           "--target", str(DEPS_DIR), "yt-dlp"]
    if progress:
        progress("正在下载 yt-dlp 及其依赖…")
    proc = subprocess.run(cmd, capture_output=True, timeout=900, check=False)
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    ensure_deps_path()
    ok = False
    try:
        import importlib
        import yt_dlp  # noqa: F401
        importlib.reload(yt_dlp)
        ok = True
    except Exception:
        ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "deps_dir": str(DEPS_DIR),
        "tail": "\n".join(output.strip().splitlines()[-12:]),
    }
