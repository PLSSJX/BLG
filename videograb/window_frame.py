"""把 Windows 的窗口边框/标题栏接进 Material 3 主题。

Kazumi 是 Flutter 应用，它的窗口边框跟着主题走 —— 标题栏和边框用的都是
Material 3 的 surface 色，暗色模式下整体是深色的。而这个程序的窗口虽然界面
完全复刻了 Kazumi，边框却还是 Windows 默认的灰白，一眼就能看出"这是个套壳"。

Windows 11（以及打了更新的 Win10）提供了 DWM 接口可以直接改这些：

  DWMWA_USE_IMMERSIVE_DARK_MODE  暗色标题栏
  DWMWA_WINDOW_CORNER_PREFERENCE 圆角
  DWMWA_BORDER_COLOR             边框颜色
  DWMWA_CAPTION_COLOR            标题栏背景色
  DWMWA_TEXT_COLOR               标题文字颜色

保留系统原生边框（而不是自绘无边框窗口）是有意的：最小化/最大化/关闭、
贴边分屏、双击标题栏、高 DPI 缩放、无障碍这些都还能正常工作。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

# DwmSetWindowAttribute 的属性号
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

DWMWCP_ROUND = 2
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


def _colorref(value: str) -> int:
    """把 #RRGGBB 转成 COLORREF（0x00BBGGRR）。空串或非法值返回"用系统默认"。"""
    text = (value or "").strip().lstrip("#")
    if len(text) != 6:
        return DWMWA_COLOR_DEFAULT
    try:
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return DWMWA_COLOR_DEFAULT
    return (b << 16) | (g << 8) | r


def find_window(title: str = "") -> int:
    """按进程号（可选再加标题）找到本进程的主窗口句柄。"""
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    pid = os.getpid()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if title and buffer.value != title:
            return True
        found.append(hwnd)
        return False

    user32.EnumWindows(callback, None)
    return found[0] if found else 0


def apply(hwnd: int, *, caption: str = "", text: str = "", border: str = "",
          dark: bool = False, rounded: bool = True) -> bool:
    """把主题色应用到窗口边框上。返回是否成功。"""
    if os.name != "nt" or not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi
    except OSError:
        return False

    def set_attr(attr: int, value: int) -> bool:
        data = ctypes.c_uint(value)
        result = dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_uint(attr),
                                           ctypes.byref(data), ctypes.sizeof(data))
        return result == 0

    ok = set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    if rounded:
        set_attr(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    # 颜色接口是 Win11 才有的；老系统上这几个调用会失败，忽略即可
    if caption:
        set_attr(DWMWA_CAPTION_COLOR, _colorref(caption))
    if text:
        set_attr(DWMWA_TEXT_COLOR, _colorref(text))
    if border:
        set_attr(DWMWA_BORDER_COLOR, _colorref(border))
    return ok


_last: dict = {}


def apply_current(title: str = "", **colors) -> bool:
    """找到窗口并应用。找不到就等待重试 —— 窗口创建和主题下发是有先后的。"""
    global _last
    _last = dict(colors)
    hwnd = find_window(title)
    if not hwnd:
        return False
    return apply(hwnd, **colors)


def apply_when_ready(title: str = "", delay: float = 0.0, attempts: int = 40,
                     interval: float = 0.25) -> None:
    """后台等窗口出现，然后把最近一次下发的主题色应用上去。"""
    def worker():
        if delay:
            time.sleep(delay)
        for _ in range(attempts):
            hwnd = find_window(title)
            if hwnd:
                apply(hwnd, **(_last or {"dark": False}))
                return
            time.sleep(interval)

    threading.Thread(target=worker, daemon=True, name="window-frame").start()
