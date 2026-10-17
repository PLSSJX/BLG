"""音乐格式转换与标签写入：让用户自己挑目标格式，再导出一份。

上游（各平台的解密模块）交出来的通常是 flac / mp3 / ogg / m4a / wav，这里负责：

* available_targets()：列出能转成什么，并标注哪些必须要 ffmpeg，界面据此提示；
* convert()：真正导出。**没有 ffmpeg 也能干活** —— copy（保持原格式）以及「目标格式
  和源格式相同」都是纯 Python 的字节复制，只搬运、不碰音频数据；真要重新编码却没有
  ffmpeg 时如实抛错，绝不把 flac 改名成 .mp3 糊弄过去；
* write_tags()：自己实现 ID3v2.3（MP3，TIT2/TPE1/TALB/APIC）与 Vorbis Comment
  （FLAC，TITLE/ARTIST/ALBUM）+ PICTURE 块，不依赖 ffmpeg、不依赖第三方库。其它容器
  （ogg / m4a / wav）要写标签得重排 Ogg 页或重建 MP4 的 moov，改坏了不值当，一律
  返回 False 且一个字节都不动。

所有落盘统一走「同目录临时文件 → 结构自检 → 原子替换」：中途失败不会留下半截文件，
也不会破坏原文件；大文件全程流式搬运，不整份读进内存。
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
from pathlib import Path

from ..muxer import find_ffmpeg

__all__ = ["available_targets", "convert", "write_tags", "detect_format", "describe", "find_ffmpeg"]

_BUFFER = 1 << 20
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# 可选目标总表。needs_ffmpeg 表示「这个组合要重新编码」，源格式和它一样时不用编。
_ALLOWED_TARGETS: tuple[str, ...] = ("copy", "mp3", "flac", "m4a", "wav", "ogg", "opus")

_TARGET_SPEC: dict[str, dict] = {
    "copy": {"label": "保持原格式", "ext": None, "needs_ffmpeg": False,
             "note": "只做解密与标签，不重新编码"},
    "mp3": {"label": "MP3", "ext": ".mp3", "needs_ffmpeg": True,
            "note": "有损，兼容性最好（libmp3lame，默认 320k）"},
    "flac": {"label": "FLAC", "ext": ".flac", "needs_ffmpeg": True,
             "note": "无损压缩（flac，压缩级别 5）"},
    "m4a": {"label": "M4A（AAC）", "ext": ".m4a", "needs_ffmpeg": True,
            "note": "有损，苹果/移动端友好（aac，默认 256k）"},
    "wav": {"label": "WAV", "ext": ".wav", "needs_ffmpeg": True,
            "note": "无压缩 PCM，体积最大（pcm_s16le）"},
    "ogg": {"label": "OGG（Vorbis）", "ext": ".ogg", "needs_ffmpeg": True,
            "note": "有损（libvorbis，默认质量 6，约 192k）"},
    "opus": {"label": "Opus", "ext": ".opus", "needs_ffmpeg": True,
             "note": "有损，同码率音质最好（libopus，默认 128k）"},
}

# 编码器候选：第一个不行就换下一个（有些 ffmpeg 构建没编进 libopus/libvorbis）。
_CODECS: dict[str, list[list[str]]] = {
    "mp3": [["-c:a", "libmp3lame"], ["-c:a", "mp3"]],
    "flac": [["-c:a", "flac"]],
    "m4a": [["-c:a", "aac"]],
    "wav": [["-c:a", "pcm_s16le"]],
    "ogg": [["-c:a", "libvorbis"], ["-c:a", "vorbis", "-strict", "-2"]],
    "opus": [["-c:a", "libopus"], ["-c:a", "opus", "-strict", "-2"]],
}

# 显式指定封装格式，这样临时文件叫什么名字都不影响 ffmpeg 判断输出容器。
_MUXERS = {"mp3": "mp3", "flac": "flac", "m4a": "ipod", "wav": "wav", "ogg": "ogg", "opus": "opus"}
_DEFAULT_BITRATE = {"mp3": "320k", "m4a": "256k", "opus": "128k"}
_BITRATE_APPLIES = ("mp3", "m4a", "opus")     # 无损/固定码率的格式用不到比特率

_LOSSY = {"mp3", "m4a", "ogg", "opus", "aac", "wma"}

_FORMAT_ALIASES = {
    "mp3": "mp3", "mp2": "mp3",
    "flac": "flac",
    "wav": "wav", "wave": "wav",
    "m4a": "m4a", "mp4": "m4a", "m4b": "m4a", "aac": "m4a", "alac": "m4a",
    "ogg": "ogg", "oga": "ogg", "vorbis": "ogg",
    "opus": "opus",
}


# ------------------------------------------------------------------ 小工具
def _emit(progress, value: float) -> None:
    """进度回调不该把转码搞崩：callback 自己抛异常就当没听见。"""
    if not progress:
        return
    try:
        progress(max(0.0, min(1.0, float(value))))
    except Exception:
        pass


def _remove(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _unique_path(path: Path) -> Path:
    """同名文件已存在时换一个名字，绝不覆盖已有文件。"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / (stem + "-" + str(os.getpid()) + suffix)


