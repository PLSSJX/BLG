# -*- coding: utf-8 -*-
"""OCR 的按需组件与识别流程。

为什么不是一上来就下载 60 MB 的 Tesseract：
Windows 10/11 自带的 OCR 引擎（Windows.Media.Ocr）已经支持中文，
质量对付扫描件够用，而且**不用下载任何东西**。所以顺序是：

  1. 系统装了 Tesseract → 用它（版式复杂时更稳）
  2. 否则用 Windows 自带 OCR（零下载）
  3. 两个都没有 → 明确告诉用户缺什么，不要假装能识别

唯一必须下载的是"把 PDF 页面画成图片"这一步：pypdfium2 的
Windows wheel 只有 3.8 MB，从 PyPI 取。它用 BSD/Apache 授权，
不像 PyMuPDF 那样是 AGPL —— 分发的桌面程序不能碰 AGPL。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .config import BIN_DIR

USER_AGENT = "MakiseToolkit/3.7"
PYLIB = BIN_DIR / "pylib"
RENDERER_PACKAGE = "pypdfium2"
REPAIR_PACKAGE = "pypdf"


class OcrError(RuntimeError):
    pass


# ---------------------------------------------------------------- 组件状态
def _importable(name: str) -> bool:
    if str(PYLIB) not in sys.path:
        sys.path.insert(0, str(PYLIB))
    try:
        __import__(name)
        return True
    except Exception:
        return False


def find_tesseract() -> str:
    # 先看我们自己的安装目录（可能是压缩包解出来的，路径不一定在顶层）
    local = Path(os.environ.get("LOCALAPPDATA") or "C:") / "MakiseToolkit" / "tesseract"
    if local.is_dir():
        for found in local.rglob("tesseract.exe"):
            return str(found)
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for path in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        BIN_DIR / "tesseract.exe",
    ):
        if path.is_file():
            return str(path)
    return ""


# Tesseract 是可选的增强项：系统 OCR 已经能用，装了它版式复杂时更稳。
# 装在 %LOCALAPPDATA% 下而不是 Program Files —— 那里需要管理员权限，
# 一个桌面小工具不该为了装个 OCR 弹 UAC。
TESS_DIR = Path(os.environ.get("LOCALAPPDATA") or str(BIN_DIR)) / "MakiseToolkit" / "tesseract"
TESS_INDEX = "https://digi.bib.uni-mannheim.de/tesseract/"
TESS_MANUAL = "https://github.com/UB-Mannheim/tesseract/wiki"
TESSDATA_BASE = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/"


def _latest_tesseract_url() -> str:
    """从发行页索引里挑最新那个安装包。"""
    req = urllib.request.Request(TESS_INDEX, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", "ignore")
    names = re.findall(r"tesseract-ocr-w64-setup-[0-9.]+\.[0-9]+\.exe", html)
    if not names:
        raise OcrError("没在发行页找到安装包。可以手动下载：" + TESS_MANUAL)
    names = sorted(set(names))
    return TESS_INDEX + names[-1]


def _download(url: str, target: Path, progress=None, label: str = "") -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp, open(target, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            block = resp.read(1 << 18)
            if not block:
                break
            out.write(block)
            done += len(block)
            if progress and total:
                progress(min(0.95, done / total), (label or "下载中") + " %.1f / %.1f MB"
                         % (done / 1048576, total / 1048576))


def install_tesseract(progress=None) -> dict:
    """下载并静默安装 Tesseract，再补上中英文语言包。"""
    if os.name != "nt":
        raise OcrError("只有 Windows 有这套安装包，其它系统请用包管理器装 tesseract")

    if progress:
        progress(0.02, "正在查找最新版本…")
    url = _latest_tesseract_url()
    with tempfile.TemporaryDirectory(prefix="mt-tess-") as tmp:
        installer = Path(tmp) / "tesseract-setup.exe"
        if progress:
            progress(0.05, "正在下载安装包…")
        _download(url, installer, progress, "已下载")
        if progress:
            progress(0.96, "正在安装…")
        TESS_DIR.mkdir(parents=True, exist_ok=True)
        # NSIS：/S 静默，/D= 指定目录且必须是最后一个参数（不能加引号）
        proc = subprocess.run([str(installer), "/S", "/D=" + str(TESS_DIR)],
                              capture_output=True, timeout=600)
        exe = TESS_DIR / "tesseract.exe"
        if not exe.is_file():
            raise OcrError("安装程序没有生成 tesseract.exe（退出码 %d）。可以手动安装：%s"
                           % (proc.returncode, TESS_MANUAL))

    # 语言包：安装包默认只带英文，中文要自己下
    data_dir = TESS_DIR / "tessdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("chi_sim", "eng"):
        target = data_dir / (name + ".traineddata")
        if target.is_file():
            continue
        try:
            if progress:
                progress(0.97, "正在下载语言包 " + name + "…")
            _download(TESSDATA_BASE + name + ".traineddata", target, None, name)
        except Exception:
            pass                    # 语言包下不下来不影响英文识别
    if progress:
        progress(1.0, "Tesseract 已就绪")
    return {"ok": True, "path": str(TESS_DIR / "tesseract.exe"),
            "tessdata": str(data_dir), "source": url}


def install_tesseract_from(installer_path: str, progress=None) -> dict:
    """用用户自己下载的安装包安装（发行站连不上时的退路）。"""
    installer = Path(str(installer_path))
    if not installer.is_file():
        raise OcrError("找不到这个文件：" + str(installer_path))
    if installer.suffix.lower() == ".exe":
        TESS_DIR.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(0.3, "正在安装…")
        subprocess.run([str(installer), "/S", "/D=" + str(TESS_DIR)],
                       capture_output=True, timeout=900)
        exe = TESS_DIR / "tesseract.exe"
        if not exe.is_file():
            raise OcrError("安装程序没有生成 tesseract.exe，可能不是官方安装包")
        return {"ok": True, "path": str(exe), "tessdata": str(TESS_DIR / "tessdata")}
    if installer.suffix.lower() == ".zip":
        if progress:
            progress(0.3, "正在解压…")
        TESS_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(installer) as zf:
            zf.extractall(TESS_DIR)
        found = list(TESS_DIR.rglob("tesseract.exe"))
        if not found:
            raise OcrError("压缩包里没找到 tesseract.exe")
        return {"ok": True, "path": str(found[0]), "tessdata": str(found[0].parent / "tessdata")}
    raise OcrError("只支持 .exe 安装包或 .zip 压缩包")


def _windows_ocr_ready() -> bool:
    """Windows 自带 OCR 是否可用（Win10 1809+ 才有）。"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "try { [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null; "
             "'yes' } catch { 'no' }"],
            capture_output=True, timeout=25, creationflags=0x08000000)
        return b"yes" in (out.stdout or b"")
    except (OSError, subprocess.SubprocessError):
        return False


