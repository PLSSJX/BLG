"""本地文件：原生选择对话框 + 可解密性扫描。

浏览器出于安全不会把用户选中的文件的真实路径交出来，而音乐文件动辄上百 MB，
走 HTTP 上传既慢又占内存。所以这里用系统自带的文件对话框拿路径，
整个流程都留在本机。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

# 系统对话框里显示的过滤器
FILTER = (
    "音乐文件|*.ncm;*.qmc0;*.qmc3;*.qmcflac;*.qmcogg;*.mflac;*.mflac0;*.mgg;*.mgg1;"
    "*.kgm;*.kgma;*.vpr;*.kwm;*.mp3;*.flac;*.m4a;*.ogg;*.wav;*.aac"
    "|平台加密格式|*.ncm;*.qmc0;*.qmc3;*.qmcflac;*.qmcogg;*.mflac;*.mflac0;*.mgg;*.mgg1;*.kgm;*.kgma;*.vpr;*.kwm"
    "|所有文件|*.*"
)


def _run_powershell(script: str, timeout: int = 300) -> tuple[bool, str]:
    """跑一段 PowerShell。返回 (是否成功执行, 输出)。

    注意区分两种"没输出"：**进程起不来**才该换下一个 shell；
    进程正常退出但没有输出（用户点了取消）说明对话框已经弹过了，
    再换 pwsh 跑一遍就会第二次弹窗 —— 装了 PowerShell 7 的机器上点取消必现。
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-STA", "-EncodedCommand", encoded],
                capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue        # 这个 shell 不可用，换下一个
        return True, (proc.stdout or b"").decode("utf-8", "replace").strip()
    return False, ""


PICK_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = '选择要转换的音乐文件'
$dlg.Multiselect = $true
$dlg.Filter = '{filter}'
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write(($dlg.FileNames -join [char]10))
}
"""


VIDEO_FILTER = (
    "视频文件|*.mp4;*.mkv;*.webm;*.flv;*.ts;*.m4v;*.mov;*.avi;*.wmv;*.mpg;*.mpeg"
    "|所有文件|*.*"
)


def pick_files(ctx=None, kind: str = "music") -> list:
    """弹出系统文件对话框，返回用户选中的绝对路径。取消则返回空列表。

    kind 决定对话框里默认能看到什么：music（音频与加密格式）还是 video。
    剪辑页要选视频，用音乐筛选器根本看不到 .mp4。
    """
    chosen = VIDEO_FILTER if kind == "video" else FILTER
    script = PICK_SCRIPT.replace("{filter}", chosen.replace("'", "''"))
    ok, output = _run_powershell(script)
    if not ok or not output:
        return []
    paths = []
    for line in output.splitlines():
        text = line.strip()
        if text and os.path.isabs(text):
            paths.append(text)
    return paths


FOLDER_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = '选择下载目录'
$dlg.ShowNewFolderButton = $true
if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dlg.SelectedPath)
}
"""


def pick_folder(ctx=None, initial: str = "") -> str:
    """弹出系统文件夹选择框，返回选中的目录；取消则返回空串。

    下载目录以前只能手动输入路径 —— 打错一个字符就会被静默忽略、
    下载还是落到默认位置，用户完全看不出来。
    """
    script = FOLDER_SCRIPT
    start = (initial or "").strip()
    if start and os.path.isdir(start):
        escaped = start.replace("'", "''")
        script = script.replace(
            "$dlg.ShowNewFolderButton = $true",
            "$dlg.ShowNewFolderButton = $true\n$dlg.SelectedPath = '" + escaped + "'")
    ok, output = _run_powershell(script)
    if not ok or not output:
        return ""
    candidate = output.strip().splitlines()[-1].strip()
    return candidate if os.path.isdir(candidate) else ""


def scan_paths(paths: list) -> list:
    """逐个判断文件是不是受支持的加密格式，给界面预检用。"""
    try:
        from .music import decrypt
    except Exception:
        return []

    items = []
    for raw in (paths or [])[:300]:
        path = Path(str(raw))
        entry = {"path": str(path), "name": path.name}
        try:
            entry["size"] = path.stat().st_size
        except OSError:
            entry.update({"exists": False, "error": "文件读不到"})
            items.append(entry)
            continue
        entry["exists"] = True
        try:
            kind = decrypt.sniff(path)
        except Exception:
            kind = ""
        entry["encrypted"] = bool(kind)
        entry["kind"] = kind
        items.append(entry)
    return items
