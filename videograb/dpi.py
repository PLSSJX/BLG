"""Windows 高 DPI 适配。

默认情况下 Python 进程是 **DPI 不感知**的（GetProcessDpiAwareness 返回 0）。
在缩放不是 100% 的机器上，Windows 会把整个窗口**位图拉伸** —— 界面看起来是糊的，
WebView2 里的文字尤其明显。150% 缩放下等于把 2/3 分辨率的画面放大回去。

解决办法是在**进程创建任何窗口之前**声明 Per-Monitor V2 感知。
必须在早期调用：一旦有窗口创建了，再设置就会失败。
"""
from __future__ import annotations

import os
import sys

_applied = ""

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_PER_MONITOR_V2 = -4


def enable() -> str:
    """声明 Per-Monitor V2 DPI 感知。返回实际生效的方式（空串表示没做成）。

    逐级降级：Win10 1703+ 用 SetProcessDpiAwarenessContext，
    再老一点的用 shcore.SetProcessDpiAwareness，最后退回 user32.SetProcessDPIAware（系统级）。
    如果感知级别已经被别处设过（比如 pywebview 自己设了），
    调用会失败，这时直接返回现状，不当成错误。
    """
    global _applied
    if _applied:
        return _applied
    if os.name != "nt":
        return ""

    try:
        import ctypes
    except ImportError:
        return ""

    # 1) Per-Monitor V2 —— 多显示器混合缩放也能正确处理
    try:
        user32 = ctypes.windll.user32
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_V2)):
            _applied = "per-monitor-v2"
            return _applied
    except (OSError, AttributeError):
        pass

    # 2) Per-Monitor（Win8.1+）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            _applied = "per-monitor"
            return _applied
    except (OSError, AttributeError):
        pass

    # 3) 系统级（Vista+）：至少不再是"完全不感知"
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            _applied = "system"
            return _applied
    except (OSError, AttributeError):
        pass

    # 走到这里通常是"已经被设置过了"，查一下现状如实返回
    _applied = _current() or "already-set"
    return _applied


def _current() -> str:
    """查当前的感知级别，只用于显示。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        value = ctypes.c_int(0)
        if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) != 0:
            return ""
        return {0: "unaware", 1: "system", 2: "per-monitor"}.get(value.value, str(value.value))
    except Exception:
        return ""


def scale_of_window(hwnd: int = 0) -> float:
    """窗口所在显示器的缩放比例，1.0 表示 100%。用于诊断和窗口尺寸计算。"""
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        dpi = 0
        try:
            dpi = user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) if hwnd else 0
        except (OSError, AttributeError):
            dpi = 0
        if not dpi:
            try:
                dpi = ctypes.windll.user32.GetDpiForSystem()
            except (OSError, AttributeError):
                dpi = 96
        return round((dpi or 96) / 96.0, 2)
    except Exception:
        return 1.0


def double_click_ms() -> int:
    """系统的双击间隔。前端原来写死 420ms，和系统设置不一致时双击要么不灵要么误触。"""
    if os.name != "nt":
        return 500
    try:
        import ctypes
        v = ctypes.windll.user32.GetDoubleClickTime()
        return int(v) if v and 100 <= v <= 2000 else 500
    except Exception:
        return 500


def scale() -> float:
    """主显示器的缩放比（100% -> 1.0，150% -> 1.5）。

    进程是 Per-Monitor V2 感知的，所以给 pywebview 的窗口尺寸是**物理像素**。
    想要"界面上看起一样大"，物理尺寸必须乘这个比值 —— 否则 200% 缩放下
    1480 物理像素只有 740 CSS 像素宽，布局会直接塌成手机版。
    """
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        dc = user32.GetDC(None)
        if not dc:
            return 1.0
        try:
            # LOGPIXELSX = 88
            dpi = ctypes.windll.gdi32.GetDeviceCaps(ctypes.c_void_p(dc), 88)
        finally:
            user32.ReleaseDC(None, ctypes.c_void_p(dc))
        if dpi and dpi > 0:
            return round(dpi / 96.0, 3)
    except Exception:
        pass
    return 1.0


def work_area() -> tuple[int, int]:
    """主显示器的可用区域（物理像素）。SPI_GETWORKAREA = 0x0030。"""
    if os.name != "nt":
        return (0, 0)
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return (rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        pass
    return (0, 0)
