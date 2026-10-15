"""通道 2：网页源码内嵌数据（__INITIAL_STATE__ + defaultWbiKey）。

桌面播放页的 window.__playinfo__ 早已废弃（页面上只剩一个恒为 undefined 的守卫），
但 __INITIAL_STATE__ 仍然完整包含 videoData，并且额外带了一份 defaultWbiKey。
用它直接算出 mixin_key 去签名 playurl，整条链路完全不需要请求 nav 接口。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlencode

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..wbi import get_mixin_key
from .base import Engine, EngineUnavailable

WBI_PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"
FNVAL_DASH = 4048

_UNDEFINED = re.compile(r"\bundefined\b")
_NAN = re.compile(r"\bNaN\b")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def extract_object(text: str, marker: str) -> dict:
    """从 marker 之后按花括号配对抠出一个 JSON 对象（正则会在大 JSON 上崩掉）。"""
    at = text.find(marker)
    if at < 0:
        return {}
    start = text.find("{", at)
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return loads_loose(text[start:index + 1])
    return {}


def loads_loose(blob: str) -> dict:
    """页面内嵌 JSON 带裸 undefined / NaN，标准 json 解析不了。"""
    cleaned = _UNDEFINED.sub("null", blob)
    cleaned = _NAN.sub("null", cleaned)
    cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except ValueError:
        return {}


class HtmlScrapeEngine(Engine):
    id = "html_scrape"
    name = "网页源码内嵌数据"
    desc = "抓取视频网页 HTML，从 __INITIAL_STATE__ 里取出元数据和站点内嵌的 defaultWbiKey，再用这份密钥自行签名取流。全程不请求 nav，也没有 playinfo 依赖。"
    kind = "web"
    badge = "零 nav"

    def available(self, ctx) -> tuple[bool, str]:
        return True, ""

    # ---------- 抓页面 ----------
    def _page_url(self, ref, page: int | None = None) -> str:
        url = f"https://www.bilibili.com/video/{ref.bvid or ('av' + str(ref.aid))}/"
        page = page or ref.p or 1
        if page > 1:
            url += f"?p={page}"
        return url

    def _html(self, ctx, ref, page=None) -> tuple[str, dict]:
        res = ctx.net.request(
            self._page_url(ref, page), retries=3,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
            referer="https://www.bilibili.com/",
        )
        html = res.text()
        if len(html) < 2000:
            raise EngineUnavailable("网页内容过短，可能触发了风控")
        state = extract_object(html, "window.__INITIAL_STATE__")
        if not state:
            raise EngineUnavailable("网页里没有解析出 window.__INITIAL_STATE__")
        return html, state

    # ---------- 元数据 ----------
    def fetch_info(self, ctx, ref) -> VideoInfo:
        self.require_video_ref(ref)
        html, state = self._html(ctx, ref)
        video = state.get("videoData") or {}
        if not video:
            raise EngineUnavailable("__INITIAL_STATE__ 里没有 videoData")

        pages = [
            Page(cid=int(item.get("cid") or 0), page=int(item.get("page") or index + 1),
                 part=item.get("part") or "", duration=int(item.get("duration") or 0))
            for index, item in enumerate(video.get("pages") or [])
        ]
        owner = video.get("owner") or {}
        info = VideoInfo(
            bvid=video.get("bvid") or ref.bvid,
            aid=int(video.get("aid") or ref.aid or 0),
            title=video.get("title") or "",
            cover=video.get("pic") or "",
            desc=video.get("desc") or "",
            duration=int(video.get("duration") or 0),
            pubdate=int(video.get("pubdate") or 0),
            tname=video.get("tname") or "",
            owner=Owner(mid=int(owner.get("mid") or 0), name=owner.get("name") or "",
                        face=owner.get("face") or ""),
            pages=pages,
            stat=video.get("stat") or {},
            engine=self.id,
        )
        key = state.get("defaultWbiKey") or {}
        info.extra["has_inline_wbi_key"] = bool(key.get("wbiImgKey"))
        info.extra["html_bytes"] = len(html)
        info.extra["playinfo_deprecated"] = "window.__playinfo__" not in html or \
            html.count('"dash"') == 0
        return info

    # ---------- 播放地址 ----------
    def _inline_mixin_key(self, state: dict) -> str:
        key = state.get("defaultWbiKey") or {}
        img = key.get("wbiImgKey") or ""
        sub = key.get("wbiSubKey") or ""
        if not img or not sub:
            return ""
        return get_mixin_key(img + sub)

    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        self.require_video_ref(ref)
        html, state = self._html(ctx, ref, page)
        video = state.get("videoData") or {}
        cid = 0
        for item in video.get("pages") or []:
            if int(item.get("page") or 0) == (page or 1):
                cid = int(item.get("cid") or 0)
                break
        if not cid:
            cid = int(video.get("cid") or 0)
        if not cid:
            raise EngineUnavailable("网页内嵌数据里没有 cid")

        mixin = self._inline_mixin_key(state)
        params = {
            "bvid": video.get("bvid") or ref.bvid,
            "cid": cid, "qn": qn or 127, "fnval": FNVAL_DASH, "fnver": 0, "fourk": 1,
        }
        note = "从网页 __INITIAL_STATE__ 取密钥并签名"
        if mixin:
            # 用的是页面自带的 img_key/sub_key 混洗出来的密钥，全程不碰 nav。
            # 这里不能复用 enc_wbi()——它是给「img_key + sub_key」用的，
            # 传空串会在重排表上越界。
            signed = dict(params)
            signed["wts"] = int(time.time())
            query = urlencode(sorted(signed.items()))
            signed["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
            url = f"{WBI_PLAYURL}?{urlencode(signed)}"
        else:
            note = "网页未提供内嵌密钥，退回免签名接口"
            url = ("https://api.bilibili.com/x/player/playurl?"
                   + urlencode({**params, "fnval": 1}))

        try:
            payload = ctx.net.api(url, referer=self._page_url(ref, page))
        except Exception as exc:
            # 内嵌密钥偶尔会撞上短时风控，退回免签名接口，别浪费已经抓到的页面
            note = f"{note}（签名请求被拒，已退回免签名接口：{str(exc)[:40]}）"
            fallback = ("https://api.bilibili.com/x/player/playurl?"
                        + urlencode({**params, "fnval": FNVAL_DASH}))
            payload = ctx.net.api(fallback, referer=self._page_url(ref, page))
        play = self.make_play(payload, qn, note=note)
        if not play.videos and not play.durl:
            raise EngineUnavailable("内嵌数据通道没有取到可用地址")
        return play
