#!/usr/bin/env python3
"""为 Video-Grabing 创建桌面快捷方式（可选同时创建开始菜单项）。

用法：
    python create_shortcut.py                  # 桌面快捷方式
    python create_shortcut.py --start-menu     # 桌面 + 开始菜单
    python create_shortcut.py --remove         # 删除快捷方式
    python create_shortcut.py --target 某个.exe

实现说明：
  .lnk 是 COM 对象，Python 标准库造不出来。这里用系统自带的 PowerShell
  调 WScript.Shell 来创建，命令通过 -EncodedCommand（Base64 / UTF-16LE）传入，
  这样中文名称在任何代码页下都不会变成乱码。
"""
from __future__ import annotations

import argparse
import os
import base64
import ctypes
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "Video-Grabing"
DESCRIPTION = "多平台视频采集器：B站 / 抖音 / 小红书 / 芒果TV / 腾讯视频 / 优酷 / 爱奇艺"


def find_target() -> Path | None:
    """按优先级找可执行文件：完整版 > 单文件版 > 项目根目录下的 exe。"""
    candidates = [
        ROOT / "dist" / "Video-Grabing" / "Video-Grabing.exe",
        ROOT / "dist" / "Video-Grabing.exe",
        ROOT / "Video-Grabing.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def desktop_dir() -> Path:
    """取真实桌面路径。

    不能简单拼 %USERPROFILE%\\Desktop —— 开了 OneDrive 之后桌面会被重定向，
    所以走 SHGetFolderPathW，让系统自己告诉我们。
    """
    CSIDL_DESKTOPDIRECTORY = 0x0010
    buffer = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buffer)
        if buffer.value:
            return Path(buffer.value)
    except (OSError, AttributeError):
        pass
    return Path.home() / "Desktop"


def start_menu_dir() -> Path:
    CSIDL_PROGRAMS = 0x0002
    buffer = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PROGRAMS, None, 0, buffer)
        if buffer.value:
            return Path(buffer.value)
    except (OSError, AttributeError):
        pass
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _shell_candidates() -> list:
    """找 powershell.exe。

    实测某些环境（包括打包后的进程）PATH 里**没有** WindowsPowerShell 目录，
    按名字调用会立刻失败，进程根本起不来 —— 这里补上固定路径兜底。
    """
    import shutil as _shutil
    found = []
    for name in ("powershell", "pwsh"):
        path = _shutil.which(name)
        if path:
            found.append(path)
    for fixed in (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                  r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
                  r"C:\Program Files\PowerShell\7\pwsh.exe"):
        if os.path.isfile(fixed):
            found.append(fixed)
    seen = set()
    unique = []
    for item in found:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def run_powershell(script: str) -> tuple[bool, str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    for shell in _shell_candidates():
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        return proc.returncode == 0, output
    return False, "找不到可用的 PowerShell"


CREATE_SCRIPT = """
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut({link})
$link.TargetPath = {target}
$link.WorkingDirectory = {workdir}
$link.IconLocation = {icon}
$link.Description = {desc}
# 常规窗口：既不最大化也不最小化，交给程序自己决定
$link.WindowStyle = 1
$link.Save()
Write-Output 'created'
"""


def create_link(link_path: Path, target: Path) -> bool:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    script = CREATE_SCRIPT.format(
        link=ps_quote(str(link_path)),
        target=ps_quote(str(target)),
        workdir=ps_quote(str(target.parent)),
        icon=ps_quote(str(target) + ",0"),
        desc=ps_quote(DESCRIPTION),
    )
    ok, output = run_powershell(script)
    if ok and link_path.exists():
        return True
    if output:
        print("    " + output)
    return False


def remove_link(link_path: Path) -> bool:
    if link_path.exists():
        link_path.unlink()
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 Video-Grabing 桌面快捷方式")
    parser.add_argument("--target", help="可执行文件路径，默认自动查找 dist 目录")
    parser.add_argument("--name", default=APP_NAME, help="快捷方式名称")
    parser.add_argument("--start-menu", action="store_true", help="同时创建开始菜单项")
    parser.add_argument("--remove", action="store_true", help="删除快捷方式")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("这个脚本只能在 Windows 上创建 .lnk 快捷方式。")
        return 1

    target = Path(args.target).resolve() if args.target else find_target()
    if not target or not target.exists():
        print("找不到 Video-Grabing.exe。")
        print("请先运行 python build.py 打包，或用 --target 指定 exe 的位置。")
        return 1

    link_name = args.name + ".lnk"
    locations = [desktop_dir() / link_name]
    if args.start_menu:
        locations.append(start_menu_dir() / link_name)

    if args.remove:
        removed = [path for path in locations if remove_link(path)]
        print("已删除：" + ("、".join(str(p) for p in removed) if removed else "没有找到快捷方式"))
        return 0

    print("Video-Grabing 快捷方式")
    print("  目标程序：" + str(target))
    failures = 0
    for link_path in locations:
        if create_link(link_path, target):
            print("  已创建：" + str(link_path))
        else:
            print("  创建失败：" + str(link_path))
            failures += 1

    if failures == 0:
        print("\n完成。回到桌面双击「" + args.name + "」即可启动（内置窗口，不会弹浏览器）。")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