def _tmp_path(out: Path) -> Path:
    return out.with_name(f"{out.stem}.part-{os.getpid()}{out.suffix}")


def _same_file(a: Path, b: Path) -> bool:
    try:
        if b.exists() and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except (OSError, ValueError):
        return False


def _norm_format(value) -> str:
    """把 'FLAC'、'.flac'、'song.flac' 之类统一成 'flac'；认不出就原样小写返回。"""
    text = str(value or "").strip().lower().strip(".")
    if "." in text:
        text = text.rsplit(".", 1)[1]
    return _FORMAT_ALIASES.get(text, text)


def _sniff(head: bytes) -> str:
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "opus" if b"OpusHead" in head else "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return ""


def detect_format(path) -> str:
    """认格式：先看文件头，认不出再退回扩展名。返回规范 id，认不出返回 ''。"""
    target = Path(path)
    head = b""
    try:
        with open(target, "rb") as fh:
            head = fh.read(64)
    except OSError:
        head = b""
    sniffed = _sniff(head)
    if sniffed:
        return sniffed
    return _norm_format(target.suffix[1:]) if target.suffix else ""


def _read_chunk(src, dst, count: int) -> int:
    """从 src 搬 count 字节到 dst，返回实际搬了多少（流式，固定 1MB 缓冲）。"""
    moved = 0
    remaining = count
    while remaining > 0:
        chunk = src.read(min(_BUFFER, remaining))
        if not chunk:
            break
        dst.write(chunk)
        moved += len(chunk)
        remaining -= len(chunk)
    return moved


# ------------------------------------------------------------------ 可选目标
def available_targets(source_format: str) -> list[dict]:
    """返回可选的目标格式，按 {'id','label','needs_ffmpeg','note'} 组织。

    needs_ffmpeg 表示「这个源转成这个目标要不要重新编码」：保持原格式、以及目标格式与
    源格式相同的组合永远是 False（纯 Python 复制即可）。本机没装 ffmpeg 时，需要它的
    目标会在 note 里额外写明，界面可以直接提示用户。
    """
    src = _norm_format(source_format)
    have_ffmpeg = find_ffmpeg() is not None
    items: list[dict] = []
    for tid in _ALLOWED_TARGETS:
        spec = _TARGET_SPEC[tid]
        same = tid != "copy" and bool(src) and tid == src
        needs = bool(spec["needs_ffmpeg"]) and not same
        if tid == "copy":
            note = spec["note"] + (f"（当前是 {src.upper()}）" if src else "")
        elif same:
            note = f"源文件已经是 {spec['label']}，直接复制，不需要 ffmpeg"
        else:
            note = spec["note"]
            if src and src in _LOSSY and tid in ("flac", "wav"):
                note += "；源本身是有损格式，转过去音质不会变好"
            if needs and not have_ffmpeg:
                note += "；本机没找到 ffmpeg，这个目标暂时用不了"
        items.append({"id": tid, "label": spec["label"], "needs_ffmpeg": needs, "note": note})
    return items


# ------------------------------------------------------------------ 转码
def _norm_target(target) -> str:
    text = _norm_format(target)
    if text not in _TARGET_SPEC:
        shown = str(target or "").strip() or "（空）"
        raise ValueError(f"不支持的目标格式：{shown}；可选 " + "、".join(_ALLOWED_TARGETS))
    return text


def _norm_bitrate(bitrate: str) -> str:
    """把 '320'、'320K'、'320k' 统一成 '320k'；明显不是比特率的写法直接报错。"""
    text = str(bitrate or "").strip().lower()
    if not text:
        return ""
    body = text[:-1] if text.endswith("k") else text
    if not body.isdigit() or not (2 <= len(body) <= 4):
        raise ValueError(f"比特率写法不对：{bitrate}；应该像 320k 或 192k")
    return f"{int(body)}k"