def status() -> dict:
    tess = find_tesseract()
    return {
        "renderer": _importable(RENDERER_PACKAGE),
        "repair": _importable(REPAIR_PACKAGE),
        "tesseract": tess,
        "windows_ocr": _windows_ocr_ready(),
        "pylib": str(PYLIB),
    }


# ---------------------------------------------------------------- 组件下载
def _pypi_wheel(name: str) -> tuple:
    """从 PyPI 取最新一版、适合当前解释器的 Windows wheel。"""
    import json as _json
    req = urllib.request.Request("https://pypi.org/pypi/%s/json" % name,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    version = data["info"]["version"]
    files = data["releases"].get(version) or []
    tag = "cp%d%d" % sys.version_info[:2]
    best = None
    for item in files:
        filename = item["filename"]
        if not filename.endswith(".whl"):
            continue
        if "win_amd64" not in filename:
            continue
        # 纯 Python 轮子（py3-none-any）或者匹配当前解释器的
        pure = "py3-none-any" in filename or "py2.py3-none-any" in filename
        if pure or tag in filename or "py%d-none" % sys.version_info[0] in filename:
            best = item
            if tag in filename:
                break
    if best is None:
        raise OcrError("PyPI 上没有可用的 Windows 安装包：" + name)
    return best["url"], best["filename"], best.get("size") or 0


def install_component(name: str, progress=None) -> dict:
    """下载并展开一个纯二进制依赖到 bin/pylib。"""
    url, filename, size = _pypi_wheel(name)
    PYLIB.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mt-ocr-") as tmp:
        target = Path(tmp) / filename
        if progress:
            progress(0.05, "正在下载 " + filename)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=180) as resp, open(target, "wb") as out:
            total = int(resp.headers.get("Content-Length") or size or 0)
            done = 0
            while True:
                block = resp.read(1 << 18)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if progress and total:
                    progress(min(0.9, 0.05 + 0.85 * done / total),
                             "已下载 %.1f / %.1f MB" % (done / 1048576, total / 1048576))
        if progress:
            progress(0.92, "正在解压…")
        with zipfile.ZipFile(target) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                if "/" not in member:
                    continue
                dest = PYLIB / member
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    if str(PYLIB) not in sys.path:
        sys.path.insert(0, str(PYLIB))
    import importlib
    for mod in list(sys.modules):
        if mod == name or mod.startswith(name + "."):
            del sys.modules[mod]
    importlib.import_module(name)
    if progress:
        progress(1.0, "已就绪：" + name)
    return {"ok": True, "package": name, "version": filename, "dir": str(PYLIB)}


