"""把 B 站下发的 DASH 音视频分离流合并为一个可播放的 MP4。

优先调用 ffmpeg（如果本机装了）。没有 ffmpeg 时启用这里自带的纯 Python
fMP4 合并器：B 站的 .m4s 本身就是 CMAF 分片格式，音视频各自是
ftyp+moov 加一串 moof+mdat，把两条轨道的 moov 合成一个、再按解码时间
交错写出分片，就能得到一个标准 MP4 —— 不需要重新编码。
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

def _ffmpeg_candidates() -> list[Path]:
    from .config import BIN_DIR, DEPS_DIR, RESOURCE_DIR
    from .util import home_dir

    # 优先级：随包发布的 > 一键安装的 > 系统里已有的。
    # BIN_DIR 是 exe 同级的 bin/，用户想手动换一版 ffmpeg 直接丢进去就行。
    paths = [
        BIN_DIR / "ffmpeg.exe",
        BIN_DIR / "ffmpeg",
        DEPS_DIR / "bin" / "ffmpeg.exe",
        RESOURCE_DIR / "bin" / "ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
    ]
    home = home_dir()
    if home:
        paths += [
            home / "scoop" / "shims" / "ffmpeg.exe",
            home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
        ]
    return paths

_ffmpeg_cache: object = None


class MuxError(Exception):
    pass


def find_ffmpeg() -> str | None:
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache if isinstance(_ffmpeg_cache, str) else None
    found = shutil.which("ffmpeg")
    if not found:
        for candidate in _ffmpeg_candidates():
            try:
                if candidate.exists():
                    found = str(candidate)
                    break
            except OSError:
                continue
    _ffmpeg_cache = found or False
    return found


# ---------------------------------------------------------------- box 工具
def iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    """遍历内存里的 MP4 box，产出 (类型, 起始偏移, 总长度, 头部长度)。"""
    pos = start
    limit = len(data) if end is None else min(end, len(data))
    while pos + 8 <= limit:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        btype = data[pos + 4:pos + 8].decode("latin-1", "replace")
        header = 8
        if size == 1:
            if pos + 16 > limit:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = limit - pos
        if size < header or pos + size > limit:
            return
        yield btype, pos, size, header
        pos += size


def find_box(data: bytes, path: str, start: int = 0, end: int | None = None):
    """按 'trak/mdia/mdhd' 这样的路径逐层下钻，找第一个匹配的 box。

    约定：iter_boxes(data, start, end) 遍历的是 data[start:end] **里面**的盒子，
    所以从一个容器的头部进入它的内容区，就是 start + header。
    """
    limit = len(data) if end is None else min(end, len(data))
    node = None
    for name in path.split("/"):
        node = None
        for btype, pos, size, header in iter_boxes(data, start, limit):
            if btype == name:
                node = (pos, size, header)
                break
        if node is None:
            return None
        start = node[0] + node[2]     # 内容区起点 = 盒子起点 + 头部长度
        limit = node[0] + node[1]     # 内容区终点 = 盒子起点 + 盒子长度
    return node


def children_of(data: bytes, box) -> tuple[int, int]:
    """某个 box 的内容区 (start, end)。"""
    pos, size, header = box
    return pos + header, pos + size


def find_all(data: bytes, start: int, end: int, name: str):
    return [(pos, size, header) for btype, pos, size, header
            in iter_boxes(data, start, end) if btype == name]


def _u32(data: bytearray, offset: int) -> int:
    return struct.unpack(">I", bytes(data[offset:offset + 4]))[0]


def _set_u32(data: bytearray, offset: int, value: int) -> None:
    data[offset:offset + 4] = struct.pack(">I", value)


def box_track_id(data: bytearray, box_pos: int, box_header: int, box_type: str) -> int:
    """从 tkhd / trex / tfhd 里读出 track_ID 在文件里的绝对偏移。"""
    base = box_pos + box_header
    if box_type == "tfhd":
        return base + 4
    version = data[base]
    if box_type == "tkhd":
        return base + 4 + (16 if version == 1 else 8)
    if box_type == "trex":
        return base + 4
    raise MuxError(f"不支持从 {box_type} 读取 track_ID")


# ---------------------------------------------------------------- 初始化段
class InitSegment:
    """一个 DASH 流的 ftyp + moov。"""

    def __init__(self, raw: bytes, label: str):
        self.label = label
        self.ftyp = b""
        self.moov = b""       # 完整 moov（含头部）
        self.payload = b""    # moov 的内容区，所有解析都在它上面做
        for btype, pos, size, header in iter_boxes(raw):
            if btype == "ftyp":
                self.ftyp = raw[pos:pos + size]
            elif btype == "moov":
                self.moov = raw[pos:pos + size]
                self.payload = raw[pos + header:pos + size]
            elif btype in ("moof", "mdat", "sidx", "styp"):
                break
        if not self.payload:
            raise MuxError(f"{label} 里没有找到 moov，可能不是分片 MP4")

    # ---- 轨道信息 ----
    def trak(self) -> tuple[bytes, int, int]:
        """返回 (trak 原始字节, tkhd 在其中的偏移, tkhd 头部长度)。"""
        hit = find_box(self.payload, "trak")
        if not hit:
            raise MuxError(f"{self.label} 的 moov 里没有 trak")
        pos, size, _header = hit
        blob = self.payload[pos:pos + size]
        # tkhd 的偏移是相对 payload 的，换算成相对 trak 本身的偏移
        tkhd = find_box(self.payload, "trak/tkhd")
        if not tkhd:
            raise MuxError(f"{self.label} 的 trak 里没有 tkhd")
        return blob, box_track_id(bytearray(blob), tkhd[0] - pos, tkhd[2], "tkhd"), tkhd[2]

    def track_id(self) -> int:
        blob, offset, _header = self.trak()
        return struct.unpack(">I", blob[offset:offset + 4])[0]

    def timescale(self) -> int:
        """mdhd 的 timescale。注意 B 站 DASH 的 mdhd.duration 恒为 0，别拿它算时长。"""
        hit = find_box(self.payload, "trak/mdia/mdhd")
        if not hit:
            return 1000
        pos, _size, header = hit
        base = pos + header
        version = self.payload[base]
        offset = base + 4 + (16 if version == 1 else 8)
        return _u32(bytearray(self.payload), offset) or 1000

    def trex(self) -> bytes:
        hit = find_box(self.payload, "mvex/trex")
        if not hit:
            return b""
        pos, size, _header = hit
        return self.payload[pos:pos + size]

    def extra_moov_children(self, skip: set[str]) -> list[bytes]:
        return [self.payload[pos:pos + size]
                for btype, pos, size, _header in iter_boxes(self.payload)
                if btype not in skip]


def _patch_track_id(blob: bytes, offset: int, new_id: int) -> bytes:
    data = bytearray(blob)
    _set_u32(data, offset, new_id)
    return bytes(data)


# mvhd 里 next_track_ID 之前还有 rate(4) volume(2) reserved(2) reserved(8)
# matrix(36) pre_defined(24)，一共 76 字节。漏掉它就会把 next_track_ID 写到
# rate 字段上（画面宽高比/音量那一片），而真正的 next_track_ID 保持陈旧值。
_MVHD_TAIL_BYTES = 4 + 2 + 2 + 8 + 36 + 24


def _patch_mvhd_next_track(mvhd_box: bytes, next_id: int) -> bytes:
    """改写 mvhd 里的 next_track_ID（version 1 时时间字段更宽，偏移不同）。"""
    data = bytearray(mvhd_box)
    version = data[8]
    offset = (8 + 4 + (16 if version == 1 else 8) + 4 + (8 if version == 1 else 4)
              + _MVHD_TAIL_BYTES)
    if offset + 4 > len(data):
        return bytes(data)
    _set_u32(data, offset, next_id)
    return bytes(data)


def _make_box(btype: str, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + btype.encode("latin-1") + payload


# ---------------------------------------------------------------- 分片读取
class FragmentReader:
    """按需从 .m4s 里读出一个分片（moof+mdat），并把 track_ID 改写掉。"""

    def __init__(self, path: Path, timescale: int, patch_track: tuple[int, int] | None):
        self.path = Path(path)
        self.fh = open(self.path, "rb")
        self.timescale = timescale or 1000
        self.patch_track = patch_track
        self.finished = False
        self._skip_init()

    def _skip_init(self) -> None:
        while True:
            header = self.fh.read(8)
            if len(header) < 8:
                self.finished = True
                return
            size, btype = struct.unpack(">I4s", header)
            btype_s = btype.decode("latin-1", "replace")
            extra = 0
            if size == 1:
                size = struct.unpack(">Q", self.fh.read(8))[0]
                extra = 8
            elif size == 0:
                size = self.path.stat().st_size - self.fh.tell() + 8
            body = size - 8 - extra
            if btype_s in ("ftyp", "moov", "free", "skip"):
                self.fh.seek(body, 1)
                continue
            self.fh.seek(-(8 + extra), 1)
            return

    def next_fragment(self):
        """返回 (解码起始秒, [待写出的片段])；片段是 bytes 或 ('file', offset, size)。"""
        if self.finished:
            return None
        pieces: list = []
        start_time = None
        while True:
            pos = self.fh.tell()
            header = self.fh.read(8)
            if len(header) < 8:
                self.finished = True
                break
            size, btype = struct.unpack(">I4s", header)
            btype_s = btype.decode("latin-1", "replace")
            extra = 0
            prefix = header
            if size == 1:
                # 64 位长度：多出来的 8 字节是整个 box 的一部分，
                # 拼不回 blob 里的话写出去的 moof 就是残的。
                extension = self.fh.read(8)
                size = struct.unpack(">Q", extension)[0]
                extra = 8
                prefix = header + extension
            elif size == 0:
                size = self.path.stat().st_size - pos
            body_len = size - 8 - extra

            if btype_s == "moof":
                blob = bytearray(prefix + (self.fh.read(body_len) if body_len > 0 else b""))
                start_time = self._patch_moof(blob, pos)
                pieces.append(bytes(blob))
            elif btype_s == "mdat":
                self.fh.seek(0, 1)
                self.fh.seek(body_len, 1)
                pieces.append(("file", str(self.path), pos, size))
                if start_time is not None:
                    break
            elif btype_s in ("sidx", "styp", "free", "skip", "prft", "emsg"):
                self.fh.seek(body_len, 1)
                continue
            else:
                self.fh.seek(body_len, 1)
                continue
            if start_time is not None and any(p == "mdat" or (isinstance(p, tuple) and p[0] == "file") for p in pieces):
                break
        if not pieces:
            self.finished = True
            return None
        return (start_time if start_time is not None else 0.0), pieces

    def _patch_moof(self, blob: bytearray, file_pos: int) -> float:
        """改写 moof 里音频轨的 track_ID，并读出这个分片的解码起始时间。"""
        base_time = 0.0
        view = bytes(blob)
        moof = find_box(view, "moof")
        if not moof:
            return base_time
        for btype, pos, size, header in iter_boxes(view, moof[0] + moof[2], moof[0] + moof[1]):
            if btype != "traf":
                continue
            for inner, ipos, _isize, iheader in iter_boxes(view, pos + header, pos + size):
                data_start = ipos + iheader
                if inner == "tfhd":
                    flags = int.from_bytes(bytes(blob[data_start - 3:data_start]), "big")
                    track_id_offset = data_start + 4
                    if flags & 0x000001:
                        raise MuxError(
                            "该分片用了绝对 base_data_offset，搬移字节会让偏移失效；"
                            "装一个 ffmpeg 就能走重封装路径"
                        )
                    if self.patch_track:
                        old, new = self.patch_track
                        if _u32(blob, track_id_offset) == old:
                            _set_u32(blob, track_id_offset, new)
                elif inner == "tfdt":
                    version = blob[data_start]
                    if version == 1:
                        value = struct.unpack(">Q", bytes(blob[data_start + 4:data_start + 12]))[0]
                    else:
                        value = struct.unpack(">I", bytes(blob[data_start + 4:data_start + 8]))[0]
                    base_time = value / float(self.timescale or 1000)
        return base_time

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


def read_init(path: Path, limit: int = 4 * 1024 * 1024) -> InitSegment:
    with open(path, "rb") as fh:
        head = fh.read(limit)
    return InitSegment(head, Path(path).name)


def build_moov(video: InitSegment, audio: InitSegment) -> tuple[bytes, int, int]:
    """把两条轨道的 moov 合二为一，返回 (moov, 视频轨号, 音频轨号)。"""
    v_trak, v_track_off, _ = video.trak()
    a_trak, a_track_off, _ = audio.trak()
    v_id = struct.unpack(">I", v_trak[v_track_off:v_track_off + 4])[0]
    a_id = struct.unpack(">I", a_trak[a_track_off:a_track_off + 4])[0]

    # 音频轨只有在"和视频轨号不同"时才能沿用原号。B 站的流里视频轨是 2、
    # 音频轨是 1 的情况真实存在，这时 max() 会把音频也改成 2，两条 trak 撞号。
    new_audio_id = a_id if a_id != v_id else max(v_id, a_id) + 1
    a_trak = _patch_track_id(a_trak, a_track_off, new_audio_id)

    children = video.extra_moov_children({"trak", "mvex", "mvhd"})
    mvhd_hit = find_box(video.payload, "mvhd")
    mvhd = _patch_mvhd_next_track(
        video.payload[mvhd_hit[0]:mvhd_hit[0] + mvhd_hit[1]], max(v_id, new_audio_id) + 1
    ) if mvhd_hit else b""

    v_trex = video.trex()
    a_trex = audio.trex()
    if a_trex:
        blob = bytearray(a_trex)
        offset = box_track_id(blob, 0, 8, "trex")   # trex 的 track_ID 紧跟 version/flags
        _set_u32(blob, offset, new_audio_id)
        a_trex = bytes(blob)

    mvex_payload = b""
    # mehd（总时长）与 trep（轨道扩展属性）没有 track_ID，原样带过来即可；
    # trex 每条轨道一份，音频那份的 track_ID 必须跟着改号。
    for name in ("mehd", "trep"):
        hit = find_box(video.payload, f"mvex/{name}")
        if hit:
            mvex_payload += video.payload[hit[0]:hit[0] + hit[1]]
    mvex_payload += v_trex + a_trex
    mvex = _make_box("mvex", mvex_payload) if mvex_payload else b""

    payload = mvhd + v_trak + a_trak + mvex + b"".join(children)
    return _make_box("moov", payload), v_id, new_audio_id


def merge_streams(video_path, audio_path, out_path, progress=None) -> dict:
    """纯 Python 合并，输入两个 .m4s，输出一个 MP4。"""
    video_path, audio_path, out_path = Path(video_path), Path(audio_path), Path(out_path)
    video = read_init(video_path)
    audio = read_init(audio_path)
    moov, v_id, a_id = build_moov(video, audio)

    ftyp = video.ftyp or audio.ftyp or _make_box("ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41")

    v_reader = FragmentReader(video_path, video.timescale(), None)
    # 音频分片里的 track_ID 必须跟着改，否则播放器找不到对应的 trak
    a_reader = FragmentReader(audio_path, audio.timescale(), (audio.track_id(), a_id))

    total = video_path.stat().st_size + audio_path.stat().st_size
    written = 0
    fragments = 0
    audio_handles: dict[str, object] = {}

    def emit(handle, pieces):
        nonlocal written
        for piece in pieces:
            if isinstance(piece, bytes):
                handle.write(piece)
                written += len(piece)
            else:
                _tag, src, offset, size = piece
                fh = audio_handles.get(src)
                if fh is None:
                    fh = open(src, "rb")
                    audio_handles[src] = fh
                fh.seek(offset)
                remaining = size
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)

    try:
        with open(out_path, "wb") as out:
            out.write(ftyp)
            out.write(moov)
            v_next = v_reader.next_fragment()
            a_next = a_reader.next_fragment()
            while v_next or a_next:
                if v_next and (not a_next or v_next[0] <= a_next[0]):
                    emit(out, v_next[1])
                    v_next = v_reader.next_fragment()
                else:
                    emit(out, a_next[1])
                    a_next = a_reader.next_fragment()
                fragments += 1
                if progress and fragments % 8 == 0:
                    progress(min(0.99, written / total if total else 0.0))
        if progress:
            progress(1.0)
    finally:
        v_reader.close()
        a_reader.close()
        for fh in audio_handles.values():
            try:
                fh.close()  # type: ignore[union-attr]
            except OSError:
                pass

    size = out_path.stat().st_size
    if size < 4096:
        raise MuxError("合并结果异常偏小，可能源流不是标准 fMP4")
    return {
        "method": "builtin-fmp4",
        "size": size,
        "tracks": {"video": v_id, "audio": a_id},
        "fragments": fragments,
    }


def merge_with_ffmpeg(ffmpeg: str, video_path, audio_path, out_path, copy_audio: bool = True) -> dict:
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path), "-i", str(audio_path),
        "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
        "-movflags", "+faststart", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise MuxError("ffmpeg 合并失败：" + (detail[-1] if detail else "未知错误"))
    return {"method": "ffmpeg", "size": Path(out_path).stat().st_size,
            "tracks": {"video": 1, "audio": 2}, "fragments": 0}


def merge(video_path, audio_path, out_path, prefer_ffmpeg: bool = True, progress=None) -> dict:
    """合并入口：能用 ffmpeg 就用，否则走内置合并器。"""
    if prefer_ffmpeg:
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            try:
                return merge_with_ffmpeg(ffmpeg, video_path, audio_path, out_path)
            except (MuxError, OSError, subprocess.SubprocessError):
                pass  # 退回内置实现
    return merge_streams(video_path, audio_path, out_path, progress)


def describe(path) -> dict:
    """快速看一眼 MP4 的结构，用于合并后自检。"""
    path = Path(path)
    info = {"path": str(path), "size": path.stat().st_size if path.exists() else 0,
            "boxes": [], "fragmented": False, "duration_hint": None}
    if not path.exists():
        return info
    try:
        with open(path, "rb") as fh:
            head = fh.read(1 << 20)
            fh.seek(max(0, info["size"] - (1 << 20)))
            tail = fh.read()
        names = [btype for btype, _p, _s, _h in iter_boxes(head)]
        info["boxes"] = names[:24]
        info["fragmented"] = b"moof" in tail or "moof" in names
        moov = find_box(head, "moov")
        if moov:
            inner_start, inner_end = children_of(head, moov)
            traks = find_all(head, inner_start, inner_end, "trak")
            info["track_count"] = len(traks)
            mvhd = find_box(head, "mvhd", inner_start, inner_end)
            if mvhd:
                base = mvhd[0] + mvhd[2]
                version = head[base]
                ts_off = base + 4 + (16 if version == 1 else 8)
                timescale = struct.unpack(">I", head[ts_off:ts_off + 4])[0] or 1000
                if version == 1:
                    duration = struct.unpack(">Q", head[ts_off + 4:ts_off + 12])[0]
                else:
                    duration = struct.unpack(">I", head[ts_off + 4:ts_off + 8])[0]
                info["duration_hint"] = round(duration / timescale, 2)
    except (OSError, struct.error, IndexError):
        pass
    return info
