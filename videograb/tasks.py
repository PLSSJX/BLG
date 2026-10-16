"""下载任务管理：排队、并发、进度、取消、重试。"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import extras
from .config import ALL_ENGINES, DATA_DIR
from .downloader import DownloadCancelled, Downloader
from .muxer import MuxError, find_ffmpeg, merge
from .quality import (codec_rank, codec_text, label as qn_label,
                      short as qn_short)
from .refs import Ref, parse_ref, resolve_short
from .util import render_template, safe_filename

# 任务历史落盘，重启后仍在；只保留最近这么多条
TASKS_PATH = DATA_DIR / "tasks.json"
MAX_HISTORY = 300

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_MERGING = "merging"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

MODES = [
    {"id": "auto", "name": "自动（最高画质 + 自动合并）",
     "desc": "拿到 DASH 就下分离流再合并，只有单文件可用时直接用单文件。"},
    {"id": "single", "name": "单文件 MP4（免合并）",
     "desc": "优先走 TV / 经典 durl 接口，拿到已经封装好音轨的 MP4，下完即可播放。"},
    {"id": "dash", "name": "强制 DASH 分离流",
     "desc": "只要音视频分离流，画质控制最细，合并用 ffmpeg 或内置合并器。"},
    {"id": "audio", "name": "仅音频",
     "desc": "只下最小体积的音轨，用来听歌或做字幕。"},
    {"id": "meta", "name": "仅元数据 / 封面 / 弹幕",
     "desc": "不下载视频本体，只收集封面、弹幕、字幕和 NFO 信息。"},
]


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    raw_input: str = ""
    site: str = ""           # 命中的平台 id
    kind: str = "download"   # download | convert —— convert 是本地文件解密/转格式
    local_path: str = ""     # convert 任务的源文件
    target: str = "copy"     # convert 任务的目标格式
    bitrate: str = ""        # convert 任务的码率（可选）
    page: int = 1            # 分 P 序号；多 P 批量下载时每个任务各占一 P
    engine: str = "auto"
    engine_used: str = ""
    qn: int = 0
    mode: str = "auto"
    status: str = STATUS_QUEUED
    title: str = ""
    cover: str = ""
    up: str = ""
    page_title: str = ""
    directory: str = ""
    progress: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    error: str = ""
    log: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def note(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {message}")
        if len(self.log) > 200:
            del self.log[:50]

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """从持久化的 JSON 还原。派生字段会被自动忽略。"""
        valid = {f.name for f in dataclasses.fields(cls)} - {"cancel_event"}
        return cls(**{k: v for k, v in (data or {}).items() if k in valid})

    def to_dict(self) -> dict:
        total = self.progress.get("total") or 0
        return {
            "id": self.id,
            "raw_input": self.raw_input,
            "site": self.site,
            "kind": self.kind,
            "local_path": self.local_path,
            "target": self.target,
            "page": self.page,
            "engine": self.engine,
            "engine_used": self.engine_used,
            "qn": self.qn,
            "qn_label": qn_label(self.qn) if self.qn else "",
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "page_title": self.page_title,
            "cover": self.cover,
            "up": self.up,
            "directory": self.directory,
            "progress": self.progress,
            "percent": self.progress.get("percent", 0.0),
            "speed_text": self.progress.get("speed_text", "-"),
            "eta_text": self.progress.get("eta_text", "-"),
            "size_text": self.progress.get("size_text", "0 B"),
            "total_text": self.progress.get("total_text", "0 B"),
            "has_total": bool(total),
            "files": self.files,
            "error": self.error,
            "log": self.log[-40:],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at else 0,
        }


class TaskManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._running: set[str] = set()
        self._stopped = False
        self._dispatcher_started = False
        self._save_lock = threading.Lock()
        self._dirty = False      # 有未落盘的状态变更时置位，由调度线程批量写入
        self._dispatch_error = ""   # 调度线程最近一次异常，供界面显示
        self._load_history()

    # ---------------- 历史持久化 ----------------
    def _load_history(self) -> None:
        """读回上次的任务记录。上次没跑完的一律标成"已中断"，可一键重试。"""
        if not TASKS_PATH.exists():
            return
        try:
            raw = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = raw.get("tasks") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for item in items[:MAX_HISTORY]:
            if not isinstance(item, dict):
                continue
            try:
                task = Task.from_dict(item)
            except Exception:
                # 单条记录坏掉不该让整个程序起不来
                continue
            if task.status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_MERGING):
                task.status = STATUS_CANCELLED
                task.note("上次退出时任务还没跑完，已标记为中断，点「重试」可以继续")
            self._tasks[task.id] = task
            self._order.append(task.id)

    def _save_history(self) -> None:
        # 快照在状态锁内取，落盘在独立的写锁内做：原先两处都用同一把锁，
        # 多个线程会往同一个 .tmp 名字上写再各自 replace，文件可能变成半截 JSON。
        with self._lock:
            items = [self._tasks[i].to_dict() for i in self._order
                     if i in self._tasks][:MAX_HISTORY]
            self._dirty = False
        payload = json.dumps({"tasks": items}, ensure_ascii=False, indent=1)
        with self._save_lock:
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                handle, name = tempfile.mkstemp(dir=str(DATA_DIR), prefix="tasks-", suffix=".tmp")
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(name, TASKS_PATH)
            except OSError:
                pass

    # ---------------- 对外接口 ----------------
    def submit_convert(self, paths: list, *, target: str = "copy",
                       bitrate: str = "") -> list:
        """为一批本地文件各建一个转换任务。"""
        from pathlib import Path as _Path

        created = []
        for raw in (paths or [])[:200]:
            path = _Path(str(raw))
            task = Task(
                raw_input=path.name,
                kind="convert",
                local_path=str(path),
                target=target or "copy",
                bitrate=bitrate or "",
                title=path.stem,
                mode="convert",
            )
            with self._lock:
                self._tasks[task.id] = task
                self._order.insert(0, task.id)
            created.append(task)
        if created:
            self._dirty = True
            self._save_history()
            self._ensure_dispatcher()
            with self._wake:
                self._wake.notify_all()
        return created

    def submit(self, raw_input: str, *, engine: str = "auto", qn: int = 0,
               mode: str = "auto", page: int = 1) -> Task:
        task = Task(raw_input=raw_input.strip(), engine=engine or "auto",
                    qn=int(qn or 0), mode=mode or "auto",
                    page=max(1, int(page or 1)))
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._wake.notify_all()
        self._save_history()
        self._ensure_dispatcher()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[dict]:
        with self._lock:
            items = [self._tasks[i] for i in self._order if i in self._tasks]
        items.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in items]

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if not task:
            return False
        task.cancel_event.set()
        if task.status == STATUS_QUEUED:
            task.status = STATUS_CANCELLED
            task.note("已取消（尚未开始）")
        return True

    def remove(self, task_id: str, delete_files: bool = False) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.cancel_event.set()
            if task.status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_MERGING):
                return False
            del self._tasks[task_id]
            if task_id in self._order:
                self._order.remove(task_id)
        self._save_history()
        if delete_files and task.directory:
            shutil.rmtree(task.directory, ignore_errors=True)
        return True

    def retry(self, task_id: str) -> Task | None:
        old = self.get(task_id)
        if not old:
            return None
        # 转换任务不能走 submit()：它的 raw_input 只是文件名，
        # 而且 mode="convert" 不是合法的下载模式。以前重试会变成一个
        # 去解析"歌名.ncm"的下载任务，用户的转换意图直接丢了。
        if old.kind == "convert":
            created = self.submit_convert([old.local_path],
                                          target=old.target or "copy",
                                          bitrate=old.bitrate or "")
            return created[0] if created else None
        return self.submit(old.raw_input, engine=old.engine, qn=old.qn,
                           mode=old.mode, page=old.page)

    def clear_finished(self) -> int:
        with self._lock:
            done = [i for i in self._order
                    if self._tasks.get(i) and self._tasks[i].status
                    in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)]
            for i in done:
                self._tasks.pop(i, None)
                self._order.remove(i)
        self._save_history()
        return len(done)

    def stats(self) -> dict:
        with self._lock:
            items = [self._tasks[i] for i in self._order if i in self._tasks]
        return {
            "total": len(items),
            "running": sum(1 for t in items if t.status in (STATUS_RUNNING, STATUS_MERGING)),
            "queued": sum(1 for t in items if t.status == STATUS_QUEUED),
            "done": sum(1 for t in items if t.status == STATUS_DONE),
            "error": sum(1 for t in items if t.status == STATUS_ERROR),
            "ffmpeg": find_ffmpeg() or "",
            # 调度线程出问题时界面能立刻看出来，而不是干等队列不动
            "dispatch_error": self._dispatch_error,
        }

    # ---------------- 调度 ----------------
    def _ensure_dispatcher(self) -> None:
        """调度线程只能有一个。

        之前每 submit 一次就新开一个永不退出的调度线程，提交 N 个任务就多出
        N 个 5Hz 空转线程，而且它们会同时抢同一批空槽位，实际并发超过上限。
        """
        with self._lock:
            if self._stopped or self._dispatcher_started:
                return
            self._dispatcher_started = True
        threading.Thread(target=self._dispatch_loop, daemon=True,
                         name="task-dispatch").start()

    def _dispatch_loop(self) -> None:
        # 常驻一个调度线程，按 max_parallel_tasks 开启任务
        while not self._stopped:
            with self._wake:
                if not any(
                    t.status == STATUS_QUEUED for t in self._tasks.values()
                ):
                    self._wake.wait(timeout=1.0)
            try:
                self._pump()
            except Exception as exc:
                # 以前这里是完全静默的：配置脏了会导致队列只涨不跑、
                # 界面上却没有任何线索。至少要让人看得见。
                self._dispatch_error = str(exc)[:200]
                time.sleep(0.5)
            time.sleep(0.2)

    def _pump(self) -> None:
        try:
            limit = int(self.ctx.settings.max_parallel_tasks)
        except (TypeError, ValueError):
            limit = 2
        limit = max(1, min(8, limit))
        with self._lock:
            queued = [self._tasks[i] for i in self._order
                      if self._tasks.get(i) and self._tasks[i].status == STATUS_QUEUED]
            slots = max(0, limit - len(self._running))
            # 占槽必须在锁内完成，否则多个调度者会抢到同一个空位
            picked = queued[:slots]
            for task in picked:
                self._running.add(task.id)
                task.status = STATUS_RUNNING
                task.started_at = time.time()
        if self._dirty:
            self._save_history()
        for task in picked:
            threading.Thread(target=self._run_task, args=(task,), daemon=True,
                             name=f"task-{task.id}").start()

    # ---------------- 单个任务 ----------------
    def _run_task(self, task: Task) -> None:
        try:
            self._execute(task)
            if task.status != STATUS_CANCELLED:
                task.status = STATUS_DONE
                task.note("全部完成")
        except DownloadCancelled:
            task.status = STATUS_CANCELLED
            task.note("已取消")
        except Exception as exc:
            task.status = STATUS_ERROR
            task.error = str(exc)
            task.note(f"失败：{exc}")
        finally:
            task.finished_at = time.time()
            with self._lock:
                self._running.discard(task.id)
            self._save_history()
            with self._wake:
                self._wake.notify_all()

    def _execute(self, task: Task) -> None:
        ctx = self.ctx
        settings = ctx.settings
        if task.kind == "convert":
            self._execute_convert(task)
            return
        task.note(f"开始解析：{task.raw_input}")

        # 先判断这是哪个平台的链接，再交给对应适配器
        from .sites import get_site, parse_any

        ref = parse_any(task.raw_input)
        site = get_site(ref.site)
        ref = site.normalize(ctx, ref)
        # 任务自己带的分 P 号优先于输入串里可能存在的 ?p=
        if task.page and task.page > 0:
            ref.p = int(task.page)
        task.site = ref.site
        task.note(f"平台：{site.name}")

        info = None
        play = None
        errors: list[str] = []
        candidates: list[tuple] = []

        # 非 B 站平台没有"多通道"这回事，直接用平台适配器拿结果；
        # 拿到之后下面的下载流程与 B 站完全共用。
        if ref.site != "bilibili":
            try:
                info = site.fetch_info(ctx, ref)
                play = site.fetch_play(ctx, ref, ref.p, task.qn)
            except Exception as exc:
                raise RuntimeError(f"{site.name} 解析失败：{exc}") from None
            task.engine_used = site.id
            if not (play.videos or play.durl):
                raise RuntimeError(f"{site.name} 没有返回可下载的地址")
            task.note(f"平台「{site.name}」解析成功")
            engine_ids: list[str] = []
        else:
            engine_ids = ([task.engine] if task.engine and task.engine != "auto"
                          else settings.ordered_engines())
            if not engine_ids:
                engine_ids = list(ALL_ENGINES)

        from .engines import get_engine

        # 不同模式需要的流不一样：只要单文件时，返回 DASH 的通道就得让位给下一条
        def acceptable(candidate) -> bool:
            if task.mode == "single":
                return bool(candidate.durl)
            if task.mode == "dash":
                return bool(candidate.videos)
            if task.mode == "audio":
                return bool(candidate.audios)
            return bool(candidate.videos or candidate.durl)

        # auto 模式要把**所有能连上的通道**都比一遍，再挑画质最高的那条。
        #
        # 以前这里写的是 wanted = 2 —— 比较两条就停。问题是通道顺序里前两条通常是
        # web_wbi(DASH 480P) 和 web_durl(单文件 720P)，于是自动模式的结果永远是
        # 这两条里较高的那个；而排在第 10 位的 yt-dlp（实测能拿 1080P）**根本轮不到
        # 参与比较**。用户看到的"自动 = 720P"就是这么来的。
        #
        # 上限设 8 是为了兜住极端情况：通道本身不可用时 available() 会快速返回，
        # 真正发请求的只有能用的那几条。
        wanted = min(len(engine_ids), 8) if task.mode == "auto" else 1

        for engine_id in engine_ids:
            engine = get_engine(engine_id)
            ok, reason = engine.available(ctx)
            if not ok:
                errors.append(f"{engine.name}：{reason}")
                continue
            if engine_id == "pgc_bangumi" and ref.kind != "bangumi":
                continue
            try:
                engine_info = engine.fetch_info(ctx, ref)
                engine_play = engine.fetch_play(ctx, ref, ref.p, task.qn)
            except Exception as exc:
                errors.append(f"{engine.name}：{exc}")
                task.note(f"通道「{engine.name}」不可用（{str(exc)[:50]}）")
                continue
            if not acceptable(engine_play):
                errors.append(f"{engine.name}：没有 {task.mode} 模式需要的流")
                continue

            candidates.append((engine_info, engine_play, engine_id, engine.name))
            if info is None:
                info, play = engine_info, engine_play
                task.engine_used = engine_id
            task.note(f"通道「{engine.name}」解析成功"
                      f"（实际最高 {engine_play.best_height() or '?'}P）")
            if len(candidates) >= wanted:
                break

        if info is None or play is None:
            raise RuntimeError("所有抓取通道都失败了：\n" + "\n".join(errors[:6]))

        if len(candidates) > 1:
            best = max(candidates, key=lambda item: item[1].best_height())
            # 把参选的每一条和它的画质都列出来。以前只写一句"对比 N 条通道"，
            # 用户没法判断它到底比了什么、为什么选这条。
            board = "、".join(f"{c[3]} {c[1].best_height() or '?'}P" for c in candidates)
            task.note(f"画质对比（{len(candidates)} 条）：{board}")
            if best[2] != task.engine_used:
                task.note(f"改用「{best[3]}」（{best[1].best_height()}P > {play.best_height()}P）")
                info, play, task.engine_used = best[0], best[1], best[2]
            else:
                task.note(f"「{best[3]}」画质最高，保持不变")

        page = info.page_by_index(ref.p) or (info.pages[0] if info.pages else None)
        if page is None:
            from .models import Page
            page = Page(cid=info.cid, page=1, part="", duration=info.duration)

        task.title = info.title or ref.display()
        task.cover = info.cover
        task.up = info.owner.name
        task.page_title = page.part or ""
        if not task.qn:
            task.qn = self._pick_qn(play, settings.quality_choice())

        # 2) 输出目录
        base = safe_filename(render_template(settings.filename_template, {
            "title": info.title, "up": info.owner.name,
            "artist": info.owner.name, "album": info.album,
            "bvid": info.bvid or ref.display(), "id": ref.display(),
            "site": ref.site, "platform": site.name,
            "quality": qn_short(task.qn), "engine": task.engine_used,
        }) or ("music" if info.media == "music" else "video"))
        directory = ctx.download_dir() / base
        directory.mkdir(parents=True, exist_ok=True)
        task.directory = str(directory)

        multi = len(info.pages) > 1
        stem = base if not multi else safe_filename(f"{base} - P{page.page:02d} {page.part}")
        quality_tag = f" [{qn_short(task.qn)}]" if task.qn else ""

        # 3) 按模式下载
        if task.mode == "meta":
            self._write_extras(task, info, page, directory, stem, play)
            return

        if task.mode == "audio":
            self._download_audio(task, info, page, directory, stem, play)
            return

        if task.mode == "dash":
            self._download_dash(task, info, page, directory, stem, quality_tag, play)
            return

        if task.mode == "single":
            self._download_single(task, info, page, directory, stem, quality_tag, play)
            return

        # auto：有 DASH 优先 DASH，否则单文件
        if play.videos:
            self._download_dash(task, info, page, directory, stem, quality_tag, play)
        elif play.durl:
            self._download_single(task, info, page, directory, stem, quality_tag, play)
        else:
            raise RuntimeError("该通道没有返回任何可下载的地址")

        self._write_extras(task, info, page, directory, stem, play)

    # ---------------- 清晰度挑选 ----------------
    @staticmethod
    def _pick_qn(play, preferred: int) -> int:
        available = [v.qn for v in play.videos] or list(play.accept_quality)
        if not available:
            return preferred or 0
        if preferred:
            exact = [q for q in available if q == preferred]
            if exact:
                return preferred
        from .quality import best
        return best(available)

    # ---------------- DASH 路线 ----------------
    def _download_dash(self, task, info, page, directory: Path, stem: str,
                       quality_tag: str, play) -> None:
        ctx = self.ctx
        settings = ctx.settings
        video = _pick_video(play, task.qn, settings.prefer_codec)
        audio = play.best_audio()
        if not video:
            raise RuntimeError("没有可用的视频流")

        task.qn = video.qn or task.qn

        # 芒果TV 这类平台只给 HLS 播放列表，不能当成普通流去下（会把 m3u8 文本
        # 当成视频存下来），必须走分片下载再拼接。
        from .hls import is_hls
        if is_hls(video.url, video.mime):
            self._download_hls(task, video, directory, stem, quality_tag, play,
                               height=video.height,
                               label=f"HLS {video.width}x{video.height}")
            return

        task.note(
            f"选中视频流 {video.width}x{video.height} · {codec_text(video.codecs)}"
            f" · {video.label} · {video.to_dict()['size_text']}"
        )

        work = directory / "_streams"
        work.mkdir(parents=True, exist_ok=True)

        video_file = work / f"{safe_filename(stem)}.video{_ext(video.mime)}"
        self._fetch(ctx, task, video, video_file, "视频流")

        audio_file = None
        if audio:
            audio_file = work / f"{safe_filename(stem)}.audio{_ext(audio.mime)}"
            self._fetch(ctx, task, audio, audio_file, "音频流")

        task.status = STATUS_MERGING
        task.progress = {"phase": "合并中", "percent": 99.0,
                         "speed_text": "-", "eta_text": "-",
                         "size_text": task.progress.get("size_text", "0 B"),
                         "total_text": task.progress.get("total_text", "0 B")}

        final = directory / f"{stem}{quality_tag}.mp4"
        if audio_file:
            method = "ffmpeg" if find_ffmpeg() else "内置 fMP4 合并器"
            task.note(f"开始合并（{method}）")
            try:
                result = merge(video_file, audio_file, final, prefer_ffmpeg=settings.auto_merge)
                task.note(f"合并完成：{result['method']}，{result['size'] / 1048576:.1f} MB")
            except MuxError as exc:
                task.note(f"合并失败：{exc}")
                for stream_file in (video_file, audio_file):
                    if stream_file and stream_file.exists():
                        task.files.append({"kind": "stream", "path": str(stream_file),
                                           "name": stream_file.name,
                                           "size": stream_file.stat().st_size})
                raise RuntimeError(
                    f"合并失败：{exc}。原始音视频分离流已保留在 {work}，"
                    "装一个 ffmpeg 后重试即可自动改用 ffmpeg 合并。"
                ) from None
        else:
            video_file.replace(final)
            final = final

        if final and not settings.keep_streams:
            shutil.rmtree(work, ignore_errors=True)
        if final and final.exists():
            task.files.append({"kind": "video", "path": str(final),
                               "name": final.name, "size": final.stat().st_size})

    # ---------------- 单文件路线 ----------------
    def _download_single(self, task, info, page, directory: Path, stem: str,
                         quality_tag: str, play) -> None:
        ctx = self.ctx
        durls = play.durl
        if not durls:
            # 芒果TV 这类平台只给 HLS，地址在 videos 里。用户选"单文件"只是想
            # 少折腾，不该因此报错 —— 直接按 HLS 下，产出的同样是单个文件。
            from .hls import is_hls
            hls_stream = next((v for v in (play.videos or []) if is_hls(v.url, v.mime)), None)
            if hls_stream is not None:
                task.note("该平台只提供 HLS 分片，已按单文件方式下载并拼接")
                directory = Path(task.directory)
                self._download_hls(task, hls_stream, directory, stem, quality_tag, play,
                                   height=hls_stream.height, label="HLS 单链路")
                return      # 必须返回：下面还会取 durls[0]，HLS 路径没有 durl
            else:
                raise RuntimeError("该通道没有返回单文件地址（durl）")

        from .hls import is_hls
        if len(durls) == 1 and is_hls(durls[0].url):
            self._download_hls(task, durls[0], directory, stem, quality_tag, play,
                               height=0, label="HLS 单链路")
            return

        # 扩展名跟着直链走：音乐平台给的是 mp3 / flac，套上 .mp4 会打不开
        ext = (getattr(durls[0], "ext", "") or play.ext or "mp4").lstrip(".")
        task.note(f"单文件模式：共 {len(durls)} 段，下载后直接拼成一个 {ext.upper()}")
        final = directory / f"{stem}{quality_tag}.{ext}"
        part_dir = directory / "_parts"
        part_dir.mkdir(parents=True, exist_ok=True)

        # 先写临时名，成功了再改名。以前直接写成正式文件名，
        # 中途失败/取消就会在下载目录里留下一个 0 字节或半截的"成品"。
        staging = final.with_suffix(final.suffix + ".part")
        total_bytes = sum(d.size for d in durls) or 0
        try:
            with open(staging, "wb") as out:
                for idx, item in enumerate(durls, start=1):
                    if task.cancel_event.is_set():
                        raise DownloadCancelled("用户已取消")
                    part = part_dir / f"part{idx:03d}.bin"
                    self._fetch(ctx, task, item, part, f"第 {idx}/{len(durls)} 段")
                    with open(part, "rb") as fh:
                        shutil.copyfileobj(fh, out, 1 << 20)
                    part.unlink(missing_ok=True)
        except BaseException:
            staging.unlink(missing_ok=True)      # 半截文件没有保留价值
            raise
        finally:
            shutil.rmtree(part_dir, ignore_errors=True)

        os.replace(staging, final)
        task.files.append({"kind": "video", "path": str(final),
                           "name": final.name, "size": final.stat().st_size})
        task.note(f"写入完成：{final.name}（{final.stat().st_size / 1048576:.1f} MB）")

    # ---------------- 本地文件：解密 + 转格式 ----------------
    def _execute_convert(self, task: Task) -> None:
        """把音乐平台的加密文件解密，再按用户选的格式输出。"""
        from pathlib import Path as _Path

        from .music import decrypt as _decrypt
        from .music import transcode as _transcode

        source = _Path(task.local_path)
        if not source.exists():
            raise RuntimeError("文件不存在：" + str(source))
        if source.is_dir():
            raise RuntimeError("这是一个目录，请选择具体文件")

        task.title = source.stem
        task.up = ""
        task.site = "local"
        task.note(f"本地文件：{source.name}（{source.stat().st_size / 1048576:.1f} MB）")

        work_dir = self.ctx.download_dir() / "音乐转换"
        work_dir.mkdir(parents=True, exist_ok=True)

        # 1) 加密文件先解密；本来就是通用格式就跳过这一步
        if _decrypt.is_encrypted(source):
            kind = _decrypt.sniff(source)
            task.note(f"识别为 {kind} 加密格式，开始解密")
            result = _decrypt.decrypt(source, work_dir)
            current = _Path(result["output"])
            task.note(f"解密完成：{current.name}（{result.get('format') or '?'}）")
            meta = result.get("meta") or {}
        else:
            current = source
            meta = {}
            task.note("不是已知的加密格式，直接按普通音频处理")

        if task.cancel_event.is_set():
            raise DownloadCancelled("用户已取消")

        # 2) 按用户选的格式输出
        target = task.target or "copy"
        converted = _transcode.convert(current, target, work_dir, bitrate=task.bitrate or "")
        final = _Path(converted["output"])
        task.note(converted.get("note") or f"输出 {final.name}")

        # 3) 尽量把标签带上（解密时解析到的元数据）
        if meta and _transcode.write_tags(final, meta):
            task.note("已写入标题 / 歌手 / 专辑 / 封面")

        task.directory = str(work_dir)
        task.files.append({"kind": "audio", "path": str(final),
                           "name": final.name, "size": final.stat().st_size})

    # ---------------- HLS（芒果TV 等） ----------------
    def _download_hls(self, task: Task, source, directory: Path, stem: str,
                      quality_tag: str, play, height: int = 0, label: str = "HLS") -> None:
        """下载 m3u8 分片并拼接。TS 分片直接接起来就是可播放的 mpegts。"""
        from .hls import HlsError
        from .hls import download as hls_download

        settings = self.ctx.settings
        referer = (getattr(source, "referer", "") or _platform_referer(task, self.ctx))
        task.note(f"{label}：开始下载分片")
        last = {"t": 0.0}

        def on_progress(p):
            now = time.time()
            if now - last["t"] < 0.3 and p.percent < 100:
                return
            last["t"] = now
            task.progress = p.to_dict()

        # 后缀先按 TS 起（绝大多数平台都是 mpegts），拿到 container 后再定；
        # fMP4 分片拼出来本身就是 MP4，命名成 .ts 会让部分播放器拒绝打开。
        final = directory / f"{stem}{quality_tag}.ts"
        try:
            result = hls_download(
                self.ctx, source.url, final,
                threads=settings.threads_per_task,
                referer=referer,
                on_progress=on_progress,
                cancel_event=task.cancel_event,
                prefer_height=height,
                label=label,
            )
        except DownloadCancelled:
            raise
        except HlsError as exc:
            raise RuntimeError(f"{label} 下载失败：{exc}") from None

        task.note(f"{label} 完成：{result['segments']} 个分片，"
                  f"{result['size'] / 1048576:.1f} MB（{result['container']}）")

        # fMP4 分片拼出来本身就是 MP4，直接把后缀改对，不必劳烦 ffmpeg
        if result["container"] == "fmp4" and final.suffix != ".mp4":
            renamed = final.with_suffix(".mp4")
            try:
                final.replace(renamed)
                final = renamed
            except OSError:
                pass

        # 有 ffmpeg 就顺手转成 mp4，兼容性更好；没有就保留 .ts（VLC / PotPlayer /
        # mpv 都能直接播，Windows 自带播放器不行）。
        out = final
        ffmpeg = find_ffmpeg()
        if ffmpeg and result["container"] == "mpegts":
            mp4 = final.with_suffix(".mp4")
            try:
                subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(final), "-c", "copy", "-bsf:a", "aac_adtstoasc", str(mp4)],
                    capture_output=True, timeout=1800, check=True)
                final.unlink(missing_ok=True)
                out = mp4
                task.note("已用 ffmpeg 转封装为 MP4")
            except (OSError, subprocess.SubprocessError):
                task.note("ffmpeg 转封装失败，保留 .ts（多数播放器可直接播放）")
        elif result["container"] == "mpegts":
            task.note("输出为 MPEG-TS，VLC / PotPlayer / mpv 可直接播放；装 ffmpeg 后会自动转成 MP4")

        task.files.append({"kind": "video", "path": str(out),
                           "name": out.name, "size": out.stat().st_size})

    # ---------------- 仅音频 ----------------
    def _download_audio(self, task, info, page, directory: Path, stem: str, play) -> None:
        audio = play.best_audio()
        if not audio:
            raise RuntimeError("该通道没有返回独立音轨，请换用其他通道")
        final = directory / f"{stem}.m4a"
        self._fetch(self.ctx, task, audio, final, "音频流")
        task.files.append({"kind": "audio", "path": str(final),
                           "name": final.name, "size": final.stat().st_size})

    # ---------------- 单个流的下载与进度 ----------------
    def _fetch(self, ctx, task: Task, source, dest: Path, phase: str,
               referer: str | None = None) -> None:
        settings = ctx.settings
        task.progress = {"phase": phase, "percent": 0.0, "speed_text": "-",
                         "eta_text": "-", "size_text": "0 B", "total_text": "0 B"}
        # 优先级：调用方指定 > 直链自带 > 平台默认。
        # 以前这里写死了 B 站的地址，换成别的平台就会带着错误的 Referer 去请求 CDN。
        referer = (referer or getattr(source, "referer", "")
                   or _platform_referer(task, ctx))
        last = {"t": 0.0}

        def on_progress(p):
            now = time.time()
            if now - last["t"] < 0.25 and p.phase != "完成":
                return
            last["t"] = now
            data = p.to_dict()
            data["phase"] = phase if p.phase != "完成" else f"{phase} · 完成"
            task.progress = data

        downloader = Downloader(
            ctx, source.url, dest,
            size=getattr(source, "size", 0) or 0,
            threads=settings.threads_per_task,
            referer=referer,
            on_progress=on_progress,
            cancel_event=task.cancel_event,
            label=phase,
        )
        urls = [source.url] + list(getattr(source, "backups", []) or [])
        last_error: Exception | None = None
        for url in urls[:3]:
            downloader.url = url
            try:
                downloader.run()
                return
            except DownloadCancelled:
                raise
            except Exception as exc:
                last_error = exc
                task.note(f"{phase} 主地址失败，尝试备用 CDN（{str(exc)[:60]}）")
                continue
        raise RuntimeError(f"{phase} 下载失败：{last_error}")

    # ---------------- 附加产物 ----------------
    def _write_extras(self, task: Task, info, page, directory: Path, stem: str, play) -> None:
        settings = self.ctx.settings
        ctx = self.ctx
        stem = safe_filename(stem)
        # 弹幕与 CC 字幕目前只有 B 站能拿到；别的平台拿它的 cid 去问 B 站接口
        # 只会得到 404，徒增一条看不懂的失败日志。
        is_bilibili = (task.site or "bilibili") == "bilibili"
        is_music = (getattr(info, "media", "video") == "music")

        if settings.write_cover and info.cover:
            try:
                path = directory / f"{stem}.jpg"
                size = extras.save_cover(ctx, info.cover, path)
                task.files.append({"kind": "cover", "path": str(path),
                                   "name": path.name, "size": size})
                task.note("封面已保存")
            except Exception as exc:
                task.note(f"封面保存失败：{exc}")

        if settings.write_danmaku and page.cid and is_bilibili and not is_music:
            try:
                path = directory / f"{stem}.xml"
                count = extras.save_danmaku(ctx, page.cid, path)
                task.files.append({"kind": "danmaku", "path": str(path),
                                   "name": path.name, "size": path.stat().st_size,
                                   "count": count})
                ass = directory / f"{stem}.ass"
                lines = extras.danmaku_to_ass(path.read_text(encoding="utf-8"), ass,
                                              title=f"{info.title} {page.part}".strip())
                if lines:
                    task.files.append({"kind": "danmaku_ass", "path": str(ass),
                                       "name": ass.name, "size": ass.stat().st_size,
                                       "count": lines})
                task.note(f"弹幕已保存，共 {count} 条")
            except Exception as exc:
                task.note(f"弹幕保存失败：{exc}")

        if settings.write_subtitle and info.bvid and page.cid and is_bilibili and not is_music:
            try:
                written = extras.save_subtitles(ctx, info.bvid, page.cid, directory, stem)
                for item in written:
                    p = Path(item)
                    task.files.append({"kind": "subtitle", "path": item,
                                       "name": p.name, "size": p.stat().st_size})
                if written:
                    task.note(f"字幕已保存 {len(written)} 条")
                else:
                    task.note("该视频没有可用字幕")
            except Exception as exc:
                task.note(f"字幕保存失败：{exc}")

        if settings.write_nfo and not is_music:
            try:
                path = directory / f"{stem}.nfo"
                extras.save_nfo(info, page, path, quality=qn_label(task.qn),
                                engine=task.engine_used,
                                website=task.raw_input if task.raw_input.startswith("http") else "")
                task.files.append({"kind": "nfo", "path": str(path),
                                   "name": path.name, "size": path.stat().st_size})
            except Exception as exc:
                task.note(f"NFO 写入失败：{exc}")


def _platform_referer(task: Task, ctx) -> str:
    """按平台给出默认 Referer。"""
    site_id = task.site or "bilibili"
    if site_id != "bilibili":
        try:
            from .sites import get_site
            home = getattr(get_site(site_id), "home", "") or ""
            # 适配器内部的常量都带结尾斜杠，这里保持一致，
            # 免得同一次下载在"取流"和"下分片"两步用了不同的 Referer
            return home.rstrip("/") + "/" if home else ""
        except Exception:
            return ""
    # raw_input 通常已是完整地址，直接往模板里塞会拼出
    # "https://www.bilibili.com/video/https://www.bilibili.com/video/BV..."
    # Task 上没有 bvid/ident 字段（以前这里直接点属性，导致 B 站任务
    # 一走到这条分支就 AttributeError，整条下载链路全挂）—— 用 getattr 兜底。
    ident = (getattr(task, "bvid", "") or getattr(task, "ident", "") or "").strip()
    if not ident:
        raw = (task.raw_input or "").strip()
        if raw.startswith("http"):
            return "https://www.bilibili.com/"
        ident = raw
    if not ident:
        return "https://www.bilibili.com/"
    if ident.startswith("ep") or ident.startswith("ss"):
        return "https://www.bilibili.com/bangumi/play/" + ident
    return "https://www.bilibili.com/video/" + ident


def _pick_video(play, qn: int, codec_preference: str = "auto"):
    """先锁画质档，再按编码偏好和码率挑具体那一条流。

    B 站的同一档画质会同时下发 H.264 / H.265 / AV1 三个版本，
    默认选 H.264 兼容性最好，也可以在设置里改成 HEVC 或 AV1 来省体积。
    """
    if not play.videos:
        return None
    pool = [v for v in play.videos if v.qn == qn] or list(play.videos)
    top = max(v.height for v in pool)
    pool = [v for v in pool if v.height == top] or pool
    pool.sort(key=lambda v: (codec_rank(v.codecs, codec_preference), -v.bandwidth))
    return pool[0]


def _ext(mime: str) -> str:
    mime = (mime or "").lower()
    if "audio" in mime:
        return ".m4a"
    return ".m4s"
