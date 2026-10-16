"""WebView2 运行时的检测与"自带运行时"支持。

Windows 上内嵌浏览器（pywebview + EdgeChromium）需要一个 WebView2 运行时。
绝大多数 Win10/11 机器上它随 Edge 一起预装了，但确实有机器没有，
这时只能退化成"用系统浏览器打开" —— 用户看到的是一个网页，不是独立窗口。

三种做法都支持，代价差很多，由使用者自己权衡：

  1. 什么都不做          体积 +0       依赖系统已有的运行时
  2. 内置引导程序        体积 +1.9 MB  缺失时一键联网安装，需要网络
  3. 内置完整离线包      体积 +204 MB  缺失时可离线安装
  4. 固定版本运行时      体积 +180~250 MB  **真正的封装**：完全不依赖系统

第 4 种是把某个具体版本的 WebView2 运行时整个放进程序目录
（约定放在 exe 同级的 webview2/ 下），再通过环境变量
WEBVIEW2_BROWSER_EXECUTABLE_FOLDER 让 WebView2 加载器改用它。
这样即使系统里没有运行时也能开出独立窗口。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_CLIENT_KEYS = (
    "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\" + _CLIENT_GUID,
    "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\" + _CLIENT_GUID,
)

FIXED_DIR_NAME = "webview2"
_FIXED_EXE = "msedgewebview2.exe"
ENV_FIXED = "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"
BOOTSTRAPPER_NAME = "MicrosoftEdgeWebview2Setup.exe"


def app_dir() -> Path:
    """程序所在目录：打包后是 exe 的位置，源码下是项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def fixed_runtime_dir() -> Path | None:
    """找 exe 同级 webview2/ 目录里的固定版本运行时。"""
    base = app_dir() / FIXED_DIR_NAME
    for candidate in (base, base / "Application"):
        if (candidate / _FIXED_EXE).is_file():
            return candidate
    if base.is_dir():
        for child in sorted(base.iterdir(), reverse=True):
            if child.is_dir() and (child / _FIXED_EXE).is_file():
                return child
    return None


def installed_version() -> str:
    """系统里装的 WebView2 运行时版本，没装返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in _CLIENT_KEYS:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "pv")
                text = str(value or "").strip()
                if text:
                    return text
            except OSError:
                continue
    return ""


def activate_fixed_runtime() -> str:
    """程序目录里带了固定版本运行时就启用它。必须在 pywebview 启动之前调用。"""
    found = fixed_runtime_dir()
    if not found:
        return ""
    os.environ[ENV_FIXED] = str(found)
    return str(found)


def bootstrapper_path() -> Path:
    return app_dir() / BOOTSTRAPPER_NAME


def status() -> dict:
    """给界面用的运行时状态。"""
    fixed = fixed_runtime_dir()
    system = installed_version()
    boot = bootstrapper_path()
    if fixed:
        mode, detail = "fixed", "自带运行时：" + str(fixed)
    elif system:
        mode, detail = "system", "系统运行时 " + system
    else:
        mode, detail = "missing", "系统里没有 WebView2 运行时"
    return {
        "mode": mode,
        "ok": mode != "missing",
        "detail": detail,
        "system_version": system,
        "fixed_dir": str(fixed) if fixed else "",
        "fixed_expected": str(app_dir() / FIXED_DIR_NAME),
        "bootstrapper": str(boot),
        "has_bootstrapper": boot.is_file(),
    }
