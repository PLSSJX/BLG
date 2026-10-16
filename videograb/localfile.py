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
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-STA", "-EncodedCommand", encoded],
                capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if out:
            return True, out
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


def pick_files(ctx=None) -> list:
    """弹出系统文件对话框，返回用户选中的绝对路径。取消则返回空列表。"""
    script = PICK_SCRIPT.replace("{filter}", FILTER.replace("'", "''"))
    ok, output = _run_powershell(script)
    if not ok or not output:
        return []
    paths = []
    for line in output.splitlines():
        text = line.strip()
        if text and os.path.isabs(text):
            paths.append(text)
    return paths


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