def ensure_renderer(progress=None) -> bool:
    if _importable(RENDERER_PACKAGE):
        return True
    install_component(RENDERER_PACKAGE, progress)
    return True


# ---------------------------------------------------------------- 渲染
def _write_png(path, width: int, height: int, data: bytes, stride: int, channels: int = 3) -> None:
    """把 pdfium 的 BGRA 缓冲区写成 PNG。

    不用 Pillow：这个程序不带任何第三方 Python 包，多一个依赖就多十几 MB，
    而为了一张图去引 Pillow 不值当。PNG 的规则很简单，自己拼就行。
    """
    import struct
    import zlib as _zlib

    rows = bytearray()
    step = channels
    for y in range(height):
        base = y * stride
        rows.append(0)                     # 过滤器类型 0（不过滤）
        line = data[base:base + width * step]
        # BGR(A) → RGB —— pdfium 默认给的是 3 通道，开了透明才是 4 通道
        rgb = bytearray(width * 3)
        rgb[0::3] = line[2::step]
        rgb[1::3] = line[1::step]
        rgb[2::3] = line[0::step]
        rows.extend(rgb)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", _zlib.compress(bytes(rows), 6))
    png += chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def render_pages(pdf_path: str, out_dir: str, scale: float = 2.0, limit: int = 0) -> list:
    """把 PDF 每页渲染成 PNG，返回图片路径列表。"""
    if not _importable(RENDERER_PACKAGE):
        raise OcrError("还没装渲染组件（pypdfium2），先在设置里点「安装 OCR 组件」。")
    import pypdfium2 as pdfium

    paths = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        count = len(doc)
        if limit:
            count = min(count, limit)
        for index in range(count):
            page = doc[index]
            bitmap = page.render(scale=scale)
            target = Path(out_dir) / ("page%03d.png" % (index + 1))
            _write_png(target, bitmap.width, bitmap.height, bytes(bitmap.buffer), bitmap.stride,
                       getattr(bitmap, "n_channels", 3))
            paths.append(str(target))
            bitmap.close()
            page.close()
    finally:
        doc.close()
    return paths


# ---------------------------------------------------------------- 识别
_PS_OCR = r"""
param([string]$List)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { Write-Output '{"error":"no-engine"}'; exit 0 }
$files = Get-Content -Path $List -Encoding UTF8
foreach ($path in $files) {
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @()
        foreach ($line in $result.Lines) { $lines += $line.Text }
        $payload = @{ path = $path; text = ($lines -join [char]10) }
        Write-Output ($payload | ConvertTo-Json -Compress)
        $stream.Dispose()
    } catch {
        Write-Output (@{ path = $path; error = $_.Exception.Message } | ConvertTo-Json -Compress)
    }
}
"""


