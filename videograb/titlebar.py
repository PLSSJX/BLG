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
HTTRANSPARENT = -1

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

user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.ReleaseCapture.argtypes = []
user32.ReleaseCapture.restype = wt.BOOL
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
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
            if msg == WM_ERASEBKGND and _bg_brush:
                rect = wt.RECT()
                user32.GetClientRect(ctypes.c_void_p(h), ctypes.byref(rect))
                user32.FillRect(ctypes.c_void_p(wparam), ctypes.byref(rect),
                                ctypes.c_void_p(_bg_brush))
                return 1
            if msg == WM_START_RESIZE:
                user32.ReleaseCapture()
                user32.SendMessageW(ctypes.c_void_p(h), WM_NCLBUTTONDOWN, wparam, 0)
                return 0
            if msg == WM_START_DRAG:
                # 这里就在 UI 线程上，可以安全地进入系统拖拽循环
                user32.ReleaseCapture()
                user32.SendMessageW(ctypes.c_void_p(h), WM_NCLBUTTONDOWN, HTCAPTION, 0)
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
    # WebView2 的子窗口是异步建出来的，这里先挂一次，再由调用方补挂
    _hook_children(hwnd)
    _fit_children(hwnd)
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

    # 一律交还给页面。
    #
    # 原来这里是"顶部 TITLEBAR_HEIGHT 像素返回 HTCAPTION"，但那有两个问题：
    # ① 这里用的是**物理像素**，而 HTML 标题栏的 44px 是 **CSS 像素**，
    #    150% 缩放下实际占 66 物理像素，两者对不上；
    # ② WebView2 的渲染窗口在独立进程、盖满客户区，这个函数在真实路径上
    #    根本不会被调用（系统问的是那个子窗口）。
    # 拖动、双击、缩放全部由页面用 CSS 像素判断再主动发起，天然跟着 DPI 走。
    return HTCLIENT


def _child_hit_test(top: int, child: int, lparam: int) -> int:
    """子窗口的命中测试。

    WebView2 是盖满客户区的子窗口，鼠标在标题栏上时命中的是它，不是顶层窗口 ——
    所以顶层返回 HTCAPTION 也轮不到。这里让标题栏那一条返回 HTTRANSPARENT，
    系统就会把消息继续往上交给父窗口，父窗口再给 HTCAPTION，拖动才成立。
    右侧按钮区必须保留给页面，否则按钮点不动。
    """
    x, y = _signed(_loword(lparam)), _signed(_hiword(lparam))
    rect = wt.RECT()
    user32.GetWindowRect(top, ctypes.byref(rect))
    if rect.right <= rect.left:
        return HTCLIENT
    if rect.top <= y < rect.top + TITLEBAR_HEIGHT:
        buttons_left = rect.right - BUTTON_WIDTH * BUTTON_COUNT
        if x < buttons_left:
            return HTTRANSPARENT
    return HTCLIENT


