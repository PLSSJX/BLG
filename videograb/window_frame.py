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
    if found:
        return found[0]
    # 标题对不上（改名、标题被程序改写、多语言）时不要直接放弃 ——
    # 退回"本进程第一个可见且有标题的窗口"。
    if title:
        spare = find_window("")
        if spare:
            return spare
    # 再退一步：标题已经被清空（我们要把标题文字去掉）时，
    # 上面的分支会因为 GetWindowTextLengthW == 0 而跳过窗口，
    # 这里补一次"只要是本进程的可见顶层窗口就认"。
    found.clear()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback_any(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if not user32.GetWindow(hwnd, 4):     # GW_OWNER：跳过对话框之类的子窗口
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback_any, None)
    return found[0] if found else 0


WM_SETICON = 0x0080
WM_SETTEXT = 0x000C
ICON_SMALL = 0


def clear_titlebar(hwnd: int) -> bool:
    """把标题栏上的图标和文字都去掉，只留右上角三个按钮。

    文字用 WM_SETTEXT 清空；图标清 ICON_SMALL。清完调一次 SetWindowPos
    带 SWP_FRAMECHANGED，让 DWM 按「无图标」重算边框 —— Windows 11 在这种
    情况下会把标题栏按钮画成标准的大号样式。
    """
    if os.name != "nt" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETTEXT, 0, ctypes.c_wchar_p(""))
        user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON, ICON_SMALL, 0)
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x01, 0x02, 0x04, 0x20
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        return True
    except OSError:
        return False


def hide_titlebar_icon(hwnd: int) -> bool:
    """去掉标题栏左边那个小图标，只留文字。

    只清 ICON_SMALL（标题栏那个）；ICON_BIG 保留，任务栏和 Alt-Tab 还需要它。
    """
    if os.name != "nt" or not hwnd:
        return False
    try:
        ctypes.windll.user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON,
                                          ICON_SMALL, 0)
        return True
    except OSError:
        return False


def apply(hwnd: int, *, caption: str = "", text: str = "", border: str = "",
          dark: bool = False, rounded: bool = True) -> bool:
    """把主题色应用到窗口边框上。返回是否成功。"""
    if os.name != "nt" or not hwnd:
        return False
    hide_titlebar_icon(hwnd)
    clear_titlebar(hwnd)
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
