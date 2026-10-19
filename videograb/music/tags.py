# -*- coding: utf-8 -*-
"""读音频文件自带的标签（作者 / 专辑 / 曲名）。

只用标准库。支持 ID3v2.2/2.3/2.4（mp3）与 FLAC 的 Vorbis 注释，
读不到就返回空字典，绝不抛异常 —— 列表页靠它显示小字，不能因为
某一个文件标签坏了就整页打不开。
"""
from __future__ import annotations

import struct
from pathlib import Path

# 只取这三样，封面（APIC）要写图片文件，这里不做
FRAMES = {
    "TPE1": "artist", "TP1": "artist", "ARTIST": "artist", "aART": "album_artist",
    "TALB": "album", "TAL": "album", "ALBUM": "album",
    "TIT2": "title", "TT2": "title", "TITLE": "title",
}


def _synchsafe(raw: bytes) -> int:
    """ID3 的长度字段是 7 位一字节，最高位恒为 0。"""
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


def _decode_text(payload: bytes) -> str:
    """ID3 文本帧：首字节是编码，后面才是文本。"""
    if not payload:
        return ""
    encoding = payload[0]
    body = payload[1:]
    try:
        if encoding == 0:
            text = body.decode("latin-1")
        elif encoding == 1:
            text = body.decode("utf-16")
        elif encoding == 2:
            text = body.decode("utf-16-be")
        else:
            text = body.decode("utf-8", "replace")
    except (UnicodeDecodeError, LookupError):
        text = body.decode("utf-8", "replace")
    return text.replace("\x00", "").strip()


def _read_id3v2(handle) -> dict:
    header = handle.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return {}
    major = header[3]
    size = _synchsafe(header[6:10])
    body = handle.read(size)
    out: dict = {}
    pos = 0
    id_len = 3 if major == 2 else 4
    head_len = 6 if major == 2 else 10
    while pos + head_len <= len(body):
        frame_id = body[pos:pos + id_len].decode("latin-1", "replace")
        if not frame_id.strip("\x00") or frame_id.startswith("\x00"):
            break
        if major == 2:
            frame_size = int.from_bytes(body[pos + 3:pos + 6], "big")
        elif major == 3:
            frame_size = int.from_bytes(body[pos + 4:pos + 8], "big")
        else:
            frame_size = _synchsafe(body[pos + 4:pos + 8])
        if frame_size <= 0 or pos + head_len + frame_size > len(body):
            break
        key = FRAMES.get(frame_id)
        if key:
            text = _decode_text(body[pos + head_len:pos + head_len + frame_size])
            if text and key not in out:
                out[key] = text
        pos += head_len + frame_size
    return out


def _read_id3v1(handle) -> dict:
    """老式 ID3v1：文件末尾固定 128 字节。"""
    try:
        handle.seek(-128, 2)
        block = handle.read(128)
    except OSError:
        return {}
    if len(block) < 128 or block[:3] != b"TAG":
        return {}
    out = {}
    title = block[3:33].split(b"\x00")[0]
    artist = block[33:63].split(b"\x00")[0]
    album = block[63:93].split(b"\x00")[0]
    for key, raw in (("title", title), ("artist", artist), ("album", album)):
        text = raw.decode("gbk", "replace").strip() or raw.decode("latin-1", "replace").strip()
        if text:
            out[key] = text
    return out


def _read_flac(handle) -> dict:
    """FLAC：'fLaC' 后面是一串元数据块，第 4 块是 Vorbis 注释。"""
    header = handle.read(4)
    if header != b"fLaC":
        return {}
    out: dict = {}
    for _ in range(32):
        block_head = handle.read(4)
        if len(block_head) < 4:
            break
        last = block_head[0] & 0x80
        block_type = block_head[0] & 0x7F
        length = int.from_bytes(block_head[1:4], "big")
        data = handle.read(length)
        if block_type == 4:
            try:
                pos = 4 + int.from_bytes(data[0:4], "little")
                count = int.from_bytes(data[pos - 4:pos], "little")
                for _ in range(count):
                    size = int.from_bytes(data[pos:pos + 4], "little")
                    pos += 4
                    entry = data[pos:pos + size].decode("utf-8", "replace")
                    pos += size
                    if "=" in entry:
                        k, v = entry.split("=", 1)
                        key = FRAMES.get(k.upper())
                        if key and v.strip() and key not in out:
                            out[key] = v.strip()
            except (IndexError, ValueError):
                pass
        if last:
            break
    return out


def read_tags(path) -> dict:
    """读标签，返回 {title, artist, album}（可能为空）。任何异常都吞掉。"""
    p = Path(path)
    try:
        if not p.is_file():
            return {}
    except OSError:
        return {}
    try:
        with open(p, "rb") as fh:
            tags = _read_id3v2(fh) or _read_flac(fh)
            if not tags:
                tags = _read_id3v1(fh)
    except (OSError, ValueError, struct.error):
        return {}
    return {k: v for k, v in tags.items() if v and k in ("title", "artist", "album")}
