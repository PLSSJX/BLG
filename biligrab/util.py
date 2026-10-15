"""小工具：文件名清洗、速度格式化等。"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path


def home_dir() -> Path | None:
    """稳健地取用户主目录：某些受控环境里 Path.home() 会直接抛异常。"""
    for name in ("USERPROFILE", "HOME"):
        value = os.environ.get(name)
        if value:
            try:
                return Path(value)
            except (OSError, ValueError):
                continue
    try:
        return Path.home()
    except (RuntimeError, OSError, ValueError):
        return None

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, fallback: str = "video", max_length: int = 120) -> str:
    """把标题清洗成 Windows 上合法的文件名。"""
    text = unicodedata.normalize("NFC", (name or "").strip())
    text = _ILLEGAL.sub("_", text)
    text = text.replace("\u3000", " ").strip(" .")
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _RESERVED:
        text = "_" + text
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text or fallback


def format_speed(bytes_per_sec: float) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "-"
    value = float(bytes_per_sec)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1024
    return "-"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds or seconds > 86400 * 7:
        return "-"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m:02d} 分"
    if m:
        return f"{m} 分 {s:02d} 秒"
    return f"{s} 秒"


def render_template(template: str, mapping: dict) -> str:
    """支持 {title} {bvid} {p} {quality} {up} 这类占位符。"""
    class _Safe(dict):
        def __missing__(self, key):
            return ""

    try:
        return template.format_map(_Safe(mapping))
    except (ValueError, IndexError, KeyError, AttributeError, TypeError):
        # 用户可能把模板写成 {title.foo} 这类非法占位符，
        # 这不该让每个任务都失败，退回标题即可。
        return mapping.get("title") or "video"
