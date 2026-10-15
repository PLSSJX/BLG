"""内置 HTTP 服务：静态界面 + JSON API。只用标准库。"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
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
        "app": "BiliGrab",
        "app_cn": "哔哩采集器",
        "engines": describe(ctx),
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
    # 配置里留空表示"用默认位置"，界面要显示解析后的真实路径
    data["download_dir"] = str(ctx.download_dir())
    # 不要把 SESSDATA 原样发回浏览器，只回显"是否已配置"
    data["sessdata"] = ""
    data["has_sessdata"] = bool(ctx.settings.sessdata.strip())
    data["has_bili_jct"] = bool(ctx.settings.bili_jct.strip())
    data["bili_jct"] = ""
    return data


def run_probe(ctx: Context, raw_input: str, qn: int = 0) -> dict:
    """把同一个视频丢给全部通道，报告每条通道的真实结果。"""
    try:
        ref = normalize(ctx, parse_ref(raw_input))
    except ValueError as exc:
        raise ApiError(str(exc)) from None

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
    server_version = "BiliGrab"
    protocol_version = "HTTP/1.1"
    ctx: Context = None  # type: ignore[assignment]

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):
        if self.server.quiet:  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status=status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("请求体过大", 413)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError("请求体不是合法 JSON") from None

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
        return self.headers.get("X-BiliGrab-Token") == expected

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path).path
        try:
            if not self._check_origin():
                return self._error("拒绝跨站请求", 403)
            # 自定义请求头无法用 no-cors 伪造，等价于一道 CSRF 防线
            if not self._check_token():
                return self._error("令牌无效或已过期，请刷新页面", 403)
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
            # 单实例检测用：另一个进程靠它判断"这个端口上是不是已经有一个 BiliGrab"
            return self._json({"ok": True, "app": "BiliGrab", "version": __version__})
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
            ref = normalize(ctx, parse_ref(raw))
            engine_id = payload.get("engine") or "auto"
            qn = int(payload.get("qn") or 0)

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
            ref = normalize(ctx, parse_ref(raw))
            if payload.get("p"):
                ref.p = int(payload["p"])
            qn = int(payload.get("qn") or 0)
            engine_id = payload.get("engine") or "auto"
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
                    play = engine.fetch_play(ctx, ref, ref.p, qn)
                except Exception as exc:
                    errors.append(f"{engine.name}：{exc}")
                    continue
                return self._json({
                    "ok": True, "play": play.to_dict(),
                    "engine": engine.id, "engine_name": engine.name,
                    "errors": errors,
                })
            raise ApiError("所有通道都没能取到播放地址：\n" + "\n".join(errors[:5]))

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
            mode = payload.get("mode") or "auto"
            task = ctx.tasks.submit(
                raw, engine=payload.get("engine") or "auto",
                qn=int(payload.get("qn") or 0), mode=mode,
            )
            return self._json({"ok": True, "task": task.to_dict()})

        if route == "/api/tasks/cancel":
            ok = ctx.tasks.cancel(payload.get("id") or "")
            return self._json({"ok": ok})

        if route == "/api/tasks/retry":
            task = ctx.tasks.retry(payload.get("id") or "")
            if not task:
                raise ApiError("任务不存在", 404)
            return self._json({"ok": True, "task": task.to_dict()})

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
    ALLOWED_IMAGE_HOSTS = ("hdslb.com", "bilibili.com", "bilivideo.com", "biliimg.com")

    def _serve_cover(self) -> None:
        url = self._query().get("url") or ""
        if not url.startswith("http"):
            return self._error("缺少合法的图片地址", 400)
        host = (urlparse(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.ALLOWED_IMAGE_HOSTS):
            return self._error("只允许代理 B 站的图片地址", 403)
        try:
            res = self.ctx.net.request(url, retries=2, referer="https://www.bilibili.com/")
        except Exception as exc:
            return self._error(f"取图失败：{exc}", 502)
        content_type = res.header("Content-Type") or "image/jpeg"
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        self._send(200, res.body, content_type,
                   {"Cache-Control": "public, max-age=86400"})

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
            if match:
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
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


def _inside_allowed(target: Path, ctx: Context) -> bool:
    """判断路径是否落在允许对外播放的目录内。"""
    allowed = [ctx.download_dir(), DATA_DIR]
    for root in allowed:
        try:
            target.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


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

