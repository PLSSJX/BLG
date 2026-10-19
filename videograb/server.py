"""内置 HTTP 服务：静态界面 + JSON API。只用标准库。"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .config import (ALL_ENGINES, BASE_DIR, DATA_DIR, THEME_PRESETS, WEB_DIR,
                     Context, is_frozen)
from .engines import describe, get_engine
from .muxer import find_ffmpeg
from .quality import (CODEC_PREFERENCES, QUALITY_TABLE, needs_login, needs_vip,
                      weight as qn_weight)
from .refs import normalize, parse_ref
from .sites import describe as describe_sites
from .sites import get_site, parse_any
from .tasks import MODES, TaskManager

MAX_BODY = 4 * 1024 * 1024
_CHANGELOG = ""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


def build_state(ctx: Context) -> dict:
    settings = ctx.settings
    qualities = []
    for qn, (name, short, _w) in sorted(QUALITY_TABLE.items(), key=lambda kv: -kv[1][2]):
        qualities.append({
            "qn": qn, "name": name, "short": short,
            "needs_login": needs_login(qn), "needs_vip": needs_vip(qn),
        })
    return {
        "version": __version__,
        "app": "Video-Grabing",
        "app_cn": "Video-Grabing",
        "engines": describe(ctx),
        "sites": describe_sites(),
        "webview": _webview_status(),
        "settings": _public_settings(ctx),
        "theme_presets": THEME_PRESETS,
        "qualities": qualities,
        "modes": MODES,
        "codec_preferences": CODEC_PREFERENCES,
        "stats": ctx.tasks.stats() if ctx.tasks else {},
        "ffmpeg": find_ffmpeg() or "",
        "python": _python_version(),
        "logged_in": bool(settings.sessdata.strip()),
        "download_dir": str(ctx.download_dir()),
        # 前端拿它给所有写操作签名；跨站页面读不到这个响应（CORS），拿不到令牌
        "api_token": getattr(ctx, "api_token", ""),
        "window_mode": getattr(ctx, "window_mode", "") or "server",
        "frozen": is_frozen(),
        "base_dir": str(BASE_DIR),
    }


def _public_settings(ctx: Context) -> dict:
    from dataclasses import asdict
    data = asdict(ctx.settings)
    configured = (ctx.settings.download_dir or "").strip()
    actual = str(ctx.download_dir())
    # 配置里留空表示"用默认位置"，界面要显示解析后的真实路径
    data["download_dir"] = actual
    try:
        data["music_dir"] = str(ctx.music_dir())
    except Exception:
        data["music_dir"] = ""
    # 配置了却没用上（盘符不存在、路径非法等）时必须让界面知道 ——
    # 以前是静默回落到默认目录，用户看到"保存成功"但文件跑去了别处。
    import os as _os
    data["download_dir_ok"] = (not configured) or (
        _os.path.normcase(_os.path.abspath(actual)) == _os.path.normcase(_os.path.abspath(configured)))
    # 凭据一律不回显原文，只告诉前端"配没配"。哪些字段算凭据由各平台适配器声明，
    # 新增平台时只要在适配器里声明 credentials，这里自动生效。
    for site in describe_sites():
        for item in site.get("credentials") or []:
            field = item.get("field")
            if not field:
                continue
            data["has_" + field] = bool(str(getattr(ctx.settings, field, "") or "").strip())
            data[field] = ""
    return data


def run_probe(ctx: Context, raw_input: str, qn: int = 0) -> dict:
    """把同一个视频丢给全部通道，报告每条通道的真实结果。"""
    try:
        ref = _prepare_ref(ctx, raw_input)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    if ref.site != "bilibili":
        raise ApiError("通道体检是哔哩哔哩专属功能：B 站有 10 条互相独立的抓取通道可以横向对比，"
                       "其它平台各自只有一条适配器路径。", 400)

    engines = [e for e in describe(ctx) if e["enabled"]]
    results: dict[str, dict] = {}

    def run_one(entry: dict) -> dict:
        engine = get_engine(entry["id"])
        item = {
            "id": entry["id"], "name": entry["name"], "kind": entry["kind"],
            "badge": entry["badge"], "ok": False, "ms": 0,
            "info_ok": False, "play_ok": False,
            "quality": "", "quality_qn": 0, "streams": 0,
            "note": "", "error": "",
            "is_dash": False, "best": None,
        }
        import time as _t
        started = _t.time()
        ok, reason = engine.available(ctx)
        if not ok:
            item["error"] = reason
            return item
        if entry["id"] == "pgc_bangumi" and ref.kind != "bangumi":
            item["error"] = "该通道只处理番剧 / 影视，当前输入是普通投稿"
            return item

        info = None
        try:
            info = engine.fetch_info(ctx, ref)
            item["info_ok"] = True
            item["title"] = info.title
            item["pages"] = len(info.pages)
        except Exception as exc:
            item["error"] = f"元数据失败：{str(exc)[:140]}"
            item["ms"] = int((_t.time() - started) * 1000)
            return item

        try:
            play = engine.fetch_play(ctx, ref, ref.p, qn)
            item["play_ok"] = True
            item["is_dash"] = bool(play.videos) and not play.durl
            item["streams"] = len(play.videos) + len(play.audios) + len(play.durl)
            item["note"] = play.note
            item["qualities"] = play.accept_quality[:8]
            # 接口"声称"支持、但实际没下发的档位，界面上会单独标注出来
            item["phantom"] = sorted(play.phantom_quality, key=qn_weight, reverse=True)[:6]
            item["declared"] = sorted(play.declared_quality, key=qn_weight, reverse=True)[:8]
            best = play.best_video()
            if best:
                item["best"] = {
                    "resolution": f"{best.width}x{best.height}",
                    "qn": best.qn, "label": best.label,
                    "codecs": best.codecs, "codec_text": best.to_dict()["codec_text"],
                    "size_text": best.to_dict()["size_text"],
                }
                item["quality"] = f"{best.width}x{best.height} · {best.label}"
                item["quality_qn"] = best.qn
            elif play.durl:
                biggest = max(play.durl, key=lambda d: d.size)
                item["quality"] = f"单文件 MP4 · {biggest.to_dict()['size_text']}"
                item["quality_qn"] = play.accept_quality[0] if play.accept_quality else 0
                item["best"] = {"resolution": "已封装", "qn": item["quality_qn"],
                                "label": "单文件", "codecs": "",
                                "size_text": biggest.to_dict()["size_text"]}
            item["ok"] = True
        except Exception as exc:
            item["error"] = f"取流失败：{str(exc)[:180]}"
        item["ms"] = int((_t.time() - started) * 1000)
        return item

    # 并发不宜过高：同时打太多请求会触发 B 站风控（412），
    # 反而让原本可用的通道看起来是坏的。客户端本身也带接口限速。
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, entry): entry["id"] for entry in engines}
        for future in as_completed(futures):
            engine_id = futures[future]
            try:
                results[engine_id] = future.result()
            except Exception as exc:
                results[engine_id] = {"id": engine_id, "ok": False, "error": str(exc)[:200]}

    ordered = [results[e["id"]] for e in engines if e["id"] in results]
    ok_count = sum(1 for r in ordered if r.get("ok"))
    return {
        "ref": ref.to_dict(),
        "identity": ref.display(),
        "results": ordered,
        "ok_count": ok_count,
        "total": len(ordered),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Video-Grabing"
    protocol_version = "HTTP/1.1"
    ctx: Context = None  # type: ignore[assignment]

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):
        if self.server.quiet:  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              extra: dict | None = None, cache: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        for key, value in (extra or {}).items():
            if key.lower() == "cache-control":
                continue      # 已经发过了，再来一个浏览器会按更严格的处理
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, extra=extra)

    def _error(self, message: str, status: int = 400, close: bool = False) -> None:
        """回一个 {"ok": false, "error": ...}。

        close=True 表示"这次拒绝发生在读请求体之前"，必须顺手断开连接：
        否则 socket 里剩下的 JSON body 会被 http.server 当成下一个请求的
        起始行，客户端收到的是一条 501 的 **HTML** 错误页
        （实测：501 Unsupported method ('{"paths":[]}POST')），
        既不是结构化错误，也让同一条 keep-alive 连接上的后续请求全废。
        """
        if close:
            self.close_connection = True
        self._json({"ok": False, "error": message}, status=status,
                   extra={"Connection": "close"} if self.close_connection else None)

    def _body(self) -> dict:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            # 长度都不知道，没法把这个连接对齐到下一个请求，只能断开
            self.close_connection = True
            raise ApiError("Content-Length 不合法") from None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            # 超限的 body 不能当成正常输入，但也不能"不读就回错然后断开"：
            # 客户端还在往外写，早关 socket 会变成 RST，413 这条 JSON 直接丢失。
            # 两倍上限以内的先收下再回错，连接仍然可用；更大的就直接断。
            if length <= MAX_BODY * 2:
                try:
                    self.rfile.read(length)
                except OSError:
                    self.close_connection = True
                raise ApiError("请求体过大", 413)
            self.close_connection = True
            raise ApiError("请求体过大", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError("请求体不是合法 JSON") from None
        # 顶层必须是对象。以前这里直接 return，所有调用方都当成 dict 用：
        # body=[] / null / "abc" 会在 payload.get(...) 处抛 AttributeError，
        # 变成 "服务器内部错误：'list' object has no attribute 'get'" 这样的 500。
        if not isinstance(data, dict):
            raise ApiError("请求体必须是一个 JSON 对象")
        return data

    def _query(self) -> dict:
        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    # ---------------- 路由 ----------------
    def do_GET(self):  # noqa: N802
        route = urlparse(self.path).path
        try:
            # 封面代理要排在 /api/ 前缀判断之前，否则会被当成未知的 JSON 接口
            if route == "/api/cover":
                return self._serve_cover()
            if route.startswith("/api/"):
                return self._api_get(route)
            if route == "/media":
                return self._serve_media()
            return self._serve_static(route)
        except ApiError as exc:
            return self._error(exc.message, exc.status)
        except BrokenPipeError:
            return
        except Exception as exc:  # 兜底，避免服务端因为单次请求崩掉
            return self._error(f"服务器内部错误：{exc}", 500)

    def do_HEAD(self):  # noqa: N802
        return self.do_GET()

    # ---------------- 请求来源校验 ----------------
    # 服务只监听 127.0.0.1，但浏览器里的任意网页都能向本机发跨站请求。
    # no-cors 的简单请求不触发预检，所以只要不校验来源，恶意页面就能改配置
    # （比如把代理指向自己的服务器，把 SESSDATA 骗走）甚至调 /api/quit。
    def _check_origin(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _check_token(self) -> bool:
        expected = getattr(self.ctx, "api_token", "")
        if not expected:
            return True
        return self.headers.get("X-Video-Grabing-Token") == expected

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path).path
        try:
            if not self._check_origin():
                return self._error("拒绝跨站请求", 403, close=True)
            # 自定义请求头无法用 no-cors 伪造，等价于一道 CSRF 防线
            if not self._check_token():
                return self._error("令牌无效或已过期，请刷新页面", 403, close=True)
            return self._api_post(route)
        except ApiError as exc:
            return self._error(exc.message, exc.status)
        except BrokenPipeError:
            return
        except Exception as exc:
            # 更新相关的失败（路径不合法、下载失败、哈希不符）是用户可纠正的，
            # 用 400 而不是 500 回报，前端才能把消息当成提示显示出来。
            from .updater import UpdaterError
            if isinstance(exc, UpdaterError):
                return self._error(str(exc), 400)
            return self._error(f"服务器内部错误：{exc}", 500)

    # ---------------- GET API ----------------
    def _api_get(self, route: str) -> None:
        ctx = self.ctx
        if route == "/api/state":
            return self._json({"ok": True, **build_state(ctx)})
        if route == "/api/tasks":
            return self._json({"ok": True, "tasks": ctx.tasks.list(), "stats": ctx.tasks.stats()})
        if route == "/api/engines":
            return self._json({"ok": True, "engines": describe(ctx)})
        if route == "/api/settings":
            return self._json({"ok": True, "settings": _public_settings(ctx)})
        if route == "/api/ping":
            # 单实例检测用：另一个进程靠它判断"这个端口上是不是已经有一个 Video-Grabing"
            return self._json({"ok": True, "app": "Video-Grabing", "version": __version__})
        if route == "/api/local/formats":
            from .music import transcode
            ffmpeg = transcode.find_ffmpeg()
            source = (self._query().get("source") or "").strip()
            return self._json({
                "ok": True,
                "ffmpeg": ffmpeg,
                "targets": transcode.available_targets(source),
            })

        if route == "/api/versions":
            return self._json({
                "ok": True, "ffmpeg": find_ffmpeg() or "",
                "python": _python_version(),
                "download_dir": str(ctx.download_dir()),
            })
        return self._error("未知接口", 404)

    # ---------------- POST API ----------------
    def _api_post(self, route: str) -> None:
        ctx = self.ctx
        payload = self._body()

        if route == "/api/parse":
            raw = (payload.get("input") or "").strip()
            if not raw:
                raise ApiError("请输入视频地址或 BV 号")
            ref = _prepare_ref(ctx, raw)
            engine_id = payload.get("engine") or "auto"
            qn = int(payload.get("qn") or 0)

            site = get_site(ref.site)
            if ref.site != "bilibili":
                # 其它平台没有多通道的概念，直接问适配器
                try:
                    info = site.fetch_info(ctx, ref)
                except Exception as exc:
                    raise ApiError(f"{site.name} 解析失败：{exc}") from None
                return self._json({
                    "ok": True, "info": info.to_dict(), "ref": ref.to_dict(),
                    "engine": site.id, "engine_name": site.name, "errors": [],
                })

            engines = ([get_engine(engine_id)] if engine_id != "auto"
                       else [get_engine(i) for i in ctx.settings.ordered_engines()])
            errors = []
            for engine in engines:
                ok, reason = engine.available(ctx)
                if not ok:
                    errors.append(f"{engine.name}：{reason}")
                    continue
                if engine.id == "pgc_bangumi" and ref.kind != "bangumi":
                    continue
                try:
                    info = engine.fetch_info(ctx, ref)
                except Exception as exc:
                    errors.append(f"{engine.name}：{exc}")
                    continue
                return self._json({
                    "ok": True, "info": info.to_dict(), "ref": ref.to_dict(),
                    "engine": engine.id, "engine_name": engine.name,
                    "errors": errors,
                })
            raise ApiError("所有通道都没能取到视频信息：\n" + "\n".join(errors[:5]))

        if route == "/api/play":
            raw = (payload.get("input") or "").strip()
            if not raw:
                raise ApiError("请输入视频地址或 BV 号")
            ref = _prepare_ref(ctx, raw)
            if payload.get("p"):
                ref.p = int(payload["p"])
            qn = int(payload.get("qn") or 0)
            engine_id = payload.get("engine") or "auto"

            site = get_site(ref.site)
            if ref.site != "bilibili":
                try:
                    play = site.fetch_play(ctx, ref, ref.p, qn)
                except Exception as exc:
                    raise ApiError(f"{site.name} 取流失败：{exc}") from None
                return self._json({
                    "ok": True, "play": play.to_dict(),
                    "engine": site.id, "engine_name": site.name, "errors": [],
                })

            engines = ([get_engine(engine_id)] if engine_id != "auto"
                       else [get_engine(i) for i in ctx.settings.ordered_engines()])
            errors: list = []
            usable = []
            for engine in engines:
                ok, reason = engine.available(ctx)
                if not ok:
                    errors.append(f"{engine.name}：{reason}")
                    continue
                if engine.id == "pgc_bangumi" and ref.kind != "bangumi":
                    continue
                usable.append(engine)

            def _grab(engine):
                try:
                    return engine, engine.fetch_play(ctx, ref, ref.p, qn), None
                except Exception as exc:
                    return engine, None, str(exc)

            # 并行把所有可用通道试一遍，再挑画质最高的那条。
            # 以前这里是"第一个成功就返回"，而通道顺序里 web_durl（durl 单文件，
            # 登录也只有 720P）排在前面，于是预览永远显示 720P —— 实际下载却
            # 会因为比较全部通道而拿到 1080P。预览和真实结果对不上就是这么来的。
            candidates = []
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(usable)))) as pool:
                for engine, play, error in pool.map(_grab, usable):
                    if play is None:
                        errors.append(f"{engine.name}：{error}")
                        continue
                    if not (play.videos or play.durl):
                        errors.append(f"{engine.name}：没有可下载的地址")
                        continue
                    candidates.append((engine, play))

            if not candidates:
                raise ApiError("所有通道都没能取到播放地址：\n" + "\n".join(errors[:5]))

            best_engine, best_play = max(candidates, key=lambda item: item[1].best_height())
            # 把参选的通道和画质一起返回，界面可以展示"为什么选这条"
            board = [{"engine": e.id, "name": e.name, "height": p.best_height()}
                     for e, p in candidates]
            return self._json({
                "ok": True, "play": best_play.to_dict(),
                "engine": best_engine.id, "engine_name": best_engine.name,
                "compared": sorted(board, key=lambda b: -b["height"]),
                "errors": errors,
            })

        if route == "/api/probe":
            raw = (payload.get("input") or "").strip()
            if not raw:
                raise ApiError("请输入视频地址或 BV 号")
            qn = int(payload.get("qn") or 0)
            return self._json({"ok": True, **run_probe(ctx, raw, qn)})

        if route == "/api/tasks":
            raw = (payload.get("input") or "").strip()
            if not raw:
                raise ApiError("请输入视频地址或 BV 号")
            engine = payload.get("engine") or "auto"
            qn = int(payload.get("qn") or 0)
            mode = payload.get("mode") or "auto"

            # 分 P 批量下载：一次请求建多个任务，每个 P 一个任务，
            # 这样每个 P 的进度、重试、失败原因都是独立的。
            pages = payload.get("pages")
            if isinstance(pages, list) and pages:
                created = []
                for item in pages[:200]:
                    try:
                        number = max(1, int(item))
                    except (TypeError, ValueError):
                        continue
                    created.append(ctx.tasks.submit(
                        raw, engine=engine, qn=qn, mode=mode, page=number).to_dict())
                if not created:
                    raise ApiError("没有解析出有效的分 P 序号")
                return self._json({"ok": True, "tasks": created, "count": len(created)})

            task = ctx.tasks.submit(
                raw, engine=engine, qn=qn, mode=mode,
                page=int(payload.get("page") or 1),
            )
            return self._json({"ok": True, "task": task.to_dict()})

        if route == "/api/window/frame":
            # 界面算好 Material 3 的颜色后下发过来，我们应用到 Windows 的窗口边框上。
            # 前端拿不到 HWND，所以这件事必须由后端做。
            from .window_frame import apply_current
            payload = payload or {}
            ok = apply_current(
                title="Video-Grabing",
                caption=str(payload.get("caption") or ""),
                text=str(payload.get("text") or ""),
                border=str(payload.get("border") or ""),
                dark=bool(payload.get("dark")),
            )
            return self._json({"ok": True, "applied": ok})

        if route == "/api/local/pick":
            from .localfile import pick_files
            paths = pick_files(self.ctx)
            return self._json({"ok": True, "paths": paths, "count": len(paths)})

        # 注意：请求体在 _api_post 开头已经读过一次了（rfile 是一次性的），
        # 这里绝不能再调 self._body()，否则会阻塞在一个已经读空的流上。
        if route == "/api/local/pickdir":
            from .localfile import pick_folder
            from .config import BASE_DIR
            # field 决定选完之后写哪个设置项：视频目录还是音乐目录
            field = str(payload.get("field") or "download_dir")
            if field not in ("download_dir", "music_dir"):
                raise ApiError("不支持设置 " + field)
            current = str(ctx.music_dir() if field == "music_dir" else ctx.download_dir())
            chosen = pick_folder(ctx, current)
            if chosen:
                ctx.update({field: chosen})
            return self._json({"ok": True, "path": chosen or "", "field": field,
                               "cancelled": not chosen,
                               "download_dir": str(ctx.download_dir()),
                               "music_dir": str(ctx.music_dir())})

        if route == "/api/video/trim":
            from .music.transcode import trim_video
            src = str(payload.get("path") or "").strip()
            if not src:
                raise ApiError("请先选择要裁剪的视频文件")
            src_path = Path(src)
            if not src_path.is_file():
                raise ApiError("找不到这个视频文件")
            # dest 只允许落在受管目录里，或者就是视频自己所在的目录。
            # 不传 dest 时保持原来的行为：输出到视频旁边。
            dest = src_path.parent
            raw_dest = str(payload.get("dest") or "").strip()
            if raw_dest:
                candidate = Path(raw_dest)
                if not _inside_output_root(candidate, ctx, src_path):
                    raise ApiError("输出目录只能放在下载目录或音乐目录里：" + raw_dest)
                if candidate.exists() and not candidate.is_dir():
                    raise ApiError("输出位置是个文件，不是目录：" + raw_dest)
                dest = candidate
            try:
                result = trim_video(src_path, dest, payload.get("start") or "0",
                                    payload.get("end") or "",
                                    precise=bool(payload.get("precise")))
            except Exception as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True, **result})

        if route == "/api/video/extract-audio":
            from .music.transcode import EXTRACT_FORMATS, extract_audio
            paths = _as_str_list(payload, "paths", "视频路径")
            if not paths:
                raise ApiError("没有选择视频")
            target = str(payload.get("target") or "mp3").strip().lower()
            if target not in EXTRACT_FORMATS:
                raise ApiError("不支持的音频格式：" + target +
                               "（可选：" + "、".join(EXTRACT_FORMATS) + "）")
            done, failed = [], []
            for p in paths:
                try:
                    done.append(extract_audio(p, ctx.music_dir(), target))
                except Exception as exc:
                    failed.append({"path": p, "error": str(exc)[:140]})
            return self._json({"ok": True, "done": done, "failed": failed})

        if route == "/api/music/search":
            # 换源搜索：同时查网易云和 QQ 音乐，每条结果都带来源平台
            from . import playlist as _pl
            keyword = str(payload.get("q") or "").strip()
            if not keyword:
                raise ApiError("请输入歌名或「歌名 歌手」")
            try:
                limit = int(payload.get("limit") or 10)
            except (TypeError, ValueError):
                raise ApiError("limit 必须是数字") from None
            # 夹到 1–50：这个值会原样拼进上游接口的查询串里，不能让调用方随便放大
            limit = max(1, min(50, limit))
            items = _pl.search(keyword, limit)
            # errors：某个平台搜挂了会把原因一起带回去，不再"静默少一半结果"
            return self._json({"ok": True, "items": items,
                               "errors": _pl.last_search_errors()})

        if route == "/api/music/import":
            # 把用户选的音频文件复制进音乐目录，之后就出现在「播放」页的歌曲列表里
            import shutil as _sh
            from pathlib import Path as _P2
            paths = _as_str_list(payload, "paths", "文件路径")
            if not paths:
                raise ApiError("还没有选择文件")
            root = ctx.music_dir()
            root.mkdir(parents=True, exist_ok=True)
            done, failed = [], []
            audio_ext = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".wma"}
            for raw in paths:
                src = _P2(raw)
                try:
                    if src.is_dir():
                        raise OSError("这是一个目录，不是音频文件")
                    if not src.is_file():
                        raise OSError("文件不存在")
                    if src.suffix.lower() not in audio_ext:
                        raise OSError("不是音频格式：" + src.suffix)
                    dest = root / src.name
                    index = 2
                    while dest.exists() and dest.resolve() != src.resolve():
                        dest = root / (src.stem + " (" + str(index) + ")" + src.suffix)
                        index += 1
                    if dest.resolve() != src.resolve():
                        _sh.copy2(src, dest)
                    done.append({"name": dest.name, "path": str(dest),
                                 "size": dest.stat().st_size})
                except OSError as exc:
                    failed.append({"path": raw, "error": str(exc)[:120]})
            return self._json({"ok": True, "done": done, "failed": failed,
                               "dir": str(root)})

        if route == "/api/music/list":
            # 扫音乐目录，把能播的文件列出来。
            # 这里必须限量：目录是用户的，可能塞进几万个文件，也可能挂着 junction。
            import os as _os
            root = ctx.music_dir()
            want = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".wma",
                    ".mp4", ".mkv", ".webm"}
            max_return = 2000         # 界面最多显示这么多
            max_collect = 4000        # 先多收一点，排序之后再截断，结果才稳定
            max_scan = 20000          # 最多看这么多目录项（5000 个文件实测 ~40ms）
            items = []
            scanned = 0
            truncated = False
            for base, dirs, files in _os.walk(str(root)):
                # 跳过 junction / 符号链接：os.walk 的 followlinks=False 只管符号链接，
                # 实测在音乐目录里放一个指向 C:\Windows 的 junction，这个接口会跑 24 秒，
                # 还会把系统自带的 wav 当成本地歌曲列出来。
                dirs[:] = [d for d in dirs
                           if not _is_reparse_point(_os.path.join(base, d))]
                scanned += len(dirs)
                for name in files:
                    scanned += 1
                    if scanned > max_scan:
                        truncated = True
                        break
                    ext = _os.path.splitext(name)[1].lower()
                    if ext not in want:
                        continue
                    full = _os.path.join(base, name)
                    try:
                        items.append({"name": name,
                                      "path": full,
                                      # 同名文件靠所在文件夹区分
                                      "folder": _os.path.basename(base) if base.lower() != str(root).lower() else "",
                                      "size": _os.path.getsize(full),
                                      "music": ext not in (".mp4", ".mkv", ".webm")})
                    except OSError:
                        continue
                    if len(items) >= max_collect:
                        truncated = True
                        break
                if truncated:
                    break
            items.sort(key=lambda x: x["name"].lower())
            if len(items) > max_return:
                truncated = True
            # truncated=True 表示列表被截断了，界面可以提示"只显示了前 N 首"
            return self._json({"ok": True, "dir": str(root),
                               "items": items[:max_return],
                               "truncated": truncated, "scanned": scanned})

        if route == "/api/playlist/fetch":
            from . import playlist as _pl
            try:
                return self._json({"ok": True, **_pl.fetch(str(payload.get("input") or ""))})
            except _pl.PlaylistError as exc:
                raise ApiError(str(exc)) from None

        if route == "/api/playlist/save":
            from . import playlist as _pl
            try:
                entry = _pl.add_local(payload.get("playlist"))
            except (OSError, ValueError, _pl.PlaylistError) as exc:
                # PlaylistError：本地 playlists.json 读不出来（被占用/拒绝访问），
                # 为了不覆盖已有收藏而主动放弃这次保存。
                raise ApiError("保存失败：" + str(exc)[:100]) from None
            return self._json({"ok": True, "playlist": entry})

        if route == "/api/playlist/list":
            from . import playlist as _pl
            return self._json({"ok": True, "playlists": _pl.load_local()})

        if route == "/api/playlist/remove":
            from . import playlist as _pl
            site = str(payload.get("site") or "").strip()
            pid = str(payload.get("id") or "").strip()
            if not site or not pid:
                raise ApiError("缺少歌单标识：site 和 id 都不能为空")
            # 已经不在本地列表里也算完成（幂等）：以前这里回 {"ok": false}（没有 error），
            # 前端 api.js 会把它当成失败抛出 "请求失败：HTTP 200"，按钮就卡住了。
            try:
                removed = _pl.remove_local(site, pid)
            except (OSError, ValueError, _pl.PlaylistError) as exc:
                raise ApiError("删除失败：" + str(exc)[:140]) from None
            return self._json({"ok": True, "removed": removed})

        if route == "/api/ffmpeg/status":
            from . import ffmpeg_setup as _fs
            return self._json({"ok": True, **_fs.status(),
                               "sources": [s[0] for s in _fs.SOURCES]})

        if route == "/api/ffmpeg/install":
            from . import ffmpeg_setup as _fs
            try:
                result = _fs.install()
            except _fs.FfmpegError as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True, **result})

        # ---------------- bigjpg 图片放大 ----------------
        if route == "/api/bigjpg/info":
            from . import bigjpg as _bj
            # 免费通道不需要任何凭据，所以 configured 恒为 True
            return self._json({"ok": True, "configured": True, "free": True,
                               **{k: v for k, v in _bj.describe().items() if k != "max_bytes"},
                               "max_mb": _bj.MAX_BYTES // 1048576})

        if route == "/api/bigjpg/submit":
            from . import bigjpg as _bj
            from pathlib import Path as _P

            paths = [str(_P(p)) for p in _as_str_list(payload, "paths", "图片路径")]
            if not paths:
                raise ApiError("还没有选择图片")
            style = str(payload.get("style") or "photo")
            noise = str(payload.get("noise") or "0")
            x2 = str(payload.get("x2") or "1")
            # 参数先校验再上传：以前 style 写错也会先把图片传到 bigjpg 的服务器，
            # 然后才在 create_task 里报"style 只能是 art 或 photo"。
            if style not in _bj.STYLES:
                raise ApiError("放大模式只能是 art（卡通插画）或 photo（照片）")
            if noise not in _bj.NOISES:
                raise ApiError("降噪档位只能是 -1、0、1、2、3")
            if x2 not in _bj.SCALES:
                raise ApiError("放大倍数只能是 1、2、3、4")
            results = []
            for path in paths:
                try:
                    upload = _bj.upload_image(ctx.settings.bigjpg_key, _P(path))
                    info = _bj.create_task(ctx.settings.bigjpg_key, style=style, noise=noise,
                                           x2=x2, upload=upload)
                    results.append({"ok": True, "file": _P(path).name, "tid": info["tid"],
                                    "minute": info.get("minute")})
                except _bj.BigjpgError as exc:
                    results.append({"ok": False, "file": _P(path).name, "error": str(exc)})
            return self._json({"ok": True, "results": results})

        if route == "/api/bigjpg/query":
            from . import bigjpg as _bj
            tids = _as_str_list(payload, "tids", "任务号")
            if not tids:
                raise ApiError("没有要查询的任务")
            try:
                raw = _bj.query_tasks(tids)
            except _bj.BigjpgError as exc:
                raise ApiError(str(exc)) from None
            items = []
            for tid in tids:
                info = raw.get(tid) or {}
                status = str(info.get("status") or "new")
                items.append({"tid": tid, "status": status,
                              "status_text": _bj.STATUS_TEXT.get(status, status or "未知"),
                              "url": info.get("url") or "",
                              "size": info.get("size") or ""})
            return self._json({"ok": True, "items": items})

        if route == "/api/bigjpg/retry":
            from . import bigjpg as _bj
            tids = _as_str_list(payload, "tids", "任务号")
            if not tids:
                raise ApiError("没有要重试的任务")
            try:
                _bj.retry_tasks(tids)
            except _bj.BigjpgError as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True})

        if route == "/api/local/decryptable":
            from .localfile import scan_paths
            return self._json({"ok": True, "items": scan_paths(payload.get("paths") or [])})

        if route == "/api/tasks/convert":
            paths = payload.get("paths") or []
            if not paths:
                raise ApiError("请先选择要转换的文件")
            created = ctx.tasks.submit_convert(
                paths,
                target=str(payload.get("target") or "copy"),
                bitrate=str(payload.get("bitrate") or ""),
            )
            if not created:
                raise ApiError("没有有效的文件")
            return self._json({"ok": True, "tasks": [t.to_dict() for t in created],
                               "count": len(created)})

        if route == "/api/tasks/cancel":
            ok = ctx.tasks.cancel(payload.get("id") or "")
            return self._json({"ok": ok})

        if route == "/api/tasks/retry":
            task = ctx.tasks.retry(payload.get("id") or "")
            if not task:
                raise ApiError("任务不存在", 404)
            return self._json({"ok": True, "task": task.to_dict()})

        if route == "/api/tasks/preview-remove":
            # 真正删除之前，把会删掉的文件逐条列给用户确认。
            task = ctx.tasks.get(str(payload.get("id") or ""))
            if not task:
                raise ApiError("任务不存在")
            items = ctx.tasks.files_to_delete(task)
            return self._json({"ok": True, "items": items,
                               "total": sum(i.get("size") or 0 for i in items),
                               "directory": task.directory or ""})

        if route == "/api/tasks/remove":
            ok = ctx.tasks.remove(payload.get("id") or "",
                                  delete_files=bool(payload.get("delete_files")))
            if not ok:
                raise ApiError("任务正在运行，无法删除", 409)
            return self._json({"ok": True})

        if route == "/api/tasks/clear":
            return self._json({"ok": True, "removed": ctx.tasks.clear_finished()})

        if route == "/api/settings":
            patch = payload.get("settings") or {}
            ctx.update(patch)
            return self._json({"ok": True, "settings": _public_settings(ctx)})

        if route == "/api/engines/order":
            order = payload.get("order") or []
            enabled = payload.get("enabled")
            patch = {"engine_order": [e for e in order if e in ALL_ENGINES]}
            if isinstance(enabled, list):
                patch["enabled_engines"] = [e for e in enabled if e in ALL_ENGINES]
            ctx.update(patch)
            return self._json({"ok": True, "engines": describe(ctx)})

        if route == "/api/open":
            target = payload.get("path") or str(ctx.download_dir())
            return self._json({"ok": True, "detail": _open_in_explorer(target)})

        if route == "/api/update/check":
            from .updater import check_update
            return self._json({"ok": True, **check_update()})

        if route == "/api/update/verify":
            from .updater import verify
            return self._json({"ok": True, **verify()})

        if route == "/api/update/repair":
            from .updater import repair
            return self._json({"ok": True, **repair()})

        if route == "/api/update/download":
            from .updater import check_update, download_installer
            return self._json({"ok": True, **download_installer(check_update())})

        if route == "/api/update/launch":
            from .updater import launch_installer
            return self._json({"ok": True,
                               **launch_installer(payload.get("path") or "")})

        if route == "/api/update/notes":
            return self._json({"ok": True, "notes": _CHANGELOG})

        if route == "/api/update/apply":
            # 下载（如需要）→ 起后台脚本 → 脚本等我们退出后静默安装并重启。
            # 必须先把响应发出去再退出，否则界面收不到结果。
            from .updater import UpdaterError, apply_auto_update
            try:
                result = apply_auto_update()
            except UpdaterError as exc:
                raise ApiError(str(exc)) from None
            self._json({"ok": True, **result, "restarting": True})

            import threading as _threading

            def _restart_later():
                import time as _time
                _time.sleep(1.2)          # 给响应一点时间发出去
                self.server.shutdown()
                callback = getattr(ctx, "on_quit", None)
                if callable(callback):
                    try:
                        callback()
                    except Exception:
                        pass

            _threading.Thread(target=_restart_later, daemon=True, name="auto-update").start()
            return

        if route == "/api/quit":
            # 桌面版没有控制台窗口，只能从界面里退出。
            # 另起一个线程调用 shutdown()，否则会和当前请求的处理线程互相等待。
            import threading as _threading

            def _stop():
                self.server.shutdown()
                # 桌面窗口模式下还要把窗口一起关掉，否则会留下一个空白窗口
                callback = getattr(ctx, "on_quit", None)
                if callable(callback):
                    try:
                        callback()
                    except Exception:
                        pass

            _threading.Thread(target=_stop, daemon=True).start()
            return self._json({"ok": True, "detail": "已退出"})

        if route == "/api/tools/install":
            from .engines.e8_external_tool import install_ytdlp
            result = install_ytdlp()
            return self._json({"ok": result["ok"], **result})

        if route == "/api/tools/jx":
            from .engines.e7_thirdparty_jx import ThirdPartyJxEngine
            bvid = (payload.get("bvid") or "BV1GJ411x7h7").strip()
            engine = ThirdPartyJxEngine()
            return self._json({"ok": True, "bvid": bvid,
                               "providers": engine.probe_all(ctx, bvid)})

        if route == "/api/login/check":
            return self._json({"ok": True, **_check_login(ctx)})

        return self._error("未知接口", 404)

    # ---------------- 静态资源 ----------------
    def _serve_static(self, route: str) -> None:
        if route in ("/", ""):
            route = "/index.html"
        relative = unquote(route).lstrip("/")
        target = (WEB_DIR / relative).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._error("非法路径", 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            # 前端用 history 路由，未知路径一律回落到 index.html
            target = WEB_DIR / "index.html"
            if not target.exists():
                return self._error("界面文件缺失", 404)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript", "application/json", "image/svg+xml"
        ):
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self._send(200, body, content_type)

    # ---------------- 封面代理 ----------------
    # B 站图床带 Referer 防盗链，浏览器里直接 <img src> 会被 403，
    # 所以统一由后端带上正确的 Referer 转发一次。
    # 各平台图床的防盗链要求不一样：除了放行域名，还得带上对应平台的 Referer。
    IMAGE_HOST_REFERER = {
        # 网易云的封面走 music.126.net（p1/p2/… 前缀）；缺了它任务卡封面全裂
        "126.net": "https://music.163.com/",
        "hdslb.com": "https://www.bilibili.com/",
        "bilibili.com": "https://www.bilibili.com/",
        "bilivideo.com": "https://www.bilibili.com/",
        "biliimg.com": "https://www.bilibili.com/",
        "douyinpic.com": "https://www.douyin.com/",
        "douyincdn.com": "https://www.douyin.com/",
        "douyin.com": "https://www.douyin.com/",
        "xhscdn.com": "https://www.xiaohongshu.com/",
        "xiaohongshu.com": "https://www.xiaohongshu.com/",
        "rednote.com": "https://www.rednote.com/",
        "mgtv.com": "https://www.mgtv.com/",
        "qpic.cn": "https://v.qq.com/",
        "qq.com": "https://v.qq.com/",
        "ykimg.com": "https://www.youku.com/",
        "youku.com": "https://www.youku.com/",
        "iqiyipic.com": "https://www.iqiyi.com/",
        "iqiyi.com": "https://www.iqiyi.com/",
    }

    def _cover_referer(self, host: str) -> str:
        """按图床域名查 Referer。白名单外的域名返回空字符串。

        这里**只认域名**，不接受调用方传入的 Referer —— 否则任何人只要塞一个
        合法的 http 地址进来，就能让本机进程去请求任意内网地址（SSRF），
        还能自定义发出去的 Referer。
        """
        for suffix, referer in self.IMAGE_HOST_REFERER.items():
            if host == suffix or host.endswith("." + suffix):
                return referer
        return ""

    def _serve_cover(self) -> None:
        query = self._query()
        url = query.get("url") or ""
        if not url.startswith("http"):
            return self._error("缺少合法的图片地址", 400)
        host = (urlparse(url).hostname or "").lower()
        referer = self._cover_referer(host)
        if not referer:
            return self._error("只允许代理受支持平台的图片地址", 403)
        try:
            # 这是"跟着用户给的地址走"的请求，绝不能带上 B 站登录态
            res = self.ctx.net.request(url, retries=2, referer=referer, allow_cookie=False)
        except Exception as exc:
            return self._error(f"取图失败：{exc}", 502)
        content_type = res.header("Content-Type") or "image/jpeg"
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        self._send(200, res.body, content_type,
                   cache="public, max-age=86400")

    # ---------------- 本地文件播放（支持 Range） ----------------
    def _serve_media(self) -> None:
        ctx = self.ctx
        query = self._query()
        raw = query.get("path") or ""
        if not raw:
            return self._error("缺少 path 参数")
        target = Path(raw)
        try:
            target = target.resolve()
        except OSError:
            return self._error("路径无效", 400)
        # 只允许播放本程序自己产出的文件。否则本机任何进程都能通过这个接口
        # 读走磁盘上的任意文件（路径穿越）。
        if not _inside_allowed(target, ctx):
            return self._error("只能播放下载目录里的文件", 403)
        if not target.exists() or not target.is_file():
            return self._error("文件不存在", 404)
        size = target.stat().st_size
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match and (match.group(1) or match.group(2)):
                first, second = match.group(1), match.group(2)
                if not first:
                    # 后缀形式 bytes=-N：要的是**最后** N 字节。
                    # 以前当成"从 0 开始"，会返回文件开头的内容，完全不是请求方要的。
                    count = int(second)
                    start = max(0, size - count) if count else 0
                    end = size - 1
                else:
                    start = int(first)
                    end = min(int(second), size - 1) if second else size - 1
                if start > size - 1:
                    # 起点越界：按规范回 416，而不是发出 start > end 的非法 Content-Range
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def _webview_status() -> dict:
    """WebView2 运行时的状态，给设置页显示用。任何异常都不该影响主接口。"""
    try:
        from .webview_runtime import status as _status
        return _status()
    except Exception as exc:
        return {"mode": "unknown", "ok": False, "detail": str(exc)[:120],
                "system_version": "", "fixed_dir": "", "fixed_expected": "",
                "bootstrapper": "", "has_bootstrapper": False}


def _prepare_ref(ctx: Context, raw: str):
    """解析用户输入：先判断平台，再交给该平台做联网补全（短链展开等）。

    适配器在"认不出这个链接"时会抛 SiteUnavailable。对调用方来说这属于
    输入问题而不是程序故障，所以统一转成 400，别让用户看到"服务器内部错误"。
    """
    from .sites.base import SiteUnavailable

    try:
        ref = parse_any(raw)
        return get_site(ref.site).normalize(ctx, ref)
    except SiteUnavailable as exc:
        raise ApiError(str(exc)) from None


def _inside_allowed(target: Path, ctx: Context) -> bool:
    """判断路径是否落在允许对外播放的目录内。"""
    # 音乐目录必须放行：默认它在 <下载目录>/音乐 之下，一旦用户在设置里
    # 指定了别的音乐目录，产物就不在下载目录里了，「播放」页/任务卡片点开会 403。
    # 只放行真正存放媒体文件的目录。
    # DATA_DIR 曾经也在这里 —— 但里面是 config.json（含 SESSDATA 等凭据），
    # 等于多开一条经 /media 读凭据的路。媒体文件不会放在 data/ 下，去掉是安全的。
    allowed = [ctx.download_dir()]
    try:
        allowed.append(ctx.music_dir())
    except Exception:
        pass
    for root in allowed:
        try:
            target.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _normal_path(value) -> str:
    r"""把路径化成可以互相比较的形式：绝对、展开 ..、解析 junction/软链接、忽略大小写。

    只做 abspath 是不够的：用户可以在 downloads\音乐 里放一个指向 C:\ 的 junction，
    abspath 看不出来，realpath 会把它展开成真实位置。
    """
    text = str(value)
    try:
        return os.path.normcase(os.path.realpath(text))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(text))


def _inside_output_root(target, ctx: Context, src: Path | None = None) -> bool:
    r"""输出目录只允许落在下载目录 / 音乐目录里，或者视频文件自己所在的目录。

    背景：/api/video/trim 的 dest 原本原样传给 transcode.trim_video，而那边直接
    out_dir.mkdir(parents=True) + ffmpeg -y。也就是说任何能带上令牌调用本机接口的
    进程（本机任何程序）都能让服务端往任意目录写视频文件：dest="C:\Windows"
    会真的去走 C:\Windows\xxx.part-<pid>.mp4 这条路径（只是被 NTFS 权限拦下），
    dest="C:\Users\Public\x" 这种可写目录则会直接成功。裁剪结果没有任何理由
    离开这两个受管目录。
    """
    want = _normal_path(target)
    if src is not None:
        try:
            if want == _normal_path(src.parent):
                return True
        except OSError:
            pass
    for root in (ctx.download_dir(), ctx.music_dir()):
        base = _normal_path(root)
        if want == base or want.startswith(base + os.sep):
            return True
    return False


def _as_str_list(payload: dict, key: str, what: str) -> list:
    """从请求体里取一组字符串（一组路径 / 一组任务号）。

    以前这里写的是 [str(x) for x in (payload.get(key) or [])]：
      · 传整数 -> "TypeError: 'int' object is not iterable"，也就是 500；
      · 传字符串 "abc" -> 被拆成 a、b、c 三项，静默按三个路径处理。
    两种都不该发生，所以统一在这里收敛成 400。
    """
    value = payload.get(key)
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        raise ApiError(f"{what}格式不对：应该是一组{what}")
    items = []
    for index, item in enumerate(value, 1):
        if isinstance(item, bool) or item is None or isinstance(item, (dict, list, tuple)):
            raise ApiError(f"{what}格式不对：第 {index} 项不是字符串")
        item = str(item) if isinstance(item, (int, float)) else item
        if not isinstance(item, str):
            raise ApiError(f"{what}格式不对：第 {index} 项不是字符串")
        if item.strip():
            items.append(item)
    return items


def _is_reparse_point(path: str) -> bool:
    """目录是不是 junction / 符号链接。

    os.walk(followlinks=False) 只跳符号链接；Windows 的 junction 在 Python 里
    os.path.islink() 为 False、os.path.isjunction() 为 True，照样会被走进去。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if os.path.islink(path):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attrs and flag and (attrs & flag))


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _open_in_explorer(target: str) -> str:
    path = Path(target)
    try:
        if os.name == "nt":
            if path.is_file():
                os.startfile(str(path.parent))  # type: ignore[attr-defined]
            else:
                path.mkdir(parents=True, exist_ok=True)
                os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(path if path.is_dir() else path.parent)])
        return f"已打开 {path}"
    except Exception as exc:
        return f"打开失败：{exc}"


def _check_login(ctx: Context) -> dict:
    settings = ctx.settings
    if not settings.sessdata.strip():
        return {"logged_in": False, "name": "", "vip": False,
                "detail": "未配置 SESSDATA，最高只能拿到 480P 左右"}
    try:
        data = ctx.net.api("https://api.bilibili.com/x/web-interface/nav")
    except Exception as exc:
        return {"logged_in": False, "name": "", "vip": False, "detail": f"校验失败：{exc}"}
    is_login = bool(data.get("isLogin"))
    vip = bool((data.get("vipStatus") or 0) == 1)
    return {
        "logged_in": is_login,
        "name": data.get("uname") or "",
        "vip": vip,
        "level": (data.get("level_info") or {}).get("current_level"),
        "detail": "登录态有效" + ("（大会员）" if vip else "（普通用户）") if is_login else "SESSDATA 无效或已过期",
    }


def serve(ctx: Context, host: str = "127.0.0.1", port: int = 8931, quiet: bool = True):
    ctx.tasks = TaskManager(ctx)
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.quiet = quiet  # type: ignore[attr-defined]
    return httpd

