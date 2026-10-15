"""附加产物：弹幕、封面、字幕、NFO 元数据。"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

DANMAKU_XML = "https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"
PLAYER_V2 = "https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"


# ---------------------------------------------------------------- 弹幕
def fetch_danmaku(ctx, cid: int) -> str:
    """取 XML 弹幕。接口用的是 deflate 压缩的 XML，net 层已经解好压。"""
    res = ctx.net.request(
        DANMAKU_XML.format(cid=cid), retries=3,
        referer="https://www.bilibili.com/",
    )
    text = res.text()
    if "<d " not in text and "<i>" not in text:
        raise IOError("弹幕接口没有返回 XML")
    return text


def save_danmaku(ctx, cid: int, path: Path) -> int:
    xml = fetch_danmaku(ctx, cid)
    path.write_text(xml, encoding="utf-8")
    try:
        root = ET.fromstring(xml)
        return len(root.findall("d"))
    except ET.ParseError:
        return xml.count("<d ")


def danmaku_to_ass(xml_text: str, path: Path, title: str = "", width: int = 1920, height: int = 1080) -> int:
    """顺手把 XML 弹幕转成 ASS 字幕，方便丢进任何播放器。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return 0
    items = []
    for node in root.findall("d"):
        try:
            params = (node.get("p") or "").split(",")
            start = float(params[0])
            mode = int(params[1])
            size = int(params[2]) if len(params) > 2 else 25
            color = int(params[3]) if len(params) > 3 else 16777215
        except (ValueError, IndexError):
            continue
        if mode not in (1, 4, 5):
            continue
        text = (node.text or "").replace("\n", " ").replace("{", "(").replace("}", ")")
        if not text.strip():
            continue
        items.append((start, mode, size, color, text))

    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
WrapStyle: 2
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,Microsoft YaHei,{max(18, height // 45)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,1,8,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    rows = 14
    for idx, (start, mode, size, color, text) in enumerate(sorted(items)):
        h, m, s = int(start // 3600), int(start % 3600 // 60), start % 60
        ts = f"{h:d}:{m:02d}:{s:05.2f}"
        if mode == 5:  # 顶部固定
            end = start + 4
            y = 8 + (idx % 3) * 6
        elif mode == 4:  # 底部固定
            end = start + 4
            y = 88 - (idx % 3) * 6
        else:  # 滚动
            duration = 8.0
            end = start + duration
            y = 6 + (idx % rows) * 6
        e_h, e_m, e_s = int(end // 3600), int(end % 3600 // 60), end % 60
        te = f"{e_h:d}:{e_m:02d}:{e_s:05.2f}"
        bgr = ((color & 0xFF) << 16) | (color & 0xFF00) | ((color >> 16) & 0xFF)
        ass_color = f"&H00{bgr:06X}"
        effect = r"{\move(%d,%d,%d,%d)}" % (width + 200, int(height * y / 100),
                                            -len(text) * 20, int(height * y / 100)) if mode == 1 else ""
        align = 8 if mode == 1 else (8 if mode == 5 else 2)
        lines.append(
            f"Dialogue: 0,{ts},{te},Danmaku,,0,0,0,,"
            + r"{\c" + ass_color + r"}" + effect + text
        )
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# ---------------------------------------------------------------- 封面
def save_cover(ctx, url: str, path: Path, width: int = 1920) -> int:
    if not url:
        raise IOError("没有封面地址")
    sep = "&" if "?" in url else "?"
    target = f"{url}{sep}width={width}&height={int(width * 9 / 16)}"
    try:
        res = ctx.net.request(target, retries=2, referer="https://www.bilibili.com/")
    except Exception:
        res = ctx.net.request(url, retries=2, referer="https://www.bilibili.com/")
    path.write_bytes(res.body)
    return len(res.body)


# ---------------------------------------------------------------- 字幕
def fetch_subtitles(ctx, bvid: str, cid: int) -> list[dict]:
    try:
        data = ctx.net.api(PLAYER_V2.format(bvid=bvid, cid=cid),
                           referer=f"https://www.bilibili.com/video/{bvid}")
    except Exception:
        return []
    subtitle = ((data.get("subtitle") or {}).get("subtitles")) or []
    out = []
    for item in subtitle:
        url = item.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            continue
        try:
            payload = ctx.net.get_json(url)
        except Exception:
            continue
        body = payload.get("body") or []
        if body:
            out.append({"lan": item.get("lan_doc") or item.get("lan") or "字幕", "body": body})
    return out


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def save_subtitles(ctx, bvid: str, cid: int, directory: Path, stem: str) -> list[str]:
    written = []
    for track in fetch_subtitles(ctx, bvid, cid):
        lang = re.sub(r"[^\w\u4e00-\u9fff]+", "_", track["lan"]).strip("_") or "sub"
        path = directory / f"{stem}.{lang}.srt"
        lines = []
        for idx, row in enumerate(track["body"], start=1):
            lines.append(str(idx))
            lines.append(f"{_srt_time(float(row.get('from', 0)))} --> {_srt_time(float(row.get('to', 0)))}")
            lines.append(str(row.get("content", "")).strip())
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(str(path))
    return written


# ---------------------------------------------------------------- NFO
def _xml_text(node, tag: str, value) -> None:
    child = ET.SubElement(node, tag)
    child.text = "" if value is None else str(value)


def save_nfo(info, page, path: Path, quality: str = "", engine: str = "") -> None:
    root = ET.Element("episodedetails")
    _xml_text(root, "title", page.part or info.title)
    _xml_text(root, "showtitle", info.title)
    _xml_text(root, "plot", info.desc)
    _xml_text(root, "season", 1)
    _xml_text(root, "episode", page.page)
    _xml_text(root, "runtime", page.duration // 60 if page.duration else 0)
    _xml_text(root, "premiered", info.pubdate)
    _xml_text(root, "studio", info.owner.name)
    _xml_text(root, "bvid", info.bvid)
    _xml_text(root, "cid", page.cid)
    _xml_text(root, "quality", quality)
    _xml_text(root, "source_engine", engine)
    _xml_text(root, "website", f"https://www.bilibili.com/video/{info.bvid}")
    if info.cover:
        _xml_text(root, "thumb", info.cover)
    for key, value in (info.stat or {}).items():
        _xml_text(root, f"stat_{key}", value)
    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass
    tree.write(path, encoding="utf-8", xml_declaration=True)


def ext_from_url(url: str, default: str = ".mp4") -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in (".mp4", ".m4s", ".flv", ".mkv", ".m4a", ".aac"):
        return suffix
    return default
