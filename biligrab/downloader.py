"""分块多线程下载器：断点续传、实时速度、可取消。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import format_eta, format_speed

CHUNK_SIZE = 4 * 1024 * 1024
MIN_PARALLEL_SIZE = 2 * 1024 * 1024


class DownloadCancelled(Exception):
    pass


@dataclass
class Progress:
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: float | None = None
    percent: float = 0.0
    connections: int = 1
    phase: str = ""

    def to_dict(self) -> dict:
        return {
            "downloaded": self.downloaded,
            "total": self.total,
            "size_text": _human(self.downloaded),
            "total_text": _human(self.total),
            "percent": round(self.percent, 2),
            "speed": self.speed,
            "speed_text": format_speed(self.speed),
            "eta": self.eta,
            "eta_text": format_eta(self.eta),
            "connections": self.connections,
            "phase": self.phase,
        }


def _human(num: float) -> str:
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return "0 B"


class Downloader:
    """把一个大文件切成固定大小的块，多个线程各领一块并行下载。

    已经下完的块记录在同名的 .bgpart 文件里，中断后重跑会自动跳过。
    """

    def __init__(
        self,
        ctx,
        url: str,
        dest: Path,
        *,
        size: int = 0,
        threads: int = 8,
        referer: str | None = None,
        on_progress=None,
        cancel_event: threading.Event | None = None,
        label: str = "",
    ):
        self.ctx = ctx
        self.url = url
        self.dest = Path(dest)
        self.size = int(size or 0)
        self.threads = max(1, min(int(threads or 4), 32))
        self.referer = referer
        self.on_progress = on_progress
        self.cancel = cancel_event or threading.Event()   # 用户点"取消"
        self._abort = threading.Event()                   # 内部分片失败，自己中止
        self.label = label

        self._lock = threading.Lock()
        self._done_bytes = 0
        self._completed: set[int] = set()
        self._total_chunks = 0
        self._state_path = self.dest.with_suffix(self.dest.suffix + ".bgpart")
        self._active = 0
        self._error: Exception | None = None
        self._size_corrected: tuple[int, int] | None = None

    # ---------------- 状态文件 ----------------
    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("url") != self.url or int(data.get("size") or 0) != self.size:
            return  # 换了源，之前的分片作废
        self._completed = set(int(i) for i in data.get("done") or [])

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "url": self.url, "size": self.size,
                "done": sorted(self._completed),
            }), encoding="utf-8")
        except OSError:
            pass

    def _cleanup_state(self) -> None:
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ---------------- 主流程 ----------------
    def run(self) -> Path:
        # 同一个 Downloader 会被备用 CDN 重试复用，每次 run 都必须回到干净状态，
        # 否则上一次的失败会立刻把这一次也中止掉（备用地址一次都请求不到）。
        self._error = None
        self._abort.clear()
        self._done_bytes = 0
        self._completed = set()
        self.dest.parent.mkdir(parents=True, exist_ok=True)

        # 一律以服务端实测长度为准。上游给的 size 可能是接口估算值
        # （DASH 流只有码率没有字节数），拿它当文件长度会让分块错位。
        probed = self.ctx.net.content_length(self.url, referer=self.referer)
        if probed:
            if self.size and probed != self.size:
                self._size_corrected = (self.size, probed)
            self.size = probed

        ranges_ok = self.ctx.net.supports_range(self.url, referer=self.referer)

        if not self.size or not ranges_ok or self.size < MIN_PARALLEL_SIZE or self.threads == 1:
            return self._run_single_stream()

        return self._run_parallel()

    # ---------------- 单连接 ----------------
    def _run_single_stream(self) -> Path:
        """服务器不支持 Range 时的退化路径。

        这里必须流式写盘：早先的实现把整个响应读进内存再分片写，
        遇到几个 GB 的文件会直接把内存吃光。
        """
        tmp = self.dest.with_suffix(self.dest.suffix + ".bgtmp")
        downloaded = 0
        started = time.time()
        total = self.size
        with open(tmp, "wb") as fh:
            for block in self.ctx.net.stream(self.url, referer=self.referer, timeout=60):
                self._check_cancel()
                fh.write(block)
                downloaded += len(block)
                self._report(downloaded, total or downloaded, started, 1, "下载中")
        if total and downloaded != total:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise IOError(f"下载长度不符：期望 {total} 字节，实得 {downloaded} 字节")
        tmp.replace(self.dest)
        self._report(downloaded, total or downloaded, started, 1, "完成")
        return self.dest

    # ---------------- 多连接 ----------------
    def _run_parallel(self) -> Path:
        self._load_state()
        self._total_chunks = (self.size + CHUNK_SIZE - 1) // CHUNK_SIZE
        started = time.time()

        # 预分配到最终大小，各线程才能并发写不同偏移
        if not self.dest.exists() or self.dest.stat().st_size != self.size:
            with open(self.dest, "wb") as fh:
                fh.truncate(self.size)

        self._done_bytes = sum(
            min(CHUNK_SIZE, self.size - index * CHUNK_SIZE) for index in self._completed
        )

        pending = [i for i in range(self._total_chunks) if i not in self._completed]
        queue: list[int] = pending
        queue_lock = threading.Lock()

        connections = max(1, min(self.threads, len(pending))) if pending else 1
        self._report(self._done_bytes, self.size, started, connections, "下载中")

        def worker():
            while True:
                if self.cancel.is_set():
                    return
                with queue_lock:
                    if not queue:
                        return
                    index = queue.pop(0)
                try:
                    self._fetch_chunk(index)
                except DownloadCancelled:
                    return
                except DownloadCancelled:
                    return
                except Exception as exc:  # 任何一块失败都让整体失败，便于换备用地址重试
                    with self._lock:
                        self._error = exc
                    self._abort.set()
                    return

        threads = [threading.Thread(target=worker, daemon=True, name=f"dl-{i}")
                   for i in range(connections)]
        for thread in threads:
            thread.start()

        while any(thread.is_alive() for thread in threads):
            if self._abort.is_set() and not self.cancel.is_set():
                break                      # 已经失败，不必再等其它分片
            time.sleep(0.4)
            self._report(self._done_bytes, self.size, started, connections, "下载中")

        for thread in threads:
            thread.join(timeout=2)

        if self._error:
            raise self._error
        self._check_cancel()

        self._cleanup_state()
        self._report(self.size, self.size, started, connections, "完成")
        return self.dest

    def _fetch_chunk(self, index: int) -> None:
        start = index * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, self.size) - 1
        attempts = 0
        while True:
            self._check_cancel()
            attempts += 1
            try:
                res = self.ctx.net.request(
                    self.url, retries=2, referer=self.referer, timeout=60,
                    byte_range=(start, end),
                    headers={"Accept": "*/*"},
                )
                if res.status not in (200, 206):
                    raise IOError(f"HTTP {res.status}")
                body = res.body
                if res.status == 200:
                    # 服务器忽略了 Range，只能整段覆盖写（退化为串行语义）
                    body = body[start:end + 1]
                expected = end - start + 1
                if len(body) != expected:
                    raise IOError(f"分片长度不符：期望 {expected}，实得 {len(body)}")
                with open(self.dest, "r+b") as fh:
                    fh.seek(start)
                    fh.write(body)
                with self._lock:
                    if index not in self._completed:
                        self._completed.add(index)
                        self._done_bytes += expected
                        if len(self._completed) % 8 == 0:
                            self._save_state()
                return
            except DownloadCancelled:
                raise
            except Exception:
                if attempts >= 4:
                    raise
                time.sleep(0.5 * attempts)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise DownloadCancelled("用户已取消")
        if self._abort.is_set():
            raise DownloadCancelled("已有分片失败，本次下载中止")

    def _report(self, done: int, total: int, started: float, connections: int, phase: str) -> None:
        if not self.on_progress:
            return
        elapsed = max(0.001, time.time() - started)
        speed = done / elapsed if done else 0.0
        percent = (done / total * 100.0) if total else 0.0
        eta = ((total - done) / speed) if speed > 0 and total > done else (0.0 if total and done >= total else None)
        self.on_progress(Progress(
            downloaded=done, total=total, speed=speed, eta=eta,
            percent=min(percent, 100.0), connections=connections, phase=phase,
        ))
