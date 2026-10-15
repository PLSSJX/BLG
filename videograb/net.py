"""纯标准库 HTTP 客户端：Cookie 注入、代理、重试、Range 请求。"""
from __future__ import annotations

import gzip
import json
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_UA

# 这些是 B 站的接口域名：它们对突发请求很敏感，命中会直接回 412（风控）。
# 下载走的 bilivideo.com 之类 CDN 不受影响，所以只对接口域名限速。
THROTTLED_HOSTS = (
    "api.bilibili.com", "app.bilibili.com", "api.snm0516.aisee.tv",
    "www.bilibili.com", "api.cenguigui.cn",
)
MIN_API_GAP = 0.15          # 同一进程内两次接口请求的最小间隔（秒）
RISK_CONTROL_CODES = (412, 429)
RISK_CONTROL_HINT = "触发了 B 站风控（HTTP 412），冷却几秒后自动重试即可"


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes = b"", reason: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} {reason} :: {url[:120]}")


@dataclass
class HttpResult:
    status: int
    headers: dict
    body: bytes
    url: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8", "replace"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str, default: str = "") -> str:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default


@dataclass
class CookieJar:
    """只保存我们显式配置的字段，避免到处散落字符串拼接。"""

    values: dict = field(default_factory=dict)

    def set(self, name: str, value: str) -> None:
        if value:
            self.values[name] = value

    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.values.items() if v)

    def __bool__(self) -> bool:
        return bool(self.header())


def _decompress(raw: bytes, encoding: str) -> bytes:
    """B 站的 CDN 有时会无视 Accept-Encoding: identity 直接回 gzip，
    所以除了看响应头，还要按 gzip 魔术字节 0x1f 0x8b 兜底判断。"""
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            return raw
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc or "x-gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