def convert(src, target: str, dest_dir=None, *, bitrate: str = "", progress=None) -> dict:
    """把 src 导出成 target 格式，返回 {'ok','output','format','transcoded','note'}。

    target 取 'copy' / 'mp3' / 'flac' / 'm4a' / 'wav' / 'ogg' / 'opus'，失败抛 ValueError，
    消息是给人看的。不用 ffmpeg 也能干活的部分：copy、以及目标格式与源格式相同的组合，
    都是纯 Python 的字节复制（只搬运，不重新编码）。
    """
    src_path = Path(src)
    if not src_path.exists():
        raise ValueError(f"源文件不存在：{src_path}")
    if not src_path.is_file():
        raise ValueError(f"源不是文件：{src_path}")

    tid = _norm_target(target)
    spec = _TARGET_SPEC[tid]
    fmt = detect_format(src_path)
    same = tid != "copy" and bool(fmt) and tid == fmt

    if tid == "copy":
        out_fmt = fmt or "bin"
        ext = src_path.suffix or ("." + out_fmt)
    elif same:
        out_fmt = fmt
        ext = spec["ext"]
    else:
        out_fmt = tid
        ext = spec["ext"]

    # 目录只在真要写文件时才建，免得转不了还在别人磁盘上建空目录
    out_dir = Path(dest_dir) if dest_dir else src_path.parent
    out_path = out_dir / (src_path.stem + ext)
    # 目标已存在且不是源文件本身时另起一个名字。
    # 以前这里是直接写 —— 用户目录里恰好有个同名的 歌.mp3 就会被静默覆盖。
    # decrypt.py 一直用 _unique_path，转码这条路漏了。
    if out_path.exists() and not _same_file(src_path, out_path):
        out_path = _unique_path(out_path)

    # ---------------------------------------------------------- 不重新编码
    if tid == "copy" or same:
        if _same_file(src_path, out_path):
            _emit(progress, 1.0)
            note = ("目标就是源文件本身，没有改动任何字节：" + spec["note"]) if tid == "copy" \
                else f"源文件已经是 {spec['label']}，直接用原文件，未重新编码"
            return {"ok": True, "output": src_path, "format": out_fmt,
                    "transcoded": False, "note": note}
        _ensure_dir(out_dir)
        _copy_stream(src_path, out_path, progress)
        if tid == "copy":
            note = spec["note"]
        else:
            note = f"源文件已经是 {spec['label']}，直接复制，没有调用 ffmpeg"
        if bitrate:
            note += "；没有重新编码，比特率参数已忽略"
        return {"ok": True, "output": out_path, "format": out_fmt,
                "transcoded": False, "note": note}

    # ---------------------------------------------------------- 需要重新编码
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ValueError(f"需要 ffmpeg 才能转成 {tid}：请先安装 ffmpeg，或改用「保持原格式」")
    want_bitrate = _norm_bitrate(bitrate)
    _ensure_dir(out_dir)

    tmp = _tmp_path(out_path)
    _remove(tmp)
    duration = _duration_hint(src_path, fmt)
    _emit(progress, 0.0)
    last_error = ""
    codecs = _CODECS[tid]
    for index, codec_args in enumerate(codecs):
        cmd = _ffmpeg_cmd(ffmpeg, src_path, tmp, tid, codec_args, want_bitrate)
        try:
            _run_ffmpeg(cmd, duration, progress)
            break
        except _FFmpegError as exc:
            last_error = exc.detail
            _remove(tmp)
            if index + 1 >= len(codecs):
                raise ValueError(f"用 ffmpeg 转成 {tid} 失败：{_last_line(last_error)}") from None
        except OSError as exc:
            _remove(tmp)
            raise ValueError(f"启动 ffmpeg 失败：{exc}") from None

    size = tmp.stat().st_size if tmp.exists() else 0
    if size < 64:
        _remove(tmp)
        raise ValueError(f"用 ffmpeg 转成 {tid} 失败：输出文件异常偏小（{size} 字节）")
    try:
        os.replace(tmp, out_path)
    except OSError as exc:
        _remove(tmp)
        raise ValueError(f"写入目标文件失败：{out_path}（{exc}）") from None

    _emit(progress, 1.0)
    note = f"已用 ffmpeg 重新编码为 {spec['label']}"
    if want_bitrate and tid in _BITRATE_APPLIES:
        note += f"（{want_bitrate}）"
    elif want_bitrate:
        note += f"；{spec['label']} 用不到比特率参数，已忽略 {want_bitrate}"
    return {"ok": True, "output": out_path, "format": tid, "transcoded": True, "note": note}


def _ensure_dir(out_dir: Path) -> None:
    try:
        if not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
        if not out_dir.is_dir():
            raise ValueError(f"目标目录不是文件夹：{out_dir}")
    except OSError as exc:
        raise ValueError(f"目标目录不可用：{out_dir}（{exc}）") from None


