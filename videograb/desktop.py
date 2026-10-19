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
from pathlib import Path

WINDOW_TITLE = "Makise Toolkit"
WINDOW_SIZE = (1480, 960)
MIN_SIZE = (1040, 720)


def fit_to_screen(width: int, height: int, margin: int = 80) -> tuple[int, int]:
    """把窗口尺寸收敛到屏幕可用区域内。

    高缩放的笔记本上（比如 1366x768 @125%）一个 960 高的窗口会超出屏幕，
    标题栏和底部按钮都会被挤到看不见的地方。这里按实际工作区收一下，
    但不会小于 MIN_SIZE —— 屏幕实在太小就让它超出去，总比缩到没法用强。
    """
    try:
        from .dpi import work_area
        screen_w, screen_h = work_area()
    except Exception:
        return (int(width), int(height))
    if not screen_w or not screen_h:
        return (int(width), int(height))
    width = max(MIN_SIZE[0], min(width, screen_w - margin))
    height = max(MIN_SIZE[1], min(height, screen_h - margin))
    return (int(width), int(height))


def wait_until_ready(url: str, timeout: float = 25.0) -> bool:
    """等服务真的开始监听再开窗口，避免用户看到一片"无法连接"。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/ping", timeout=1.5) as response:
                if b"MakiseToolkit" in response.read(256):
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
    except Exception as exc:
        # 以前这里是静默 return False，出问题时完全看不出为什么降级了 ——
        # 源码模式没装 pywebview 时会走到这条，用户只会看到"打开了浏览器"。
        print("  没有内置窗口组件（pywebview 不可用）：" + str(exc)[:120])
        return False
    try:
        # WINDOW_SIZE 是"界面上看起来"的尺寸（CSS 像素）。进程是 DPI 感知的，
        # pywebview 要的是物理像素，所以要乘缩放比 —— 不乘的话 200% 缩放下
        # 窗口只有 740 CSS 像素宽，整个布局会塌掉。
        try:
            from .dpi import scale as _dpi_scale
            _s = _dpi_scale() or 1.0
        except Exception:
            _s = 1.0
        win_w, win_h = fit_to_screen(int(WINDOW_SIZE[0] * _s), int(WINDOW_SIZE[1] * _s))
        _min = (int(MIN_SIZE[0] * _s), int(MIN_SIZE[1] * _s))
        window = webview.create_window(
            WINDOW_TITLE, url,
            width=win_w, height=win_h,
            min_size=_min,
            text_select=True,
            confirm_close=False,
        )
        if on_closed is not None:
            on_closed(window)
        if ctx is not None:
            # 自绘标题栏的按钮由后端代为调用窗口方法
            ctx.window = window
            if (ctx.settings.titlebar or "").strip().lower() == "custom":
                try:
                    from .titlebar import install as install_titlebar
                    from .window_frame import find_window
                    def _hook():
                        # 窗口刚 create 还没真正显示，等它出现再挂钩子
                        for _ in range(160):
                            hwnd = find_window(WINDOW_TITLE)
                            if hwnd and install_titlebar(hwnd):
                                # 子窗口是陆续建出来的，补挂几轮
                                from .titlebar import _hook_children
                                import time as _t2
                                for _ in range(40):
                                    _t2.sleep(0.5)
                                    try:
                                        _hook_children(hwnd)
                                    except Exception:
                                        pass
                                return
                            import time as _t
                            _t.sleep(0.25)
                    import threading as _th
                    _th.Thread(target=_hook, daemon=True, name="titlebar-hook").start()
                except Exception as exc:
                    print("  自绘标题栏未启用：" + str(exc)[:120])
        # 关键：webview.start() 会一直阻塞到窗口关闭，所以模式必须在它之前写进去，
        # 否则窗口活着的整段时间里 /api/state 都只能报默认值。
        if ctx is not None:
            ctx.window_mode = "webview"
        # 窗口一出现就把 Material 3 的主题色刷到边框上。
        # 颜色是界面加载后通过 /api/window/frame 下发的，所以这里只需要
        # 起一个后台线程等着窗口出现，然后套用"最近一次"下发的颜色。
        try:
            from .window_frame import apply_when_ready
            apply_when_ready(WINDOW_TITLE)
        except Exception as exc:
            print("  窗口边框上色失败（不影响使用）：" + str(exc)[:100])
        # 托盘必须在 webview.start() 之前起：它会一直阻塞到窗口关闭，
        # 之后再起就没有主线程活路了。托盘自己开后台线程跑消息循环。
        try:
            from .tray import Tray, app_icon_path
            tray = Tray(
                window_getter=lambda: window,
                icon_path=app_icon_path(),
                on_quit=(ctx.on_quit if ctx is not None else None),
            )
            tray.start()
        except Exception as exc:
            print("  托盘图标不可用（不影响主程序）：" + str(exc)[:120])
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
        print("  没有找到 Edge，无法使用应用模式窗口。")
        return False
    try:
        proc = subprocess.Popen([
            edge, "--app=" + url,
            "--window-size=%d,%d" % fit_to_screen(*WINDOW_SIZE),
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
    # 到这里说明内置窗口和 Edge 应用模式都起不来。
    # **绝不再自动打开系统浏览器** —— 用户点的是桌面程序，给他一个浏览器
    # 标签页只会让人以为"这软件是个网页"，而且看不出到底哪里出了问题。
    # 改成弹一个原生对话框，把原因和补救办法讲清楚，网址也给他，
    # 但要由他自己决定要不要用浏览器打开。
    _explain_failure(url)
    return announce("failed")


def _explain_failure(url: str) -> None:
    """所有窗口方案都失败时的兜底：原生对话框，不碰浏览器。"""
    reason = (
        "内置窗口没能启动，可能是系统里缺少 WebView2 运行时。\n\n"
        "服务已经在后台正常运行：\n"
        + url + "\n\n"
        "可以用下面任一方式补救：\n"
        "  1. 安装 WebView2 运行时（微软官网免费下载，装完重开本程序）\n"
        "  2. 如果程序目录里有 MicrosoftEdgeWebview2Setup.exe，直接运行它\n"
        "  3. 把固定版本的 WebView2 运行时放进程序目录的 webview2\\ 子目录\n\n"
        "在此之前，你也可以自己用浏览器打开上面的地址继续使用。"
    )
    print("  内置窗口未能启动。服务仍在运行：" + url)
    try:
        import ctypes
        # 0x10 = 错误图标，0x40000 = 置顶，避免被别的窗口挡住看不见
        ctypes.windll.user32.MessageBoxW(None, reason, "MakiseToolkit", 0x10 | 0x40000)
    except Exception as exc:
        print("  （无法弹出提示框：" + str(exc)[:80] + "）")
        print(reason)


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