class HttpClient:
    """带重试与限速礼貌的同步客户端。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self._opener = None
        self._buvid_fetched = False
        self._throttle_lock = threading.Lock()
        self._last_api_at = 0.0
        self._risk_until = 0.0

    def _is_api(self, url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in THROTTLED_HOSTS)

    def _throttle(self, url: str) -> None:
        """给接口域名之间留出最小间隔，避免多线程抓取时自己把自己打成 412。"""
        if not self._is_api(url):
            return
        while True:
            with self._throttle_lock:
                now = time.time()
                # 一旦有请求吃了风控，全局一起冷却，而不是各自硬撞
                wait = max(self._risk_until - now, MIN_API_GAP - (now - self._last_api_at), 0.0)
                if wait <= 0:
                    self._last_api_at = now
                    return
            time.sleep(min(wait, 1.0))

    def _back_off(self, url: str, seconds: float) -> None:
        """记录一次风控命中，让后续所有接口请求都先等一下。"""
        if not self._is_api(url):
            return
        with self._throttle_lock:
            self._risk_until = max(self._risk_until, time.time() + seconds)

    # ---------- opener ----------
    def _build_opener(self):
        settings = self.ctx.settings
        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        proxy = (settings.proxy or "").strip()
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = []
        return opener

    @property
    def opener(self):
        if self._opener is None:
            self._opener = self._build_opener()
        return self._opener

    # ---------- cookies ----------
    def cookie(self) -> CookieJar:
        s = self.ctx.settings
        jar = CookieJar()
        jar.set("SESSDATA", s.sessdata.strip())
        jar.set("bili_jct", s.bili_jct.strip())
        jar.set("DedeUserID", str(s.dede_user_id).strip())
        jar.set("buvid3", (s.buvid3 or "").strip())
        return jar

    def ensure_buvid(self) -> str:
        """没有 buvid3 时向官方指纹接口要一个，能显著降低风控概率。"""
        if self._buvid_fetched:
            return self.ctx.settings.buvid3
        self._buvid_fetched = True
        if self.ctx.settings.buvid3:
            return self.ctx.settings.buvid3
        try:
            data = self.get_json("https://api.bilibili.com/x/frontend/finger/spi")
            buvid = (data.get("data") or {}).get("b_3") or ""
            if buvid:
                self.ctx.settings.buvid3 = buvid
        except Exception:
            pass
        return self.ctx.settings.buvid3

    def is_logged_in(self) -> bool:
        return bool(self.ctx.settings.sessdata.strip())

    # ---------- request ----------
    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        data: bytes | None = None,
        timeout: int | None = None,
        retries: int = 3,
        referer: str | None = None,
        byte_range: tuple[int, int] | None = None,
        allow_redirects: bool = True,
    ) -> HttpResult:
        settings = self.ctx.settings
        hdrs = {
            "User-Agent": settings.user_agent or DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer if referer is not None else settings.referer,
            "Connection": "keep-alive",
        }
        cookie = self.cookie().header()
        if cookie:
            hdrs["Cookie"] = cookie
        if byte_range is not None:
            hdrs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        hdrs.update(headers or {})

        last_error: Exception | None = None
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                self._throttle(url)
                req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                resp = self.opener.open(req, timeout=timeout or settings.request_timeout)
                raw = resp.read()
                body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                return HttpResult(resp.status, dict(resp.headers), body, resp.geturl())
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:
                    pass
                # 4xx 里只有 412（风控）和 429（限流）值得重试。
                # 实测 412 是短时冷却：隔两秒重试同一个签名请求就会恢复，
                # 所以这里退避给得比普通错误更宽。
                if exc.code in RISK_CONTROL_CODES:
                    # 冷却要短：抓取链路上一条通道被风控时，
                    # 应该尽快让位给下一条通道，而不是把整个流程卡住十几秒。
                    self._back_off(url, 1.2 + attempt * 1.2)
                    if attempt < attempts - 1:
                        time.sleep(1.2 * (attempt + 1) + random.random() * 0.5)
                        last_error = HttpError(exc.code, url, body, RISK_CONTROL_HINT)
                        continue
                    raise HttpError(exc.code, url, body, RISK_CONTROL_HINT) from None
                if 500 <= exc.code < 600 and attempt < attempts - 1:
                    time.sleep(0.6 * (attempt + 1))
                    last_error = HttpError(exc.code, url, body, exc.reason)
                    continue
                raise HttpError(exc.code, url, body, exc.reason) from None
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1) + random.random() * 0.3)
                    continue
                raise
        raise last_error if last_error else RuntimeError("request failed")

    # ---------- helpers ----------
    def get(self, url: str, **kw) -> HttpResult:
        return self.request(url, **kw)

    def get_json(self, url: str, **kw) -> dict:
        return self.request(url, **kw).json()

    def get_text(self, url: str, **kw) -> str:
        return self.request(url, **kw).text()

    def api(self, url: str, **kw) -> dict:
        """调 B 站 JSON 接口，code != 0 时抛出带 message 的异常。"""
        data = self.get_json(url, **kw)
        code = data.get("code", 0)
        if code != 0:
            raise BiliApiError(code, data.get("message") or data.get("msg") or "接口返回错误", url)
        return data.get("data") or data.get("result") or {}

    def stream(self, url: str, *, referer: str | None = None, timeout: int = 60,
               chunk_size: int = 1 << 20, byte_range: tuple[int, int] | None = None):
        """流式读取响应体，避免把大文件整个塞进内存。产出 bytes 块。"""
        settings = self.ctx.settings
        headers = {
            "User-Agent": settings.user_agent or DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer if referer is not None else settings.referer,
        }
        cookie = self.cookie().header()
        if cookie:
            headers["Cookie"] = cookie
        if byte_range is not None:
            headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        resp = self.opener.open(req, timeout=timeout)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        raw = resp
        if "gzip" in encoding or "deflate" in encoding:
            # 需要解压时只能整体读入，这里用增量解压器尽量省内存
            decompressor = (zlib.decompressobj(16 + zlib.MAX_WBITS)
                            if "gzip" in encoding else zlib.decompressobj())
            while True:
                block = raw.read(chunk_size)
                if not block:
                    break
                out = decompressor.decompress(block)
                if out:
                    yield out
            tail = decompressor.flush()
            if tail:
                yield tail
            return
        while True:
            block = raw.read(chunk_size)
            if not block:
                break
            yield block

    def content_length(self, url: str, referer: str | None = None) -> int | None:
        """用 Range: bytes=0-0 探长度，比 HEAD 兼容性好。"""
        try:
            res = self.request(url, byte_range=(0, 0), retries=2, referer=referer)
        except Exception:
            return None
        cr = res.header("Content-Range")
        if cr and "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        if res.status == 200 and res.header("Content-Length"):
            try:
                return int(res.header("Content-Length"))
            except ValueError:
                return None
        return None

    def supports_range(self, url: str, referer: str | None = None) -> bool:
        try:
            res = self.request(url, byte_range=(0, 1023), retries=2, referer=referer)
        except Exception:
            return False
        return res.status == 206


class BiliApiError(Exception):
    def __init__(self, code: int, message: str, url: str = ""):
        self.code = code
        self.message = message
        self.url = url
        super().__init__(f"[{code}] {message}")


def build_query(params: dict) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