def _subclass_child(top: int, child: int) -> None:
    if not child or child in _installed:
        return

    def child_proc(h, msg, wparam, lparam):
        try:
            if msg == WM_NCHITTEST:
                result = _child_hit_test(top, h, lparam)
                if result != HTCLIENT:
                    return result
        except Exception:
            pass
        return user32.CallWindowProcW(_installed[h]["old"], h, msg, wparam, lparam)

    proc = WNDPROC(child_proc)
    old = user32.SetWindowLongPtrW(child, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
    if old:
        _installed[child] = {"proc": proc, "old": old}


def _hook_children(top: int) -> int:
    """把顶层窗口下面所有子窗口都挂上钩子。返回挂上的数量。"""
    kids: list = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(h, _l):
        kids.append(h)
        return True

    try:
        user32.EnumChildWindows(ctypes.c_void_p(top), collect, None)
    except OSError:
        return 0
    count = 0
    for kid in kids:
        try:
            _subclass_child(top, kid)
            count += 1
        except (OSError, ValueError):
            continue
    return count


# 给父窗口留出的可拖拽边距。子窗口内缩这么多之后，最外圈就归父窗口了，
# 四边四角的 WM_NCHITTEST 才轮得到我们，缩放才成立。
INSET = 0   # WebView2 会自己重排子窗口，内缩留边距的做法留不住，改由页面发起缩放

WM_ERASEBKGND = 0x0014
_bg_brush = 0


def set_background(hwnd: int, colorref: int) -> None:
    """给父窗口刷上和主题一致的底色。

    子窗口内缩之后，最外圈 6px 是父窗口画的 —— 不设底色的话就是系统默认的
    白色，等于又给自己造了个白框。
    """
    global _bg_brush
    if os.name != "nt" or not hwnd:
        return
    try:
        gdi = ctypes.windll.gdi32
        if _bg_brush:
            gdi.DeleteObject(ctypes.c_void_p(_bg_brush))
        _bg_brush = gdi.CreateSolidBrush(ctypes.c_uint(colorref & 0xFFFFFF))
        user32.InvalidateRect(ctypes.c_void_p(hwnd), None, True)
    except OSError:
        pass


WM_APP = 0x8000
WM_START_DRAG = WM_APP + 71
def is_zoomed(hwnd: int) -> bool:
    """窗口当前是不是最大化。

    不缓存状态：用户可能拖到屏幕顶端让系统最大化、也可能用 Win+Up，
    缓存会和真实状态对不上，双击时就变成"该还原却继续最大化"。
    """
    if os.name != "nt" or not hwnd:
        return False
    try:
        return bool(user32.IsZoomed(ctypes.c_void_p(hwnd)))
    except OSError:
        return False


WM_NCLBUTTONDOWN = 0x00A1


HT_CODES = {
    "left": HTLEFT, "right": HTRIGHT, "top": HTTOP, "bottom": HTBOTTOM,
    "topleft": HTTOPLEFT, "topright": HTTOPRIGHT,
    "bottomleft": HTBOTTOMLEFT, "bottomright": HTBOTTOMRIGHT,
}
WM_START_RESIZE = WM_APP + 72


def start_resize(hwnd: int, edge: str) -> bool:
    """从页面发起一次系统缩放拖拽。

    和拖动同一个道理：WebView2 的渲染窗口在独立进程里，边缘的命中测试
    永远到不了父窗口，所以只能由页面检测到"按在边上"之后主动让系统接管。
    """
    if os.name != "nt" or not hwnd:
        return False
    ht = HT_CODES.get((edge or "").lower())
    if not ht:
        return False
    try:
        return bool(user32.PostMessageW(ctypes.c_void_p(hwnd), WM_START_RESIZE, ht, 0))
    except OSError:
        return False


def start_drag(hwnd: int) -> bool:
    """让系统接手势开始拖动窗口。

    为什么不能靠 WM_NCHITTEST 返回 HTCAPTION：WebView2 渲染在**独立进程**里，
    它那个 Chrome_RenderWidgetHostHWND 盖满客户区，而跨进程窗口没法子类化
    （SetWindowLongPtr 返回 ERROR_ACCESS_DENIED）。鼠标点永远先命中它，
    父窗口的命中测试根本轮不到。

    改成：页面里按下标题栏时调这里，ReleaseCapture 之后补一条
    WM_NCLBUTTONDOWN(HTCAPTION)，系统就会当成"用户按住了标题栏"，
    拖动、贴边分屏、双击最大化全都由系统原生处理。
    """
    if os.name != "nt" or not hwnd:
        return False
    try:
        # 关键：不能在 HTTP 线程里直接 SendMessage(WM_NCLBUTTONDOWN)。
        # 拖拽是一个**模态移动循环**，必须跑在窗口自己的 UI 线程上；
        # 跨线程发过去只会立刻返回、窗口纹丝不动（实测就是拖不动）。
        # 所以只往窗口消息队列投一条自定义消息，由窗口过程在 UI 线程里发起。
        ok = user32.PostMessageW(ctypes.c_void_p(hwnd), WM_START_DRAG, 0, 0)
        return bool(ok)
    except OSError:
        return False

def _fit_children(top: int) -> None:
    """把子窗口拉满整个窗口。

    WM_NCCALCSIZE 把非客户区去掉之后，窗口变"大"了，但 WebView2 那个子窗口
    还按**旧客户区**的尺寸摆着，四周就露出一圈父窗口背景（白框）。
    这里把每个子窗口重设成 (0,0,客户区宽,客户区高)。
    """
    rect = wt.RECT()
    if not user32.GetClientRect(ctypes.c_void_p(top), ctypes.byref(rect)):
        return
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    kids: list = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(h, _l):
        kids.append(h)
        return True

    try:
        user32.EnumChildWindows(ctypes.c_void_p(top), collect, None)
    except OSError:
        return
    for kid in kids:
        try:
            user32.SetWindowPos(ctypes.c_void_p(kid), None, INSET, INSET,
                                max(1, width - INSET * 2), max(1, height - INSET * 2),
                                SWP_NOZORDER | SWP_NOACTIVATE)
        except OSError:
            continue


def uninstall(hwnd: int) -> None:
    info = _installed.pop(hwnd, None)
    if not info:
        return
    try:
        user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, info["old"])
    except OSError:
        pass