def _copy_stream(src: Path, dst: Path, progress=None) -> None:
    """流式复制 + 原子替换；不整份读进内存。"""
    tmp = _tmp_path(dst)
    _remove(tmp)
    try:
        total = src.stat().st_size
    except OSError as exc:
        raise ValueError(f"读不到源文件：{src}（{exc}）") from None
    moved = 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(_BUFFER)
                if not chunk:
                    break
                fo.write(chunk)
                moved += len(chunk)
                if total:
                    _emit(progress, moved / total)
    except OSError as exc:
        _remove(tmp)
        raise ValueError(f"复制失败：{src} → {dst}（{exc}）") from None
    if moved != total:
        _remove(tmp)
        raise ValueError(f"复制不完整：{src} 有 {total} 字节，只写入 {moved} 字节")
    try:
        os.replace(tmp, dst)
    except OSError as exc:
        _remove(tmp)
        raise ValueError(f"写入目标文件失败：{dst}（{exc}）") from None


class _FFmpegError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _last_line(detail: str) -> str:
    lines = [line.strip() for line in (detail or "").splitlines() if line.strip()]
    text = lines[-1] if lines else "ffmpeg 没有给出错误信息"
    return text[:300]


def _ffmpeg_cmd(ffmpeg: str, src: Path, tmp: Path, tid: str, codec_args: list[str],
                bitrate: str) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(src), "-vn", "-map_metadata", "0", *codec_args]
    if tid in _BITRATE_APPLIES:
        cmd += ["-b:a", bitrate or _DEFAULT_BITRATE[tid]]
    elif tid == "ogg":
        cmd += ["-q:a", "6"]
    elif tid == "flac":
        cmd += ["-compression_level", "5"]
    if tid == "m4a":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", _MUXERS[tid], "-progress", "pipe:1", "-nostats", str(tmp)]
    return cmd


def _run_ffmpeg(cmd: list[str], duration: float | None, progress) -> None:
    """跑 ffmpeg，顺便把 -progress 的进度喂给回调。stderr 落到临时文件，避免管道塞死。"""
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=errlog, creationflags=_NO_WINDOW)
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if duration and line.startswith(("out_time_us=", "out_time_ms=")):
                        value = line.split("=", 1)[1].strip()
                        if value.isdigit():
                            _emit(progress, int(value) / 1_000_000 / duration)
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        if code != 0:
            errlog.seek(0)
            raise _FFmpegError(errlog.read().decode("utf-8", "replace"))


