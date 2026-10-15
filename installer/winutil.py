"""安装程序与卸载程序共用的 Windows 小工具。"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "Video-Grabing"
APP_ID = "Video-Grabing"
PUBLISHER = "PLSSJX"
EXE_NAME = "Video-Grabing.exe"
UNINSTALL_NAME = "uninstall.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Video-Grabing"


# CSIDL_LOCAL_APPDATA —— 环境变量缺失时用它兜底
_CSIDL_LOCAL_APPDATA = 0x001C
_CSIDL_APPDATA = 0x001A


def _appdata_dir() -> Path:
    r"""拿到 %LOCALAPPDATA% 的绝对路径。

    以前这里写的是 os.environ[...] or "."，一旦环境变量缺失（从某些启动器、
    计划任务或精简环境里拉起安装程序时就会发生），默认目录会变成
    "Programs\Video-Grabing" 这样的**相对路径** —— 文件被解到当前工作目录，
    而快捷方式里的 TargetPath 又被 PowerShell 按 C:\ 解析，
    结果就是桌面图标指向一个根本不存在的 exe。
    现在依次尝试：环境变量 → 系统 API → 用户主目录，绝不返回相对路径。
    """
    for name in ("LOCALAPPDATA", "USERPROFILE"):
        value = (os.environ.get(name) or "").strip()
        if value and os.path.isabs(value):
            candidate = Path(value)
            if name == "USERPROFILE":
                candidate = candidate / "AppData" / "Local"
            return candidate
    for csidl in (_CSIDL_LOCAL_APPDATA, _CSIDL_APPDATA):
        found = _known_folder(csidl)
        if found:
            return found
    try:
        return Path.home() / "AppData" / "Local"
    except RuntimeError:
        # 连主目录都拿不到时退回系统盘根下的用户目录，仍然是绝对路径
        return Path(os.environ.get("SystemDrive", "C:") + "\\Users\\Default\\AppData\\Local")


def default_install_dir() -> Path:
    return _appdata_dir() / "Programs" / APP_ID


def _known_folder(csidl: int) -> Path | None:
    buffer = ctypes.create_unicode_buffer(260)
    try:
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    except (OSError, AttributeError):
        pass
    return None


def desktop_dir() -> Path:
    # CSIDL_DESKTOPDIRECTORY：让系统告诉我们真实桌面，兼容 OneDrive 重定向
    return _known_folder(0x0010) or Path.home() / "Desktop"


def programs_dir() -> Path:
    # CSIDL_PROGRAMS：开始菜单 → 程序
    return _known_folder(0x0002) or (Path.home() / "AppData" / "Roaming" / "Microsoft"
                                     / "Windows" / "Start Menu" / "Programs")


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _run_powershell(script: str) -> tuple[bool, str]:
    """用 -EncodedCommand 传脚本：中文路径在任何代码页下都不会乱码。"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return proc.returncode == 0, (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    return False, "找不到可用的 PowerShell"


CREATE_LNK = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut({link})
$link.TargetPath = {target}
$link.WorkingDirectory = {workdir}
$link.IconLocation = {icon}
$link.Description = {desc}
$link.Arguments = {args}
$link.WindowStyle = 1
$link.Save()
Write-Output 'ok'
"""


def create_shortcut(link_path: Path, target: Path, description: str,
                    arguments: str = "") -> bool:
    # WScript.Shell 会把相对路径按 PowerShell 自己的工作目录解析（通常是 C:\），
    # 于是快捷方式指向一个不存在的位置。这里统一转成绝对路径。
    link_path = Path(os.path.abspath(str(link_path)))
    target = Path(os.path.abspath(str(target)))
    try:
        link_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    script = CREATE_LNK.format(
        link=_ps_quote(str(link_path)),
        target=_ps_quote(str(target)),
        workdir=_ps_quote(str(target.parent)),
        icon=_ps_quote(str(target) + ",0"),
        desc=_ps_quote(description),
        args=_ps_quote(arguments),
    )
    ok, _ = _run_powershell(script)
    return ok and link_path.exists()


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def kill_running_app() -> int:
    """安装前把正在运行的 Video-Grabing 关掉，否则文件被占用替换不了。"""
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/IM", EXE_NAME, "/T"],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def app_is_running() -> bool:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + EXE_NAME, "/NH"],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return EXE_NAME.lower().encode() in (proc.stdout or b"").lower()


def register_uninstall(install_dir: Path, version: str, size_kb: int) -> None:
    import winreg

    uninstaller = install_dir / UNINSTALL_NAME
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        with key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ,
                              str(install_dir / EXE_NAME))
            if uninstaller.exists():
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                                  '"' + str(uninstaller) + '"')
                winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ,
                                  '"' + str(uninstaller) + '" --silent')
            winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    except OSError:
        pass


def unregister_uninstall() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def directory_size_kb(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total // 1024


def launch(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError:
        pass
