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
from . import projects_api
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
        "app": "MakiseToolkit",
        "app_cn": "MakiseToolkit",
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
        "bili_name": _bili_login()[1],
        "logged_in": _bili_login()[0],
        "download_dir": str(ctx.download_dir()),
        # 四个分类目录的真实路径（设置留空时就是算出来的默认值），
        # 界面上直接显示，用户才知道文件到底落在哪
        "dirs": {
            "download": str(ctx.download_dir()),
            "video": str(ctx.video_dir()),
            "music": str(ctx.music_dir()),
            "document": str(ctx.doc_root()),
            "image": str(ctx.image_dir()),
        },
        # 前端拿它给所有写操作签名；跨站页面读不到这个响应（CORS），拿不到令牌
        "api_token": getattr(ctx, "api_token", ""),
        "window_mode": getattr(ctx, "window_mode", "") or "server",
        # 前端靠它决定要不要画自绘标题栏 —— 之前漏了这个字段，
        # 结果原生标题栏被去掉了、HTML 标题栏又没画，窗口一个按钮都没有。
        "dblclick_ms": _dblclick_ms(),
        "titlebar": (
            "custom"
            if (getattr(ctx, "window_mode", "") == "webview"
                and (getattr(ctx.settings, "titlebar", "") or "").strip().lower() == "custom")
            else "native"
        ),
        "frozen": is_frozen(),
        "base_dir": str(BASE_DIR),
    }


_BIGJPG_HISTORY = Path(__file__).resolve().parent.parent / "data" / "bigjpg.json"
_BIGJPG_LOCK = __import__("threading").Lock()


