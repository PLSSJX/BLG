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
WM_GETICON = 0x007F
WM_SETTEXT = 0x000C
ICON_SMALL = 0
ICON_BIG = 1


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
        # 两个尺寸都要清：只清 SMALL，有些窗口会把 BIG 缩下来当标题栏图标用
        # 塞透明图标顶掉系统回退的 exe 图标；同时把类图标也换掉，
        # 因为有的窗口会从窗口类里取图标。
        blank = blank_icon()
        for kind in (ICON_SMALL, ICON_BIG):
            user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON, kind,
                                ctypes.c_void_p(blank) if blank else 0)
        if blank:
            try:
                # GCLP_HICON = -14, GCLP_HICONSM = -34
                user32.SetClassLongPtrW(ctypes.c_void_p(hwnd), -14, ctypes.c_void_p(blank))
                user32.SetClassLongPtrW(ctypes.c_void_p(hwnd), -34, ctypes.c_void_p(blank))
            except (OSError, AttributeError):
                pass
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x01, 0x02, 0x04, 0x20
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        return True
    except OSError:
        return False


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_uint),
                ("yHotspot", ctypes.c_uint), ("hbmMask", ctypes.c_void_p),
                ("hbmColor", ctypes.c_void_p)]


_blank_icon = None


def blank_icon() -> int:
    """造一个全透明的 16x16 图标。

    为什么不是直接把图标设成 NULL：Windows 在窗口没有图标时会**回退到 exe
    的应用程序图标**继续画在标题栏左上角 —— 这正是清了 WM_GETICON 之后
    图标还在的原因。塞一个透明图标进去，位置还在但什么都看不见。
    """
    global _blank_icon
    if _blank_icon:
        return _blank_icon
    try:
        gdi = ctypes.windll.gdi32
        size = 16
        # 32bpp 全 0 = 全透明
        color = gdi.CreateBitmap(size, size, 1, 32, ctypes.c_void_p(0))
        # AND 掩码全 1 = 透明
        mask_bits = (ctypes.c_ubyte * (size * size // 8))(*([0xFF] * (size * size // 8)))
        mask = gdi.CreateBitmap(size, size, 1, 1, mask_bits)
        info = ICONINFO(1, 0, 0, ctypes.c_void_p(mask), ctypes.c_void_p(color))
        handle = ctypes.windll.user32.CreateIconIndirect(ctypes.byref(info))
        gdi.DeleteObject(ctypes.c_void_p(color))
        gdi.DeleteObject(ctypes.c_void_p(mask))
        if handle:
            _blank_icon = handle
        return _blank_icon or 0
    except OSError:
        return 0


def titlebar_icon_cleared(hwnd: int) -> bool:
    """查一下标题栏图标到底还在不在（WM_GETICON 返回 0 表示没有了）。"""
    if os.name != "nt" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        small = user32.SendMessageW(ctypes.c_void_p(hwnd), WM_GETICON, ICON_SMALL, 0)
        big = user32.SendMessageW(ctypes.c_void_p(hwnd), WM_GETICON, ICON_BIG, 0)
        return not small and not big
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


WATCH_SECONDS = 300.0
WATCH_INTERVAL = 2.0


def _watch_titlebar(hwnd: int) -> None:
    """低频守着标题栏。

    只在后台线程里做，只调 clear_titlebar（几条 SendMessage，开销可以忽略），
    不动 DWM 颜色，避免反复设置导致闪烁。
    """
    rounds = int(WATCH_SECONDS / WATCH_INTERVAL)
    for _ in range(rounds):
        time.sleep(WATCH_INTERVAL)
        try:
            if not hwnd:
                return
            clear_titlebar(hwnd)
        except OSError:
            return


def apply_when_ready(title: str = "", delay: float = 0.0, attempts: int = 160,
                     interval: float = 0.25) -> None:
    """后台等窗口出现，套用主题色，并在启动阶段反复清标题栏。

    为什么要反复清：WebView2 冷启动可能十几秒，窗口在这个过程中会重建，
    图标会被重新设回去。所以找到窗口后不立刻收工，继续清若干轮，
    直到 WM_GETICON 连续两次返回 0 为止。
    """
    def worker():
        if delay:
            time.sleep(delay)
        hwnd = 0
        clean_streak = 0
        for _ in range(attempts):
            if not hwnd:
                hwnd = find_window(title)
                if not hwnd:
                    time.sleep(interval)
                    continue
            apply(hwnd, **(_last or {"dark": False}))
            if titlebar_icon_cleared(hwnd):
                clean_streak += 1
                if clean_streak >= 2:
                    # 别收工 —— 拖到不同缩放比例的显示器、explorer 重启、
                    # 主题切换都可能让系统重新用 exe 图标重画标题栏。
                    # 之后低频守着，持续 5 分钟。
                    _watch_titlebar(hwnd)
                    return
            else:
                clean_streak = 0
            time.sleep(interval)

    threading.Thread(target=worker, daemon=True, name="window-frame").start()
