"""HLS（m3u8）下载器。

芒果TV 这类平台只下发 HLS 分片，没有单文件也没有 DASH。分片本身是
未加密的 MPEG-TS 或者 fMP4，按顺序拼起来就是一个能播的文件，所以纯标准库
也能处理 —— 不需要第三方库。

如果某个平台的播放列表带 #EXT-X-KEY（AES-128 加密），标准库没有 AES 实现，
这里会明确报错而不是产出坏文件。
"""
from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

SEGMENT_SUFFIX_TS = ".ts"
SEGMENT_SUFFIX_MP4 = ".mp4"


class HlsError(Exception):
    pass


def is_hls(url: str, mime: str = "") -> bool:
    text = ((url or "") + " " + (mime or "")).lower()
    return ".m3u8" in text or "mpegurl" in text


def _absolute(base: str, uri: str) -> str:
    return urllib.parse.urljoin(base, uri.strip())


def parse_master(text: str, base: str) -> list[dict]:
    """主播放列表：把每个码率的子列表挑出来。"""
    variants = []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.split(":", 1)[1]
            height = 0
            bandwidth = 0
            match = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
            if match:
                height = int(match.group(1))
            match = re.search(r"BANDWIDTH=(\d+)", attrs)
            if match:
                bandwidth = int(match.group(1))
            pending = {"height": height, "bandwidth": bandwidth}
        elif not line.startswith("#") and pending is not None:
            pending["url"] = _absolute(base, line)
            variants.append(pending)
            pending = None
    return variants


def parse_media(text: str, base: str) -> dict:
    """媒体播放列表：分出初始化段、分片列表，并检查是否加密。"""
    init_url = ""
    segments = []
    encrypted = False
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY:"):
            if "METHOD=NONE" not in line.upper():
                encrypted = True
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_url = _absolute(base, match.group(1))
        elif line.startswith("#EXTINF:"):
            try:
                duration += float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pass
        elif not line.startswith("#"):
            segments.append(_absolute(base, line))
    return {"init": init_url, "segments": segments,
            "encrypted": encrypted, "duration": duration}


def _fetch_text(ctx, url, referer):
    result = ctx.net.request(url, referer=referer, retries=3, timeout=30,
                             headers={"Accept": "*/*"})
    return result.text()


def resolve_playlist(ctx, url: str, referer: str | None, prefer_height: int = 0) -> dict:
    """把可能是「主列表」的地址解析成真正的分片列表。"""
    text = _fetch_text(ctx, url, referer)
    if "#EXT-X-STREAM-INF" in text:
        variants = parse_master(text, url)
        if not variants:
            raise HlsError("主播放列表里没有可用的码率")
        if prefer_height:
            fit = [v for v in variants if v["height"] and v["height"] <= prefer_height]
            chosen = max(fit or variants, key=lambda v: v["height"] or 0)
        else:
            chosen = max(variants, key=lambda v: (v["height"], v["bandwidth"]))
        text = _fetch_text(ctx, chosen["url"], referer)
        url = chosen["url"]
    media = parse_media(text, url)
    if media["encrypted"]:
        raise HlsError("该播放列表是 AES-128 加密的，纯标准库无法解密；装上 ffmpeg 后可以用它下载")
    if not media["segments"]:
        raise HlsError("播放列表里没有分片")
    return media


def download(ctx, url: str, dest: Path, *, threads: int = 8,
             referer: str | None = None, on_progress=None,
             cancel_event: threading.Event | None = None,
             prefer_height: int = 0, label: str = "HLS") -> dict:
    """下载整个 HLS 流并拼成一个文件。返回统计信息。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    media = resolve_playlist(ctx, url, referer, prefer_height)

    items = ([("init", media["init"])] if media["init"] else []) + \
            [("seg", u) for u in media["segments"]]
    total = len(items)
    lock = threading.Lock()
    done = {"n": 0, "bytes": 0}
    started = time.time()
    work = tempfile.mkdtemp(prefix="hls-")
    errors: list[Exception] = []
    cancel = cancel_event or threading.Event()

    def report(phase: str) -> None:
        if not on_progress:
            return
        with lock:
            n, size = done["n"], done["bytes"]
        elapsed = max(0.001, time.time() - started)
        from .downloader import Progress
        percent = (n / total * 100.0) if total else 0.0
        on_progress(Progress(downloaded=size, total=0, speed=size / elapsed,
                             eta=None, percent=min(percent, 99.0),
                             connections=threads, phase="%s 分片 %d/%d" % (phase, n, total)))

    def worker(index: int, kind: str, target: str) -> None:
        if cancel.is_set() or errors:
            return
        path = Path(work) / ("%05d%s" % (index, SEGMENT_SUFFIX_MP4 if kind == "init" else SEGMENT_SUFFIX_TS))
        last = None
        for attempt in range(4):
            try:
                result = ctx.net.request(target, referer=referer, retries=2, timeout=45,
                                         headers={"Accept": "*/*"})
                if result.status not in (200, 206) or not result.body:
                    raise HlsError("HTTP %s" % result.status)
                path.write_bytes(result.body)
                with lock:
                    done["n"] += 1
                    done["bytes"] += len(result.body)
                report("下载中")
                return
            except Exception as exc:      # 单分片失败重试几次再放弃
                last = exc
                time.sleep(0.4 * (attempt + 1))
        errors.append(last or HlsError("分片下载失败"))

    queue_index = 0
    queue_lock = threading.Lock()

    def runner() -> None:
        nonlocal queue_index
        while True:
            if cancel.is_set() or errors:
                return
            with queue_lock:
                if queue_index >= total:
                    return
                index = queue_index
                queue_index += 1
            kind, target = items[index]
            worker(index, kind, target)

    pool = [threading.Thread(target=runner, daemon=True) for _ in range(max(1, min(threads, total)))]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()

    if errors:
        shutil.rmtree(work, ignore_errors=True)
        raise HlsError(str(errors[0])[:160])
    if cancel.is_set():
        shutil.rmtree(work, ignore_errors=True)
        raise HlsError("已取消")

    # 按顺序拼接。TS 分片直接接起来就是可播放的 mpegts。
    written = 0
    with open(dest, "wb") as out:
        for index in range(total):
            kind = items[index][0]
            suffix = SEGMENT_SUFFIX_MP4 if kind == "init" else SEGMENT_SUFFIX_TS
            part = Path(work) / ("%05d%s" % (index, suffix))
            if not part.exists():
                continue
            with open(part, "rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 20)
            written += part.stat().st_size
    shutil.rmtree(work, ignore_errors=True)

    if written < 1024:
        raise HlsError("拼接结果异常偏小，可能分片都下载失败了")

    if on_progress:
        from .downloader import Progress
        on_progress(Progress(downloaded=written, total=written, speed=0,
                             eta=0, percent=100.0, connections=threads,
                             phase=label + " 完成"))
    return {"segments": len(media["segments"]), "size": written,
            "duration": media["duration"],
            "container": "fmp4" if media["init"] else "mpegts"}