def _load_bigjpg() -> list:
    try:
        import json as _json
        items = _json.loads(_BIGJPG_HISTORY.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _record_bigjpg(result: dict, *, style: str, noise: str, x2: str) -> None:
    """把一次放大任务记进 data/bigjpg.json，历史记录页要能看到。

    以前放大结果是纯内存的，页面一刷新就没了，用户也没法回头找。
    """
    import json as _json
    import time as _time
    try:
        with _BIGJPG_LOCK:
            items = _load_bigjpg()
            items.insert(0, {
                "file": result.get("file") or "",
                "ok": bool(result.get("ok")),
                "tid": result.get("tid") or "",
                "error": result.get("error") or "",
                "style": style, "noise": noise, "x2": x2,
                "time": int(_time.time()),
            })
            del items[300:]
            _BIGJPG_HISTORY.parent.mkdir(parents=True, exist_ok=True)
            _BIGJPG_HISTORY.write_text(_json.dumps(items, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    except Exception:
        pass


_BILI_LOGIN_CACHE = {"at": 0.0, "ok": False, "name": ""}


def _bili_login() -> tuple:
    """B 站登录态：真的问一次 nav 接口，缓存 5 分钟。

    原来只看"SESSDATA 非空"，Cookie 过期了界面还显示已登录 —— 用户会以为
    1080P 拿不到是程序的锅。现在按接口的真实结果来。
    """
    import time as _t
    now = _t.time()
    if now - _BILI_LOGIN_CACHE["at"] < 300:
        return _BILI_LOGIN_CACHE["ok"], _BILI_LOGIN_CACHE["name"]
    ok, name = False, ""
    try:
        from . import credentials as _cred
        r = _cred.check("bilibili", "")
        ok = bool(r.get("ok"))
        detail = str(r.get("detail") or "")
        if ok and "：" in detail:
            name = detail.split("：", 1)[1].strip()
    except Exception:
        pass
    _BILI_LOGIN_CACHE.update({"at": now, "ok": ok, "name": name})
    return ok, name


def _dblclick_ms() -> int:
    """系统双击间隔。前端写死 420ms 会和系统设置对不上。"""
    try:
        from .dpi import double_click_ms
        return double_click_ms()
    except Exception:
        return 500


# 启动时缓存两个目录的真实值。改目录要拿它当「旧目录」——
# 不依赖前端传值，用户点几次「选择…」都不会判断错。
_BOOT_DIRS = {}


def remember_boot_dirs(ctx) -> None:
    for field in ("download_dir", "music_dir", "video_dir", "image_dir", "doc_dir"):
        try:
            _BOOT_DIRS[field] = str(getattr(ctx, field)())
        except Exception:
            pass


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
    server_version = "MakiseToolkit"
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
        return self.headers.get("X-MakiseToolkit-Token") == expected

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
            from .log import log_error as _le2
            _le2("api:" + route, exc)
            return self._error(f"服务器内部错误：{exc}", 500)

    def _activate_window(self) -> None:
        """把本进程的窗口拉到前台（第二次双击时用，避免开第二个 WebView 窗口）。"""
        try:
            from . import window_frame as _wf
            hwnd = _wf.find_window("")
            if not hwnd:
                return self._json({"ok": False, "error": "没找到窗口"})
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)        # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return self._json({"ok": True})
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)[:120]})

    # ---------------- 项目管理 ----------------
    def _project_api(self, route: str, data: dict) -> None:
        try:
            return self._json({"ok": True, **projects_api.dispatch(route, data)})
        except projects_api.ApiError as exc:
            return self._error(str(exc), 400)

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
            # 单实例检测用：另一个进程靠它判断"这个端口上是不是已经有一个 MakiseToolkit"
            return self._json({"ok": True, "app": "MakiseToolkit", "version": __version__})
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
        if route == "/api/window/activate":
            return self._activate_window()
        if route.startswith("/api/project"):
            return self._project_api(route, self._body())
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

        if route == "/api/window/resize":
            from .titlebar import start_resize
            from .window_frame import find_window
            from .desktop import WINDOW_TITLE
            hwnd = find_window(WINDOW_TITLE) or find_window("")
            if not hwnd:
                raise ApiError("找不到窗口")
            return self._json({"ok": True,
                               "resizing": start_resize(hwnd, str(payload.get("edge") or ""))})

        if route == "/api/window/ready":
            # 前端起来了。自绘标题栏的逃生口靠它判断：收不到就别用自绘。
            ctx.frontend_ready = True
            return self._json({"ok": True})

        if route == "/api/window/frontend-error":
            # 前端自己遇到的异常也写进同一份日志，免得只能靠用户复述现象
            from .log import log_error
            log_error("frontend", str(payload.get("message") or "")[:600],
                      str(payload.get("where") or "")[:120])
            return self._json({"ok": True})

        if route == "/api/window/drag":
            # 页面按下标题栏 -> 让系统接管拖动（WebView2 跨进程，命中测试穿不过去）
            from .titlebar import start_drag
            from .window_frame import find_window
            from .desktop import WINDOW_TITLE
            hwnd = find_window(WINDOW_TITLE) or find_window("")
            if not hwnd:
                raise ApiError("找不到窗口")
            return self._json({"ok": True, "dragging": start_drag(hwnd)})

        if route in ("/api/window/minimize", "/api/window/maximize",
                     "/api/window/close"):
            # 自绘标题栏的按钮走这里 —— 页面里没有 pywebview 的对象，
            # 只能由后端代劳。窗口引用在建窗时挂到 ctx 上。
            win = getattr(ctx, "window", None)
            if win is None:
                raise ApiError("当前不是窗口模式，无法控制窗口")
            action = route.rsplit("/", 1)[-1]
            try:
                if action == "minimize":
                    win.minimize()
                elif action == "maximize":
                    # 用系统真实状态判断，不用缓存的标志
                    from .titlebar import is_zoomed
                    from .window_frame import find_window
                    from .desktop import WINDOW_TITLE
                    hwnd = find_window(WINDOW_TITLE) or find_window("")
                    if is_zoomed(hwnd):
                        win.restore()
                    else:
                        win.maximize()
                else:
                    # 「叉掉」不退出程序：隐藏窗口留到托盘里，用户从托盘菜单退出。
                    # 托盘没起来时才真的退出，否则窗口藏了就再也找不回来。
                    tray = getattr(ctx, "tray", None)
                    hidden = False
                    if tray is not None:
                        # 这个版本的 pywebview 没有可用的 hide()，必须直接调
                        # Win32 ShowWindow(SW_HIDE)，否则只会最小化，看着像"叉不掉"。
                        try:
                            import ctypes as _ct
                            from .window_frame import find_window as _fw
                            from .desktop import WINDOW_TITLE as _T
                            hwnd = _fw(_T) or _fw("")
                            if hwnd:
                                _ct.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
                                hidden = True
                        except Exception:
                            hidden = False
                        if not hidden:
                            try:
                                win.minimize()
                                hidden = True
                            except Exception:
                                hidden = False
                    if not hidden and ctx.on_quit:
                        ctx.on_quit()
            except Exception as exc:
                raise ApiError("窗口操作失败：" + str(exc)[:100]) from None
            return self._json({"ok": True, "action": action})

        if route == "/api/window/frame":
            # 界面算好 Material 3 的颜色后下发过来，我们应用到 Windows 的窗口边框上。
            # 前端拿不到 HWND，所以这件事必须由后端做。
            from .window_frame import apply_current
            payload = payload or {}
            from .desktop import WINDOW_TITLE
            ok = apply_current(
                title=WINDOW_TITLE,
                caption=str(payload.get("caption") or ""),
                text=str(payload.get("text") or ""),
                border=str(payload.get("border") or ""),
                dark=bool(payload.get("dark")),
            )
            if ok and (getattr(ctx.settings, "titlebar", "") or "").lower() == "custom":
                # 自绘模式下子窗口是内缩的，最外圈由父窗口绘制，得刷上主题底色
                try:
                    from .titlebar import set_background
                    from .window_frame import find_window, _colorref
                    from .desktop import WINDOW_TITLE
                    hwnd = find_window(WINDOW_TITLE) or find_window("")
                    if hwnd:
                        set_background(hwnd, _colorref(str(payload.get("caption") or "")))
                except Exception:
                    pass
            return self._json({"ok": True, "applied": ok})

        if route == "/api/local/pick":
            from .localfile import pick_files
            paths = pick_files(self.ctx, kind=str(payload.get("kind") or "music"))
            _remember_picked(paths)          # 选过就允许播放，剪辑页要用
            return self._json({"ok": True, "paths": paths, "count": len(paths)})

        # 注意：请求体在 _api_post 开头已经读过一次了（rfile 是一次性的），
        # 这里绝不能再调 self._body()，否则会阻塞在一个已经读空的流上。
        if route == "/api/local/pickdir":
            from .localfile import pick_folder
            from .config import BASE_DIR
            # field 决定选完之后写哪个设置项：视频目录还是音乐目录
            field = str(payload.get("field") or "download_dir")
            if field not in ("download_dir", "music_dir", "video_dir", "image_dir", "doc_dir"):
                raise ApiError("不支持设置 " + field)
            current = str(ctx.music_dir() if field == "music_dir" else ctx.download_dir())
            chosen = pick_folder(ctx, current)
            if chosen:
                ctx.update({field: chosen})
            return self._json({"ok": True, "path": chosen or "", "field": field,
                               "cancelled": not chosen,
                               "download_dir": str(ctx.download_dir()),
                               "music_dir": str(ctx.music_dir())})

        if route == "/api/papers/list":
            folder = ctx.download_dir() / "document" / "papers"
            items = []
            if folder.is_dir():
                for f in sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
                    st = f.stat()
                    items.append({"name": f.name, "path": str(f), "size": st.st_size,
                                  "mtime": int(st.st_mtime)})
            return self._json({"items": items, "dir": str(folder), "total": len(items)})

        if route == "/api/papers/download":
            from .papers import download_pdf, PaperError
            url = str(payload.get("url") or "").strip()
            if not url:
                raise ApiError("没有指定下载地址")
            dest = ctx.download_dir() / "document" / "papers"
            try:
                got = download_pdf(url, dest, str(payload.get("name") or ""))
            except PaperError as exc:
                raise ApiError(str(exc)) from None

            # 下载即入库：把搜索结果里的元数据一起存下来，
            # 之后能直接生成引用、能挂到项目节点上。
            entry = {}
            try:
                from . import library as _lib
                meta = payload.get("meta") or {}
                entry = {
                    "title": str(meta.get("title") or got.get("name") or ""),
                    "authors": meta.get("authors") or meta.get("author") or "",
                    "year": meta.get("year") or "",
                    "venue": meta.get("venue") or meta.get("journal") or "",
                    "doi": meta.get("doi") or "",
                    "volume": meta.get("volume") or "",
                    "issue": meta.get("issue") or "",
                    "pages": meta.get("pages") or "",
                    "source": meta.get("source") or "",
                    "url": meta.get("url") or url,
                    "oa": meta.get("oa"),
                    "file": str(got.get("path") or ""),
                    "size": got.get("size") or 0,
                }
                entry = _lib.add(entry)
            except Exception as exc:          # 入库失败不能影响下载本身
                entry = {"error": str(exc)[:120]}
            got["library"] = entry
            return self._json(got)

        if route == "/api/papers/library":
            from . import library as _lib
            data = _lib.load()
            items = data.get("items") or []
            for item in items:
                path = item.get("file") or ""
                item["exists"] = bool(path) and __import__("os").path.isfile(path)
            return self._json({"items": items, "styles": _lib.STYLES, "total": len(items)})

        if route == "/api/papers/export":
            from . import library as _lib
            fmt = str(payload.get("format") or "bibtex").lower()
            wanted = payload.get("ids")
            items = _lib.load().get("items") or []
            if isinstance(wanted, list) and wanted:
                items = [x for x in items if x.get("id") in set(wanted)]
            if not items:
                raise ApiError("文献库里没有可导出的条目")
            text = _lib.to_ris(items) if fmt == "ris" else _lib.to_bibtex(items)
            return self._json({"text": text, "format": fmt, "count": len(items)})

        if route == "/api/papers/cite":
            from . import library as _lib
            entry = _lib.find(str(payload.get("id") or ""))
            if not entry:
                raise ApiError("文献库里没有这一条")
            return self._json({"text": _lib.citation(entry, str(payload.get("style") or "gbt7714")),
                               "style": str(payload.get("style") or "gbt7714")})

        if route == "/api/papers/library/remove":
            from . import library as _lib
            ok = _lib.remove(str(payload.get("id") or ""))
            return self._json({"ok": ok})

        if route == "/api/papers/search":
            from .papers import search as _paper_search, PaperError
            try:
                result = _paper_search(
                    str(payload.get("query") or ""),
                    payload.get("sources") or None,
                    int(payload.get("limit") or 10),
                    int(payload.get("year_from") or 0),
                    int(payload.get("year_to") or 0),
                    bool(payload.get("oa_only")),
                    str(payload.get("sort") or "relevance"))
            except PaperError as exc:
                raise ApiError(str(exc)) from None
            return self._json(result)

        if route == "/api/doc/cancel":
            from .convert import request_cancel
            request_cancel()
            return self._json({"ok": True})

        if route == "/api/shell/open":
            # 只允许打开"我们自己产出的目录"里的东西。
            # 不设白名单的话，这个接口等于给页面一个任意执行文件的入口。
            import os as _os
            import subprocess as _sp
            raw = str(payload.get("path") or "").strip()
            if not raw:
                raise ApiError("没有指定要打开的路径")
            target = Path(raw).resolve()
            roots = [ctx.download_dir().resolve()]
            for _getter in ("video_dir", "image_dir"):
                try:
                    roots.append(getattr(ctx, _getter)().resolve())
                except Exception:
                    pass
            try:
                roots.append(ctx.doc_root().resolve())
            except Exception:
                pass
            # 音乐目录必须放行：用户在设置里把音乐目录指到下载目录之外时，
            # 「打开目录」会被这里挡掉，看着像功能坏了。
            try:
                roots.append(ctx.music_dir().resolve())
            except Exception:
                pass
            if not any(target == root or root in target.parents for root in roots):
                raise ApiError("只能打开下载目录里的文件")
            if not target.exists():
                raise ApiError("文件已经不在了：" + target.name)
            try:
                if target.is_dir():
                    _os.startfile(str(target))
                else:
                    # 文件一律用资源管理器"选中"，绝不 os.startfile。
                    # 直接 startfile 一个 .exe/.bat/.lnk 是**执行**而不是打开，
                    # 下载目录里恰恰什么类型的文件都可能存在。
                    _sp.Popen(["explorer", "/select,", str(target)])
            except OSError as exc:
                raise ApiError("打开失败：" + str(exc)[:120]) from None
            return self._json({"ok": True})

        if route == "/api/doc/convert":
            from . import convert as _cv
            paths = payload.get("paths")
            if not isinstance(paths, list):
                raise ApiError("文件路径格式不对：应该是一组文件路径")
            if not paths:
                raise ApiError("还没有选择文件")
            target = str(payload.get("target") or "").strip().lower()
            if target not in ("docx", "xlsx", "md", "pdf"):
                raise ApiError("目标格式只能是 docx、xlsx、md 或 pdf")
            dest = Path(payload.get("dest") or str(ctx.doc_root()))
            # 和 /api/video/trim 一样必须校验：dest 原本原样传给 convert，
            # 那边直接 mkdir + 写文件，等于给了调用方一个往任意目录写文件的口子。
            if not _inside_output_root(dest, ctx):
                raise ApiError("输出目录只能放在下载目录或音乐目录里")
            from .convert import clear_cancel, is_cancelled
            clear_cancel()
            done, failed = [], []
            for raw in paths:
                if is_cancelled():
                    failed.append({"path": str(raw), "error": "已中止"})
                    break
                try:
                    done.append(_cv.convert(str(raw), dest, target))
                except _cv.ConvertError as exc:
                    failed.append({"path": str(raw), "error": str(exc)})
                except (OSError, ValueError) as exc:
                    failed.append({"path": str(raw), "error": str(exc)[:140]})
            return self._json({"ok": True, "done": done, "failed": failed, "dir": str(dest)})

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

        if route == "/api/video/demux":
            from .music.transcode import extract_audio, extract_video
            from pathlib import Path as _P4
            src = str(payload.get("path") or "")
            if not src or not _P4(src).is_file():
                raise ApiError("找不到这个视频文件")
            mode = str(payload.get("mode") or "audio")
            if mode not in ("audio", "video"):
                raise ApiError("mode 只能是 audio 或 video")
            target = str(payload.get("target") or ("mp3" if mode == "audio" else "copy"))
            dest = _P4(src).parent
            try:
                if mode == "audio":
                    result = extract_audio(src, dest, target)
                else:
                    result = extract_video(src, dest, target)
            except Exception as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True, "mode": mode, **result})

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
                        # 同目录里通常还躺着下载时保存的封面（cover.jpg 之类），
                        # 顺手带上，歌单里就能显示封面了。
                        cover = ""
                        for probe in ("cover.jpg", "cover.png", "cover.webp", "folder.jpg"):
                            cand = _os.path.join(base, probe)
                            if _os.path.isfile(cand):
                                cover = cand
                                break
                        # 真正读音频标签。以前从文件名猜"歌手 - 歌名"，
                        # 结果把 "VALORANT 08-30-2026" 的日期拆成了作者。
                        from .music.tags import read_tags
                        tags = read_tags(full)
                        items.append({"name": name,
                                      "title": tags.get("title") or _os.path.splitext(name)[0],
                                      "artist": tags.get("artist") or "",
                                      "album": tags.get("album") or "",
                                      "path": full,
                                      # 同名文件靠所在文件夹区分
                                      "folder": _os.path.basename(base) if base.lower() != str(root).lower() else "",
                                      "cover": cover,
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

        if route in ("/api/settings/move-preview", "/api/settings/move-apply"):
            # 改下载目录时，问用户要不要把已有文件搬过去。
            # 前端先调 move-preview 拿数量，用户点"迁移"再调 move-apply。
            import os as _os
            import shutil as _shutil
            field = str(payload.get("field") or "download_dir")
            if field not in ("download_dir", "music_dir"):
                raise ApiError("只能迁移下载目录或音乐目录")
            # 旧目录由前端传进来。不能读 ctx：设置页的「选择…」按钮会先把新目录
            # 存进服务端，等用户点「保存」时 ctx 里已经是新值了，两下相等 →
            # 永远判断成"没改过"，迁移询问就不会出现（实测就是这样）。
            given_from = (str(payload.get("from") or "").strip()
                          or _BOOT_DIRS.get(field, ""))
            if given_from:
                old_root = Path(given_from)
            else:
                old_root = getattr(ctx, field)()
            raw_new = str(payload.get("value") or "").strip()
            if not raw_new:
                # 留空 = 恢复默认目录，同样要迁移
                raw_new = str(ctx.download_dir()) if field == "download_dir" else ""
            new_root = (Path(raw_new) if raw_new else old_root)
            try:
                old_r = old_root.resolve()
                new_r = new_root.resolve()
            except OSError as exc:
                raise ApiError("路径无法解析：" + str(exc)[:80]) from None
            if old_r == new_r:
                return self._json({"ok": True, "same": True, "files": 0, "bytes": 0,
                                   "from": str(old_r), "to": str(new_r)})
            # 互相包含时拒绝：会把文件搬进自己里面，越搬越深
            if old_r in new_r.parents or new_r in old_r.parents:
                raise ApiError("新旧目录不能是父子关系")
            if not old_r.is_dir():
                return self._json({"ok": True, "same": False, "files": 0, "bytes": 0,
                                   "from": str(old_r), "to": str(new_r)})
            total = 0
            size = 0
            for item in old_r.rglob("*"):
                try:
                    if item.is_file() and not item.is_symlink():
                        total += 1
                        size += item.stat().st_size
                except OSError:
                    continue
            if route == "/api/settings/move-preview":
                return self._json({"ok": True, "same": False, "files": total, "bytes": size,
                                   "from": str(old_r), "to": str(new_r)})
            # 真正搬：逐文件移动，保留相对路径；失败只记录，不中断
            new_r.mkdir(parents=True, exist_ok=True)
            moved = failed = moved_bytes = skipped = 0
            skipped_names = []
            errors = []
            for item in list(old_r.rglob("*")):
                try:
                    if not item.is_file() or item.is_symlink():
                        continue
                    rel = item.relative_to(old_r)
                    dest = new_r / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        # 同名就跳过，绝不动已有文件 —— 迁移是搬运，不是覆盖。
                        # 覆盖掉的旧文件是恢复不了的，宁可留着让用户自己判断。
                        skipped += 1
                        if len(skipped_names) < 5:
                            skipped_names.append(rel.name)
                        continue
                    size_now = item.stat().st_size
                    _shutil.move(str(item), str(dest))
                    moved += 1
                    moved_bytes += size_now
                except Exception as exc:
                    failed += 1
                    if len(errors) < 3:
                        errors.append(item.name + ": " + str(exc)[:60])
            return self._json({"ok": True, "moved": moved, "failed": failed,
                               "skipped": skipped, "skipped_names": skipped_names,
                               "bytes": moved_bytes, "errors": errors,
                               "from": str(old_r), "to": str(new_r)})
        if route == "/api/credentials/check":
            from . import credentials as _cred
            site = str(payload.get("site") or "").strip().lower()
            if not site:
                raise ApiError("没有指定平台")
            result = _cred.check(site, str(payload.get("value") or ""))
            return self._json(result)

        if route == "/api/playlist/import":
            # 音乐页粘贴歌单链接 -> 新建同名歌单 -> 排前几首的下载任务。
            # 一次只排 5 个，剩下的等前面的完成后再由用户点一次「继续下载」，
            # 免得一口气塞几十个任务把队列和平台限速都顶爆。
            from . import playlist as _pl
            raw = str(payload.get("input") or "").strip()
            try:
                parsed = _pl.fetch(raw)
            except _pl.PlaylistError as exc:
                raise ApiError(str(exc)) from None
            songs = parsed.get("songs") or []
            # 用户在界面上勾选了哪几首就只导那几首；没传就全导
            only = payload.get("only")
            if isinstance(only, list) and only:
                wanted = {str(u) for u in only}
                songs = [s for s in songs if str(s.get("url")) in wanted]
            if not songs:
                raise ApiError("这个歌单里没有解析到歌曲，可能是空的或者需要登录")
            title = (str(parsed.get("title") or "").strip() or "导入的歌单")[:60]
            try:
                pl = _pl.create_empty(title)
            except (OSError, ValueError) as exc:
                raise ApiError("新建歌单失败：" + str(exc)[:100]) from None
            limit = max(1, min(int(payload.get("limit") or 5), 20))
            queued, failed = 0, []
            for song in songs:
                entry = {
                    "url": str(song.get("url") or ""),
                    "name": str(song.get("name") or ""),
                    "artist": str(song.get("artist") or ""),
                    "cover": str(song.get("cover") or ""),
                }
                if not entry["url"] or not entry["name"]:
                    continue
                if not entry["url"].startswith(("http://", "https://")):
                    continue      # 本地文件不需要下载
                if queued < limit:
                    try:
                        task = ctx.tasks.submit(entry["url"], engine="auto",
                                                qn=0, mode="auto")
                        entry["task_id"] = task.id
                        queued += 1
                    except Exception as exc:
                        failed.append({"name": entry["name"], "error": str(exc)[:100]})
                try:
                    _pl.add_song("local", pl["id"], entry)
                except (OSError, ValueError):
                    pass
            return self._json({"ok": True, "playlist": _pl.find_local("local", pl["id"]) or pl,
                               "total": len(songs), "queued": queued, "failed": failed})

        if route == "/api/playlist/save":
            from . import playlist as _pl
            try:
                entry = _pl.add_local(payload.get("playlist"))
            except (OSError, ValueError, _pl.PlaylistError) as exc:
                # PlaylistError：本地 playlists.json 读不出来（被占用/拒绝访问），
                # 为了不覆盖已有收藏而主动放弃这次保存。
                raise ApiError("保存失败：" + str(exc)[:100]) from None
            return self._json({"ok": True, "playlist": entry})

        if route == "/api/playlist/create":
            from . import playlist as _pl
            try:
                entry = _pl.create_empty(str(payload.get("title") or ""))
            except (ValueError, OSError) as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True, "playlist": entry})

        if route == "/api/playlist/add-song":
            from . import playlist as _pl
            try:
                entry = _pl.add_song(str(payload.get("site") or ""),
                                     str(payload.get("id") or ""),
                                     payload.get("song") or {})
            except (ValueError, OSError) as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True, "playlist": entry})

        if route == "/api/playlist/remove-song":
            from . import playlist as _pl
            try:
                entry = _pl.remove_song(str(payload.get("site") or ""),
                                        str(payload.get("id") or ""),
                                        str(payload.get("url") or ""))
            except (ValueError, OSError) as exc:
                raise ApiError(str(exc)) from None
            return self._json({"ok": True, "playlist": entry})

        if route == "/api/playlist/list":
            from . import playlist as _pl
            try:
                # 先把下载完的任务回填成本地路径，界面才能显示「已下载」
                _pl.backfill(lambda tid: ctx.tasks.get(tid))
                # 注意：这里**不再自动排队**。以前每次读歌单都会 advance，
                # 结果一打开歌单就开始下载，用户并没有点任何东西。
                # 现在只有显式调用 /api/playlist/advance 才会排。
            except Exception as exc:
                from .log import log_error
                log_error("playlist.backfill", exc)
            items = _pl.load_local()
            # 标出「记着本地路径但文件已经不存在」的条目。
            # **只在 deep 时做**：歌单列表页只需要标题和首数，没必要为几百首
            # 歌逐个 stat 文件；展开某个歌单时前端才会带 deep=1 过来。
            try:
                if not payload.get("deep"):
                    raise StopIteration      # 浅查询：不做文件存在性检查
                # 先把「同一个 key 已经下好」的路径建一张索引，
                # 这样别的歌单下过的歌在这个歌单里也不会显示下载按钮。
                from pathlib import Path as _P2
                _have = {}
                for pl in items:
                    for song in (pl.get("songs") or []):
                        loc = str(song.get("local") or "")
                        if loc and _P2(loc).exists():
                            k = _pl._song_key(song.get("url"))
                            if k and k not in _have:
                                _have[k] = loc
                # 除了"别的歌单记着路径"，还要认"音乐目录里已经有这个文件"——
                # 用户可能直接从音乐页下载，那样歌单条目里压根没有 task_id，
                # 永远不会被回填。这里按文件名兜底匹配。
                import re as _re
                _AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma", ".ape"}
                def _clean(n: str) -> str:
                    n = _re.sub(r"\s*(\[\d+\]|\(\d+\))$", "", str(n or "").strip())
                    return n.replace(" ", "").lower()
                _byname = {}
                try:
                    # 必须用 ctx.music_dir()：settings.music_dir 留空时它会算成
                    # <下载目录>/音乐。之前我拿空字符串拼 Path('')/'音乐'，
                    # 得到的是相对路径，永远指不到真实目录 —— 兜底等于没做。
                    _mdir = ctx.music_dir()
                    if _mdir.exists():
                        for _f in _mdir.rglob("*"):
                            if _f.is_file() and _f.suffix.lower() in _AUDIO:
                                base = _re.sub(r"\s*\[[^\]]*\]$", "", _f.stem)
                                _byname.setdefault(_clean(base), str(_f))
                except Exception:
                    _byname = {}

                # 只靠歌单里记着的路径不够：从音乐页直接下载的歌，歌单条目
                # 压根没有 local。这里再把"已完成任务"的产物按 URL 归一后
                # 塞进索引，这样无论从哪儿下的，两个歌单都能认到。
                try:
                    for _t in ctx.tasks.list():
                        st = str(_t.get("status") or "").lower()
                        if st != "done":
                            continue
                        src = str(_t.get("url") or _t.get("raw_input") or _t.get("input") or "")
                        k = _pl._song_key(src)
                        if not k:
                            continue
                        for _f in (_t.get("files") or []):
                            p2 = str(_f.get("path") or "")
                            if p2 and _P2(p2).exists() and p2.lower().endswith(
                                    (".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma", ".ape")):
                                _have.setdefault(k, p2)
                                break
                except Exception:
                    pass

                for pl in items:
                    for song in (pl.get("songs") or []):
                        if not song.get("local"):
                            k = _pl._song_key(song.get("url"))
                            if k in _have:
                                song["local"] = _have[k]      # 只填内存，不写盘
                                song.pop("missing", None)
                            else:
                                hit = _byname.get(_clean(song.get("name")))
                                if hit:
                                    song["local"] = hit
                                    song.pop("missing", None)
                # 深查询顺便统计每个歌单正在下载几首，界面据此显示「取消下载」
                _DONE = ("done", "error", "failed", "canceled", "cancelled", "stopped")
                for pl in items:
                    live = 0
                    for song in (pl.get("songs") or []):
                        tid = song.get("task_id")
                        if not tid or song.get("local"):
                            continue
                        try:
                            task = ctx.tasks.get(tid)
                        except Exception:
                            task = None
                        if task and str(getattr(task, "status", "")).lower() not in _DONE:
                            live += 1
                    pl["downloading"] = live
                from pathlib import Path as _Path
                for pl in items:
                    for song in (pl.get("songs") or []):
                        local = str(song.get("local") or "")
                        if local:
                            song["missing"] = not _Path(local).exists()
            except StopIteration:
                pass
            except Exception:
                pass
            return self._json({"ok": True, "playlists": items})

        if route == "/api/playlist/resolve":
            # 点播放时先问一句"这首歌是不是已经在别处下过了"
            # 找到就把路径写回这个条目，前端直接播，不用再下载
            from . import playlist as _pl
            site = str(payload.get("site") or "")
            pid = str(payload.get("id") or "")
            url = str(payload.get("url") or "")
            local = ""
            try:
                local = _pl.find_existing_local(url)
                if local:
                    _pl.set_song_local(site, pid, url, local)
            except Exception as exc:
                from .log import log_error
                log_error("playlist.resolve", exc)
            return self._json({"ok": True, "local": local})

        if route == "/api/playlist/download-song":
            # 只下这一首：
            #   1) 别的歌单里同 URL 且文件还在 -> 直接复用，不下载
            #   2) 否则只给这一首建任务，不动同歌单里其它歌
            from . import playlist as _pl
            site = str(payload.get("site") or "")
            pid = str(payload.get("id") or "")
            url = str(payload.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise ApiError("这首没有可下载的平台链接")
            reused = _pl.find_existing_local(url)
            if reused:
                _pl.set_song_local(site, pid, url, reused)
                return self._json({"ok": True, "reused": True, "local": reused})
            try:
                task = ctx.tasks.submit(url, engine="auto", qn=0, mode="auto")
            except Exception as exc:
                raise ApiError("提交下载失败：" + str(exc)[:120]) from None
            try:
                _pl.reset_song(site, pid, url)
                _pl.set_song_task(site, pid, url, task.id)
            except Exception:
                pass
            return self._json({"ok": True, "reused": False, "task": task.id})

        if route == "/api/playlist/advance":
            # 显式排队：只在用户点「下载」或「继续下载未完成的」时调用
            from . import playlist as _pl
            try:
                _pl.backfill(lambda tid: ctx.tasks.get(tid))
                n = _pl.advance(lambda tid: ctx.tasks.get(tid),
                                lambda url: ctx.tasks.submit(url, engine="auto", qn=0, mode="auto"),
                                5)
            except Exception as exc:
                raise ApiError("排队失败：" + str(exc)[:120]) from None
            return self._json({"ok": True, "queued": n})

        if route == "/api/playlist/retry-song":
            from . import playlist as _pl
            try:
                pl = _pl.reset_song(str(payload.get("site") or ""),
                                    str(payload.get("id") or ""),
                                    str(payload.get("url") or ""))
            except (OSError, ValueError) as exc:
                raise ApiError(str(exc)[:120]) from None
            return self._json({"ok": True, "playlist": pl})

        if route == "/api/playlist/cancel":
            # 取消这个歌单里所有还在跑的任务。取消后 advance 会自动接下一首。
            from . import playlist as _pl
            try:
                stopped = _pl.cancel_all(lambda tid: ctx.tasks.get(tid),
                                         lambda tid: ctx.tasks.cancel(tid))
            except Exception as exc:
                from .log import log_error
                log_error("playlist.cancel", exc)
                raise ApiError("取消失败：" + str(exc)[:120]) from None
            return self._json({"ok": True, "stopped": stopped})

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
                _record_bigjpg(results[-1], style=style, noise=noise, x2=x2)
            return self._json({"ok": True, "results": results})

        if route == "/api/bigjpg/history":
            return self._json({"ok": True, "items": _load_bigjpg()})

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
            return self._json({"ok": True,
                               "detail": _open_in_explorer(target, bool(payload.get("reveal")))})

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


# 用户在系统文件对话框里**亲手选过**的路径。这些文件允许经 /media 播放 ——
# 剪辑本地视频的前提就是能放它，而把它们全塞进下载目录不现实。
_PICKED: set = set()
_PICKED_LOCK = __import__("threading").Lock()


def _remember_picked(paths) -> None:
    with _PICKED_LOCK:
        for raw in paths or []:
            try:
                _PICKED.add(_normal_path(raw))
            except (OSError, ValueError):
                continue
        # 只留最近的 500 个，别无限增长
        while len(_PICKED) > 500:
            _PICKED.pop()


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
    # 用户自己选过的文件也放行
    with _PICKED_LOCK:
        if _normal_path(target) in _PICKED:
            return True
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


def _open_in_explorer(target: str, reveal: bool = False) -> str:
    """打开目录；reveal=True 时在资源管理器里选中这个文件。"""
    path = Path(target)
    try:
        if os.name == "nt":
            if reveal and path.is_file():
                # explorer 的 /select 会选中文件，比只打开父目录好用
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(path)])
                return f"已定位 {path.name}"
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


def _init_projects() -> None:
    """登记表放 data/ 下，并启动自动提交巡检。没有 git 也不能让应用起不来。"""
    try:
        projects_api.init(DATA_DIR)
    except Exception as exc:
        from .log import log_error
        log_error("projects", exc)


def serve(ctx: Context, host: str = "127.0.0.1", port: int = 8931, quiet: bool = True):
    remember_boot_dirs(ctx)      # 必须在处理任何请求之前记下目录原值
    _init_projects()
    ctx.tasks = TaskManager(ctx)
    Handler.ctx = ctx
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.quiet = quiet  # type: ignore[attr-defined]
    return httpd

