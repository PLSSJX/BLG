"""独立桌面窗口。

三层降级，尽量让用户拿到一个"正常程序"而不是一个浏览器标签页：

  1. pywebview —— 进程内嵌入 WebView2，窗口属于本程序，任务栏图标、标题、尺寸都由我们控制
  2. Edge 应用模式（--app=）—— 没有标签栏和地址栏的独立窗口，系统里一定有 Edge 时的兜底
  3. 系统默认浏览器 —— 最后手段

只有第 1 种是真正的"独立应用"，后两种是退路，会在界面上如实说明。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

WINDOW_TITLE = "哔哩采集器 BiliGrab"
WINDOW_SIZE = (1480, 960)
MIN_SIZE = (1040, 720)


def wait_until_ready(url: str, timeout: float = 25.0) -> bool:
    """等服务真的开始监听再开窗口，避免用户看到一片"无法连接"。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/ping", timeout=1.5) as response:
                if b"BiliGrab" in response.read(256):
                    return True
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            time.sleep(0.15)
    return False


def find_edge() -> str | None:
    candidates = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None


def _run_webview(url: str, on_closed=None, ctx=None) -> bool:
    try:
        import webview
    except Exception:
        return False
    try:
        window = webview.create_window(
            WINDOW_TITLE, url,
            width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
            min_size=MIN_SIZE,
            text_select=True,
            confirm_close=False,
        )
        if on_closed is not None:
            on_closed(window)
        # 关键：webview.start() 会一直阻塞到窗口关闭，所以模式必须在它之前写进去，
        # 否则窗口活着的整段时间里 /api/state 都只能报默认值。
        if ctx is not None:
            ctx.window_mode = "webview"
        # 显式指定 EdgeChromium：Windows 上其它后端要么是 IE，要么要额外运行库
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(_storage_dir()))
        return True
    except Exception as exc:
        print("  内置窗口启动失败，改用备用方式：" + str(exc)[:160])
        return False


def _storage_dir() -> Path:
    from .config import DATA_DIR
    path = DATA_DIR / "webview"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return DATA_DIR
    return path


def _run_edge_app(url: str) -> bool:
    edge = find_edge()
    if not edge:
        return False
    try:
        proc = subprocess.Popen([
            edge, "--app=" + url,
            "--window-size=%d,%d" % WINDOW_SIZE,
            "--user-data-dir=" + str(_storage_dir() / "edge-app"),
            "--no-first-run", "--no-default-browser-check",
        ])
    except OSError:
        return False
    print("  已用 Edge 应用模式打开独立窗口（无标签栏）。")
    proc.wait()
    return True


def open_window(url: str, ctx=None) -> str:
    """打开独立窗口并阻塞到窗口关闭。返回实际使用的模式。

    ctx.window_mode 会在选定后端的那一刻就写进去 —— 界面上的「运行方式」
    要显示的是"现在用的是哪种窗口"，而不是等窗口关掉之后才补上。
    """
    ready = wait_until_ready(url)
    if not ready:
        print("  警告：服务似乎还没就绪，仍尝试打开窗口。")

    holder: dict = {}

    def remember(window):
        holder["window"] = window
        if ctx is not None:
            # 界面上的"退出程序"按钮会先停服务，再把窗口一起关掉
            ctx.on_quit = lambda: _safe_destroy(window)

    def announce(mode: str) -> str:
        if ctx is not None:
            ctx.window_mode = mode
        return mode

    if _run_webview(url, remember, ctx):
        return announce("webview")
    if ctx is not None:
        ctx.window_mode = "edge-app"      # Edge 是阻塞式等待，同样要提前写
    if _run_edge_app(url):
        return announce("edge-app")
    webbrowser.open(url)
    print("  已用系统默认浏览器打开：" + url)
    return announce("browser")


def _safe_destroy(window) -> None:
    try:
        window.destroy()
    except Exception:
        pass


def backend_available() -> tuple[bool, str]:
    """给设置页显示：当前这台机器能用哪种窗口。"""
    if sys.platform != "win32":
        return False, "非 Windows 平台"
    try:
        import webview  # noqa: F401
        import clr          # noqa: F401
    except Exception:
        return False, "未安装 pywebview / pythonnet，将退回 Edge 应用窗口或浏览器"
    return True, "内置 WebView2 独立窗口"