def _ocr_with_windows(images: list) -> list:
    with tempfile.TemporaryDirectory(prefix="mt-ocr-") as tmp:
        script = Path(tmp) / "ocr.ps1"
        script.write_text(_PS_OCR, encoding="utf-8-sig")
        listing = Path(tmp) / "list.txt"
        listing.write_text("\n".join(images), encoding="utf-8")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-List", str(listing)],
            capture_output=True, timeout=300)
        raw = (proc.stdout or b"").decode("utf-8", "replace")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _tidy(text: str) -> str:
    """收拾 OCR 输出。

    Windows OCR 会把中文按"词"拆开用空格连起来（"低 功 耗 时 钟"），
    直接拼回去会丢英文单词之间的空格，所以只压缩"两个汉字之间"的空格。
    """
    text = (text or "").replace("\r\n", "\n")
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    # 中文标点前面的空格要去掉（OCR 常把「，」「。」识别成独立的词）
    text = re.sub(r"\s+(?=[，。；：、！？（）《》])", "", text)
    text = re.sub(r"(?<=[（《])\s+", "", text)
    # 全角的「．」多半是句点在英文数字后的误判，留在原样反而更乱
    text = text.replace("．", ".")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def _ocr_with_tesseract(images: list, exe: str, langs: str = "chi_sim+eng") -> list:
    out = []
    for path in images:
        try:
            proc = subprocess.run([exe, path, "stdout", "-l", langs, "--psm", "6"],
                                  capture_output=True, timeout=180)
            text = (proc.stdout or b"").decode("utf-8", "replace")
            if proc.returncode != 0 and not text.strip():
                out.append({"path": path, "error": (proc.stderr or b"").decode("utf-8", "replace")[:200]})
            else:
                out.append({"path": path, "text": text})
        except (OSError, subprocess.SubprocessError) as exc:
            out.append({"path": path, "error": str(exc)[:200]})
    return out


def ocr_pdf(pdf_path: str, progress=None, scale: float = 2.0, limit: int = 0) -> dict:
    """把扫描件 PDF 变成可搜索的文本。返回 {text, pages, chars, out}。"""
    source = Path(pdf_path)
    if not source.is_file():
        raise OcrError("找不到这个 PDF：" + str(pdf_path))

    tess = find_tesseract()
    use_windows = (not tess) and _windows_ocr_ready()
    if not tess and not use_windows:
        raise OcrError("没有可用的 OCR 引擎。装上 Tesseract 5（含中文语言包），或换一台带 Windows OCR 的机器。")

    if progress:
        progress(0.02, "正在准备渲染组件…")
    ensure_renderer(progress)
    if progress:
        progress(0.12, "正在把页面渲染成图片…")

    with tempfile.TemporaryDirectory(prefix="mt-ocr-") as tmp:
        images = render_pages(str(source), tmp, scale=scale, limit=limit)
        if not images:
            raise OcrError("这个 PDF 没有可渲染的页面")
        if progress:
            progress(0.25, "正在识别 %d 页…" % len(images))
        pages = _ocr_with_tesseract(images, tess) if tess else _ocr_with_windows(images)

    lines = []
    failed = 0
    for item in pages:
        if item.get("error"):
            failed += 1
            continue
        lines.append(_tidy(item.get("text") or ""))
    text = "\n\n".join(x for x in lines if x)
    out_path = source.with_suffix(source.suffix + ".ocr.txt")
    out_path.write_text(text, encoding="utf-8")
    # 识别完立刻进检索库：下次问 AI 就能用上，不用等重建索引
    try:
        from . import ai as _ai
        _ai.index_path(str(out_path), "文献库", source.name)
    except Exception:
        pass
    if progress:
        progress(1.0, "识别完成：%d 页，%d 字" % (len(pages), len(text)))
    return {"ok": True, "text": text, "pages": len(pages), "failed": failed,
            "chars": len(text), "out": str(out_path),
            "engine": "tesseract" if tess else "windows"}
