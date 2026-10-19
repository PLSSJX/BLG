# -*- coding: utf-8 -*-
"""自绘标题栏：把 Windows 的原生标题栏整个换掉，换成 HTML 画的一条。

做法（Chromium / Electron 用的同一套）：
  WM_NCCALCSIZE -> 返回 0，客户区铺满整个窗口，原生边框和标题栏消失
  WM_NCHITTEST  -> 按鼠标位置告诉系统"这里是可以拖 / 可以缩放 / 普通客户区"
  WM_GETMINMAXINFO 不动，最大化尺寸仍由系统算

这样能拿到：
  - 完全自定义的高度、配色、按钮大小与样式
  - 拖动窗口、双击最大化、拖到屏幕边缘贴边分屏（返回 HTCAPTION 就自动有）
  - 四边和四角拖拽缩放（返回对应的 HTxxx）
  - 最小化 / 最大化 / 还原 / 关闭 走前端按钮 → /api/window/*

注意：标题栏右侧那一段必须返回 HTCLIENT，否则鼠标事件到不了页面，
按钮就点不动。这里用固定像素区域来划分，前端 CSS 必须和它对齐。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

# ---- 消息与常量
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCACTIVATE = 0x0086
WM_NCPAINT = 0x0085
WM_GETMINMAXINFO = 0x0024
WM_SETTEXT = 0x000C
WM_SETICON = 0x0080
WM_GETICON = 0x007F

HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

GWLP_WNDPROC = -4

# 尺寸必须和 web/css/app.css 里的 #titlebar 完全一致
TITLEBAR_HEIGHT = 44          # 标题栏总高（比系统默认的 ~32px 大）
BUTTON_WIDTH = 52             # 每个按钮宽度
BUTTON_COUNT = 3
RESIZE_BORDER = 6             # 四边可拖拽缩放的宽度

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CallWindowProcW.restype = LRESULT
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wt.BOOL
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsZoomed.restype = wt.BOOL

_installed: dict = {}


def _hiword(v: int) -> int:
    return (v >> 16) & 0xFFFF


def _loword(v: int) -> int:
    return v & 0xFFFF


def _signed(v: int) -> int:
    """鼠标坐标是有符号 16 位；超过 32767 要还原成负数（多显示器左侧时会出现）。"""
    return v - 0x10000 if v >= 0x8000 else v


def is_enabled() -> bool:
    return os.name == "nt" and sys.platform == "win32"


def install(hwnd: int) -> bool:
    """给窗口装上自绘标题栏。返回是否成功。"""
    if not is_enabled() or not hwnd or hwnd in _installed:
        return bool(hwnd in _installed)

    def wnd_proc(h, msg, wparam, lparam):
        try:
            if msg == WM_NCCALCSIZE and wparam:
                # 客户区铺满整个窗口：原生标题栏和边框就此消失
                return 0
            if msg == WM_NCHITTEST:
                return _hit_test(h, lparam)
            if msg == WM_NCACTIVATE:
                # 不返回它的话，窗口失焦时标题栏会被系统重画成一条白边
                return 1
            if msg == WM_NCPAINT:
                return 0
        except Exception:
            pass
        return user32.CallWindowProcW(_installed[hwnd]["old"], h, msg, wparam, lparam)

    proc = WNDPROC(wnd_proc)
    old = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
    if not old:
        return False
    _installed[hwnd] = {"proc": proc, "old": old}
    # 让系统按新样式重算一次边框
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x01, 0x02, 0x04, 0x20
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
    return True


def _hit_test(hwnd: int, lparam: int) -> int:
    """决定鼠标点的这一块归系统还是归页面。

    顺序很关键：先判四边缩放区，再判标题栏（右侧按钮区除外），最后才是客户区。
    """
    x, y = _signed(_loword(lparam)), _signed(_hiword(lparam))
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom

    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return HTCLIENT

    # 最大化时不要给缩放边框，否则拖动边缘会试图"还原"，行为很怪
    zoomed = bool(user32.IsZoomed(hwnd))
    border = 0 if zoomed else RESIZE_BORDER

    on_left = x < left + border
    on_right = x >= right - border
    on_top = y < top + border
    on_bottom = y >= bottom - border

    if not zoomed:
        if on_top and on_left:
            return HTTOPLEFT
        if on_top and on_right:
            return HTTOPRIGHT
        if on_bottom and on_left:
            return HTBOTTOMLEFT
        if on_bottom and on_right:
            return HTBOTTOMRIGHT
        if on_left:
            return HTLEFT
        if on_right:
            return HTRIGHT
        if on_top:
            return HTTOP
        if on_bottom:
            return HTBOTTOM

    # 标题栏区域：顶部 TITLEBAR_HEIGHT 像素
    if y < top + TITLEBAR_HEIGHT:
        buttons_left = right - BUTTON_WIDTH * BUTTON_COUNT
        if x >= buttons_left:
            return HTCLIENT          # 右侧按钮区交给页面，否则点不动
        return HTCAPTION             # 其余部分：拖动、双击最大化、贴边分屏都靠它

    return HTCLIENT


def uninstall(hwnd: int) -> None:
    info = _installed.pop(hwnd, None)
    if not info:
        return
    try:
        user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, info["old"])
    except OSError:
        pass
