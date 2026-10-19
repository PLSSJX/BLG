"""本地文件：原生选择对话框 + 可解密性扫描。

浏览器出于安全不会把用户选中的文件的真实路径交出来，而音乐文件动辄上百 MB，
走 HTTP 上传既慢又占内存。所以这里用系统自带的文件对话框拿路径，
整个流程都留在本机。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

# 系统对话框里显示的过滤器
# 可选格式必须和 transcode.py 真正认识的格式对齐，否则用户在对话框里
# 根本看不到那些文件，会以为程序不支持。
FILTER = (
    "音乐文件|*.mp3;*.flac;*.m4a;*.ogg;*.opus;*.wav;*.aac;*.wma;*.ape"
    ";*.ncm;*.qmc0;*.qmc3;*.qmcflac;*.qmcogg;*.mflac;*.mflac0;*.mgg;*.mgg1;"
    "*.kgm;*.kgma;*.vpr;*.kwm"
    "|平台加密格式|*.ncm;*.qmc0;*.qmc3;*.qmcflac;*.qmcogg;*.mflac;*.mflac0;*.mgg;*.mgg1;*.kgm;*.kgma;*.vpr;*.kwm"
    "|所有文件|*.*"
)

DOC_FILTER = (
    "文档|*.docx;*.xlsx;*.pdf;*.md"
    "|Word 文档|*.docx"
    "|Excel 表格|*.xlsx"
    "|PDF|*.pdf"
    "|Markdown|*.md"
    "|所有文件|*.*"
)

IMAGE_FILTER = (
    "图片|*.jpg;*.jpeg;*.png;*.bmp;*.webp"
    "|所有文件|*.*"
)


POWERSHELL_CANDIDATES = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
)


def powershell_exe() -> str:
    """找到 powershell.exe。

    实测环境里 PATH 常常**不含** WindowsPowerShell 目录，直接按名字调用会
    立刻失败（进程都起不来，1ms 返回）。所以先 which，再退回固定路径。
    """
    found = shutil.which("powershell")
    if found:
        return found
    for candidate in POWERSHELL_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return "powershell"


def _run_powershell(script: str, timeout: int = 300) -> tuple[bool, str]:
    """跑一段 PowerShell。返回 (是否成功执行, 输出)。

    注意区分两种"没输出"：**进程起不来**才该换下一个 shell；
    进程正常退出但没有输出（用户点了取消）说明对话框已经弹过了，
    再换 pwsh 跑一遍就会第二次弹窗 —— 装了 PowerShell 7 的机器上点取消必现。
    """
    # Windows PowerShell 的 stdout 默认用**控制台 OEM 代码页**（中文系统是 GBK），
    # 不是 UTF-8。中文文件名按 UTF-8 解码就变成 ����——这就是"选了文件却乱码、
    # 转换时报找不到文件"的原因。这里先把输出编码钉死成 UTF-8，再解码。
    preamble = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                "$OutputEncoding = [Text.Encoding]::UTF8; ")
    encoded = base64.b64encode((preamble + script).encode("utf-16-le")).decode("ascii")
    for shell in ("powershell", "pwsh"):
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-STA", "-EncodedCommand", encoded],
                capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue        # 这个 shell 不可用，换下一个
        raw = proc.stdout or b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 兜底：还是按系统 ANSI 代码页解一次，总比留一串替换字符强
            text = raw.decode("mbcs", "replace")
        return True, text.strip()
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

    kind 决定对话框里默认能看到什么：music / video / doc / image。
    用错筛选器会让对应类型的文件在对话框里直接不可见 —— 用户会以为程序不支持。
    """
    chosen = {"video": VIDEO_FILTER, "doc": DOC_FILTER, "image": IMAGE_FILTER}.get(kind, FILTER)
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