# ------------------------------------------------------------------ 时长估算
_MP3_RATES = ([0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
              [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160])


def _duration_hint(path: Path, fmt: str) -> float | None:
    """估算时长（秒），只拿来算进度条；解析不出来就返回 None，不瞎猜。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if fmt == "flac":
                return _flac_duration(fh)
            if fmt == "wav":
                return _wav_duration(fh, size)
            if fmt in ("ogg", "opus"):
                return _ogg_duration(fh, size)
            if fmt == "mp3":
                return _mp3_duration(fh, size)
            if fmt == "m4a":
                return _m4a_duration(fh, size)
    except (OSError, struct.error, ValueError, IndexError):
        return None
    return None


def _flac_duration(fh) -> float | None:
    fh.seek(0)
    if fh.read(4) != b"fLaC":
        return None
    head = fh.read(4)
    if len(head) < 4 or (head[0] & 0x7F) != 0:
        return None
    info = fh.read(min(int.from_bytes(head[1:4], "big"), 34))
    if len(info) < 18:
        return None
    bits = int.from_bytes(info[10:18], "big")
    rate = bits >> 44
    total = bits & ((1 << 36) - 1)
    if rate <= 0 or total <= 0:
        return None
    return total / rate


def _wav_duration(fh, size: int) -> float | None:
    fh.seek(0)
    head = fh.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos, byte_rate, data_size = 12, 0, 0
    for _ in range(64):
        if pos + 8 > size:
            break
        fh.seek(pos)
        chunk = fh.read(8)
        if len(chunk) < 8:
            break
        cid = chunk[:4]
        csize = int.from_bytes(chunk[4:8], "little")
        if cid == b"fmt ":
            data = fh.read(16)
            if len(data) >= 12:
                byte_rate = int.from_bytes(data[8:12], "little")
        elif cid == b"data":
            data_size = csize
            if byte_rate:
                return data_size / byte_rate
        pos += 8 + csize + (csize & 1)
    return data_size / byte_rate if byte_rate and data_size else None


def _ogg_duration(fh, size: int) -> float | None:
    fh.seek(0)
    head = fh.read(256)
    if head[:4] != b"OggS":
        return None
    if b"OpusHead" in head:
        rate = 48000
    elif b"vorbis" in head:
        index = head.index(b"vorbis") + 12    # 包类型(1)+"vorbis"(6)+版本(4)+声道(1) 之后是采样率
        if index + 4 > len(head):
            return None
        rate = int.from_bytes(head[index:index + 4], "little")
    else:
        return None
    if rate <= 0:
        return None
    tail_size = min(size, 1 << 16)
    fh.seek(size - tail_size)
    tail = fh.read(tail_size)
    index = tail.rfind(b"OggS")
    while index >= 0:
        if index + 14 <= len(tail) and tail[index + 4] == 0:
            granule = int.from_bytes(tail[index + 6:index + 14], "little")
            if granule not in (0, 0xFFFFFFFFFFFFFFFF):
                return granule / rate
        index = tail.rfind(b"OggS", 0, index)
    return None


def _mp3_duration(fh, size: int) -> float | None:
    fh.seek(0)
    head = fh.read(10)
    offset = 0
    if head[:3] == b"ID3" and len(head) == 10:
        offset = 10 + _syncsafe_read(head[6:10])
    end = size
    if size - offset > 128:
        fh.seek(size - 128)
        if fh.read(3) == b"TAG":
            end = size - 128
    if end <= offset:
        return None
    fh.seek(offset)
    block = fh.read(4096)
    for i in range(len(block) - 4):
        if block[i] != 0xFF or (block[i + 1] & 0xE0) != 0xE0:
            continue
        version = (block[i + 1] >> 3) & 0x03
        layer = (block[i + 1] >> 1) & 0x03
        if version == 1 or layer != 1:      # 只看 Layer III
            continue
        index = block[i + 2] >> 4
        if index in (0, 15):
            continue
        rate = _MP3_RATES[0 if version == 3 else 1][index] * 1000
        if rate > 0:
            return (end - offset) * 8 / rate
    return None


def _m4a_duration(fh, size: int) -> float | None:
    chunk_size = 1 << 20
    pos, carry = 0, b""
    while pos < size:
        fh.seek(pos)
        chunk = fh.read(chunk_size)
        if not chunk:
            break
        data = carry + chunk
        index = data.find(b"mvhd")
        if index >= 0 and index + 32 <= len(data):
            body = data[index + 4:]
            if body[0] == 1:
                timescale = int.from_bytes(body[20:24], "big")
                duration = int.from_bytes(body[24:32], "big")
            else:
                timescale = int.from_bytes(body[12:16], "big")
                duration = int.from_bytes(body[16:20], "big")
            if timescale > 0 and duration > 0:
                return duration / timescale
        carry = data[-8:]
        pos += len(chunk)
    return None


# ------------------------------------------------------------------ 标签：MP3
def _syncsafe_read(data: bytes) -> int:
    return ((data[0] & 0x7F) << 21) | ((data[1] & 0x7F) << 14) | ((data[2] & 0x7F) << 7) | (data[3] & 0x7F)


def _syncsafe_write(value: int) -> bytes:
    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


_META_ALIASES = {
    "title": ("title", "name", "song"),
    "artist": ("artist", "singer"),
    "album": ("album",),
    "track": ("track", "tracknumber"),
    "date": ("date", "year"),
}

# 我们的字段 → ID3v2.3 帧 ID
_ID3_FRAMES = {"title": b"TIT2", "artist": b"TPE1", "album": b"TALB",
               "track": b"TRCK", "date": b"TYER"}

# 改写 MP3 标签时，旧标签里这些帧原样保留（只认 v2.3 里合法的，避免把非法帧搬过来）
_ID3_KEEP = {b"TIT2", b"TPE1", b"TALB", b"TRCK", b"TYER", b"TCON", b"TCOM", b"TEXT",
             b"TPUB", b"TBPM", b"TKEY", b"TLEN", b"TCOP", b"TPOS", b"TSSE", b"TENC",
             b"COMM", b"USLT", b"TXXX", b"APIC", b"WXXX"}


def _text_frame(frame_id: bytes, text: str) -> bytes:
    # v2.3 + UTF-16（带 BOM）：中文歌名最稳的写法
    payload = b"\x01" + text.encode("utf-16") + b"\x00\x00"
    return frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _apic_frame(cover: bytes, mime: str) -> bytes:
    # 文本编码 0x00 + MIME + 图片类型 0x03（正面封面）+ 空描述 + 图片数据
    payload = (b"\x00" + mime.encode("latin-1", "replace") + b"\x00"
               + b"\x03" + b"\x00" + cover)
    return b"APIC" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _mp3_audio_range(fh, size: int) -> tuple[int, int]:
    """跳过开头的 ID3v2 标签（可能连着好几个），并去掉结尾的 ID3v1。"""
    pos = 0
    while True:
        fh.seek(pos)
        head = fh.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            break
        tag_size = _syncsafe_read(head[6:10])
        if head[3] == 4 and (head[5] & 0x10):
            tag_size += 10                     # v2.4 的 footer
        if tag_size <= 0 or pos + 10 + tag_size > size:
            break
        pos += 10 + tag_size
    end = size
    if size - pos > 128:
        fh.seek(size - 128)
        if fh.read(3) == b"TAG":
            end = size - 128
    return pos, end


def _read_old_frames(fh, tag_size: int, major: int) -> dict[bytes, bytes]:
    """把旧标签里认识的帧整帧读出来（含它自己的编码），不认识的跳过。"""
    frames: dict[bytes, bytes] = {}
    if tag_size <= 0:
        return frames
    fh.seek(10)
    body = fh.read(tag_size)
    pos = 0
    while pos + 10 <= len(body):
        frame_id = body[pos:pos + 4]
        if frame_id == b"\x00\x00\x00\x00":     # 到 padding 了
            break
        if major >= 4:
            frame_size = _syncsafe_read(body[pos + 4:pos + 8])
        else:
            frame_size = int.from_bytes(body[pos + 4:pos + 8], "big")
        if frame_size <= 0 or pos + 10 + frame_size > len(body):
            break
        if frame_id in _ID3_KEEP and frame_id not in frames:
            frames[frame_id] = body[pos:pos + 10 + frame_size]
        pos += 10 + frame_size
    return frames


def _write_mp3(path: Path, fields: dict) -> bool:
    cover = fields.get("cover")
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        body_start, body_end = _mp3_audio_range(fh, size)
        if body_end - body_start <= 0:
            return False
        old: dict[bytes, bytes] = {}
        if body_start >= 10:
            fh.seek(0)
            head = fh.read(10)
            old = _read_old_frames(fh, _syncsafe_read(head[6:10]), head[3])

    overridden = set()
    frames: list[bytes] = []
    for key, frame_id in _ID3_FRAMES.items():
        value = fields.get(key)
        if value:
            frames.append(_text_frame(frame_id, value))
            overridden.add(frame_id)
    cover_ok = True
    if cover:
        if len(cover) > 200 * 1024 * 1024:
            cover_ok = False
        else:
            frames.append(_apic_frame(cover, fields.get("cover_mime") or _image_mime(cover)))
            overridden.add(b"APIC")
    for frame_id, raw in old.items():
        if frame_id not in overridden:
            frames.append(raw)

    body = b"".join(frames)
    if len(body) > 0x0FFFFFFF:
        return False
    tag = b"ID3\x03\x00\x00" + _syncsafe_write(len(body)) + body

    tmp = _tmp_path(path)
    _remove(tmp)
    try:
        with open(path, "rb") as fi, open(tmp, "wb") as fo:
            fo.write(tag)
            fi.seek(body_start)
            if _read_chunk(fi, fo, body_end - body_start) != body_end - body_start:
                raise OSError("音频数据读取不完整")
    except OSError:
        _remove(tmp)
        return False
    if not _verify_mp3(tmp):
        _remove(tmp)
        return False
    try:
        os.replace(tmp, path)
    except OSError:
        _remove(tmp)
        return False
    return cover_ok


def _verify_mp3(path: Path) -> bool:
    """自检：标签头合法、帧长度自洽、标签后面还有音频数据。"""
    with open(path, "rb") as fh:
        head = fh.read(10)
        if len(head) < 10 or head[:3] != b"ID3" or head[3] != 3:
            return False
        size = _syncsafe_read(head[6:10])
        body = fh.read(size)
        if len(body) != size:
            return False
        pos = 0
        while pos + 10 <= len(body):
            if body[pos:pos + 4] == b"\x00\x00\x00\x00":
                break
            frame_size = int.from_bytes(body[pos + 4:pos + 8], "big")
            if frame_size <= 0 or pos + 10 + frame_size > len(body):
                return False
            pos += 10 + frame_size
        return bool(fh.read(1))


# ------------------------------------------------------------------ 标签：FLAC
def _parse_vorbis_comment(data: bytes) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if len(data) < 8:
        return pairs
    pos = 4 + int.from_bytes(data[0:4], "little")
    if pos + 4 > len(data):
        return pairs
    count = int.from_bytes(data[pos:pos + 4], "little")
    pos += 4
    for _ in range(min(count, 4096)):
        if pos + 4 > len(data):
            break
        length = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if length <= 0 or pos + length > len(data):
            break
        item = data[pos:pos + length].decode("utf-8", "replace")
        pos += length
        if "=" in item:
            key, value = item.split("=", 1)
            pairs.append((key, value))
    return pairs


def _build_vorbis_comment(pairs: list[tuple[str, str]], vendor: bytes = b"Video-Grabing") -> bytes:
    """FLAC 的 Vorbis Comment 内部长度是小端（跟 Metadata Block 头的大端不一样）。"""
    out = bytearray()
    out += struct.pack("<I", len(vendor)) + vendor
    out += struct.pack("<I", len(pairs))
    for key, value in pairs:
        item = f"{key}={value}".encode("utf-8")
        out += struct.pack("<I", len(item)) + item
    return bytes(out)


def _image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "application/octet-stream"


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _image_info(data: bytes) -> tuple[str, int, int, int]:
    """图片的 (mime, 宽, 高, 位深)。FLAC 的 PICTURE 块要这几个数，认不出就填 0。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 26:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(data[25], 1)
        return ("image/png", int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"), data[24] * channels)
    if data[:2] == b"\xff\xd8":
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            if marker == 0xDA:                 # 进入扫描数据，后面不会再有 SOF
                break
            seg = int.from_bytes(data[pos + 2:pos + 4], "big")
            if marker in _SOF_MARKERS and pos + 9 <= len(data):
                return ("image/jpeg", int.from_bytes(data[pos + 7:pos + 9], "big"),
                        int.from_bytes(data[pos + 5:pos + 7], "big"), data[pos + 4] * data[pos + 9])
            if seg < 2:
                break
            pos += 2 + seg
        return ("image/jpeg", 0, 0, 0)
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return ("image/gif", int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"), 8)
    return (_image_mime(data), 0, 0, 0)


def _build_picture(cover: bytes, mime_hint: str = "") -> bytes | None:
    mime, width, height, depth = _image_info(cover)
    raw_mime = (mime_hint or mime).encode("latin-1", "replace")
    block = (struct.pack(">I", 3) + struct.pack(">I", len(raw_mime)) + raw_mime
             + struct.pack(">I", 0)
             + struct.pack(">IIII", width, height, depth, 0)
             + struct.pack(">I", len(cover)) + cover)
    return block if len(block) <= 0xFFFFFF else None


def _merge_comments(old: list[tuple[str, str]], fields: dict) -> list[tuple[str, str]]:
    """我们写的字段覆盖旧的，其余（TRACKNUMBER、DATE、ENCODER 等）原样留着。"""
    wanted = {"title": "TITLE", "artist": "ARTIST", "album": "ALBUM",
              "track": "TRACKNUMBER", "date": "DATE"}
    mine = {tag: fields[key] for key, tag in wanted.items() if fields.get(key)}
    pairs = [(key, value) for key, value in old if key.strip().upper() not in mine]
    pairs += list(mine.items())
    return pairs


def _write_flac(path: Path, fields: dict) -> bool:
    streaminfo = None
    others: list[tuple[int, bytes]] = []
    old_pairs: list[tuple[str, str]] = []
    with open(path, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return False
        while True:
            head = fh.read(4)
            if len(head) < 4:
                return False
            last = bool(head[0] & 0x80)
            block_type = head[0] & 0x7F
            length = int.from_bytes(head[1:4], "big")
            data = fh.read(length)
            if len(data) != length:
                return False
            if block_type == 0:
                streaminfo = data
            elif block_type == 4:
                old_pairs = _parse_vorbis_comment(data)
            elif block_type == 6:
                if not fields.get("cover"):
                    others.append((block_type, data))       # 没给新封面就留着旧的
            else:
                others.append((block_type, data))
            if last:
                break
        audio_start = fh.tell()
    if streaminfo is None or len(streaminfo) < 34:
        return False

    pairs = _merge_comments(old_pairs, fields)
    blocks: list[tuple[int, bytes]] = [(0, streaminfo)]
    if pairs:
        blocks.append((4, _build_vorbis_comment(pairs)))
    cover_ok = True
    cover = fields.get("cover")
    if cover:
        picture = _build_picture(cover, fields.get("cover_mime") or "")
        if picture is None:
            cover_ok = False
        else:
            blocks.append((6, picture))
    blocks += others
    for _, data in blocks:
        if len(data) > 0xFFFFFF:            # 块长度只有 24 位，塞不下就别写
            return False

    try:
        size = path.stat().st_size
    except OSError:
        return False
    tmp = _tmp_path(path)
    _remove(tmp)
    try:
        with open(path, "rb") as fi, open(tmp, "wb") as fo:
            fo.write(b"fLaC")
            for index, (block_type, data) in enumerate(blocks):
                flag = 0x80 if index == len(blocks) - 1 else 0x00     # last-block 只给最后一块
                fo.write(bytes([flag | block_type]) + len(data).to_bytes(3, "big"))
                fo.write(data)
            fi.seek(audio_start)
            if _read_chunk(fi, fo, size - audio_start) != size - audio_start:
                raise OSError("音频帧读取不完整")
    except OSError:
        _remove(tmp)
        return False
    if not _verify_flac(tmp):
        _remove(tmp)
        return False
    try:
        os.replace(tmp, path)
    except OSError:
        _remove(tmp)
        return False
    return cover_ok


def _verify_flac(path: Path) -> bool:
    """自检：fLaC 魔数、STREAMINFO 在最前、块链长度自洽、末尾有音频帧。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if fh.read(4) != b"fLaC":
                return False
            blocks = 0
            while True:
                head = fh.read(4)
                if len(head) < 4:
                    return False
                if blocks == 0 and (head[0] & 0x7F) != 0:
                    return False
                length = int.from_bytes(head[1:4], "big")
                data = fh.read(length)
                if len(data) != length:
                    return False
                blocks += 1
                if blocks > 4096:
                    return False
                if head[0] & 0x80:
                    break
            return blocks >= 1 and fh.tell() < size
    except OSError:
        return False


# ------------------------------------------------------------------ 对外入口
def _collect_fields(meta) -> dict:
    if not isinstance(meta, dict):
        return {}
    fields: dict = {}
    for key, aliases in _META_ALIASES.items():
        for alias in aliases:
            raw = meta.get(alias)
            if raw not in (None, ""):
                fields[key] = str(raw).strip()
                break
    cover = meta.get("cover")
    if isinstance(cover, dict):
        cover = cover.get("data") or cover.get("bytes")
    if isinstance(cover, (bytearray, memoryview)):
        cover = bytes(cover)
    if isinstance(cover, bytes) and cover:
        fields["cover"] = cover
        hint = meta.get("cover_mime") or meta.get("mime")
        if isinstance(hint, str) and hint:
            fields["cover_mime"] = hint
    return fields


def write_tags(path, meta: dict) -> bool:
    """把 title / artist / album / cover 写进音频文件的标签，失败返回 False，不抛异常。

    目前实现了 MP3（ID3v2.3：TIT2/TPE1/TALB/APIC，旧标签里认识的帧会保留）与 FLAC
    （Vorbis Comment 的 TITLE/ARTIST/ALBUM + PICTURE 块），其它格式一律返回 False 且
    一个字节都不动。返回 True 表示请求的字段全都写进去了；meta['cover'] 是图片字节
    （也接受 {'data': bytes}），可选 cover_mime 指定 MIME，另有可选 track / date。
    """
    try:
        target = Path(path)
        if not target.is_file():
            return False
        fields = _collect_fields(meta)
        if not fields:
            return False
        fmt = detect_format(target)
        if fmt == "mp3":
            return _write_mp3(target, fields)
        if fmt == "flac":
            return _write_flac(target, fields)
        return False
    except Exception:
        return False


def describe(path) -> dict:
    """看一眼文件结构、时长和标签，测试与排错用。"""
    target = Path(path)
    info = {"path": str(target), "size": target.stat().st_size if target.exists() else 0,
            "format": detect_format(target), "duration": None, "tags": {}, "cover": 0}
    if not info["size"]:
        return info
    fmt = info["format"]
    info["duration"] = _duration_hint(target, fmt)
    try:
        if fmt == "flac":
            with open(target, "rb") as fh:
                fh.seek(4)
                while True:
                    head = fh.read(4)
                    if len(head) < 4:
                        break
                    data = fh.read(int.from_bytes(head[1:4], "big"))
                    if (head[0] & 0x7F) == 4:
                        info["tags"] = dict(_parse_vorbis_comment(data))
                    elif (head[0] & 0x7F) == 6:
                        info["cover"] += 1
                    if head[0] & 0x80:
                        break
        elif fmt == "mp3":
            with open(target, "rb") as fh:
                head = fh.read(10)
                if head[:3] == b"ID3" and len(head) == 10:
                    body = fh.read(_syncsafe_read(head[6:10]))
                    pos = 0
                    while pos + 10 <= len(body):
                        frame_id = body[pos:pos + 4]
                        if frame_id == b"\x00\x00\x00\x00":
                            break
                        frame_size = int.from_bytes(body[pos + 4:pos + 8], "big")
                        if frame_size <= 0:
                            break
                        payload = body[pos + 10:pos + 10 + frame_size]
                        if frame_id == b"APIC":
                            info["cover"] += 1
                        elif payload[:1] == b"\x01" and len(payload) > 3:
                            info["tags"][frame_id.decode("latin-1")] = \
                                payload[1:].decode("utf-16", "replace").rstrip("\x00")
                        elif payload:
                            info["tags"][frame_id.decode("latin-1")] = \
                                payload[1:].decode("latin-1", "replace").rstrip("\x00")
                        pos += 10 + frame_size
    except (OSError, struct.error, ValueError, IndexError):
        pass
    return info
