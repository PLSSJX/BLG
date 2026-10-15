"""通道 1：Web 端 WBI 签名接口（api.bilibili.com/x/player/wbi/playurl）。"""
from __future__ import annotations

import time

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..wbi import enc_wbi, keys_from_urls
from .base import Engine, EngineUnavailable

PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"
VIEW = "https://api.bilibili.com/x/web-interface/view"
NAV = "https://api.bilibili.com/x/web-interface/nav"

# fnval 位掩码：4048 = DASH + HDR + 4K + 杜比 + 8K + AV1
FNVAL_DASH = 4048
FNVAL_MP4 = 1


class WebWbiEngine(Engine):
    id = "web_wbi"
    name = "Web WBI 签名接口"
    desc = "官方网页端主力接口，请求参数经 WBI（mixin key）签名。返回 DASH 音视频分离流，画质上限取决于登录态。"
    kind = "api"
    badge = "推荐"

    _keys: tuple[str, str] | None = None
    _keys_at: float = 0.0

    def available(self, ctx) -> tuple[bool, str]:
        return True, ""

    # ---------- WBI 密钥 ----------
    def _wbi_keys(self, ctx) -> tuple[str, str]:
        cls = WebWbiEngine
        if cls._keys and time.time() - cls._keys_at < 1800:
            return cls._keys
        # 注意：nav 在未登录时会返回 code=-101「账号未登录」，但 wbi_img
        # 依然照常下发。所以这里必须用 get_json 而不是 api()——后者会在
        # code != 0 时抛异常，白白丢掉本来可用的密钥。
        payload = ctx.net.get_json(NAV, referer="https://www.bilibili.com/")
        data = payload.get("data") or {}
        img = (data.get("wbi_img") or {})
        img_url = img.get("img_url") or ""
        sub_url = img.get("sub_url") or ""
        if not img_url or not sub_url:
            raise EngineUnavailable("未能从 nav 接口取得 WBI 密钥")
        cls._keys = keys_from_urls(img_url, sub_url)
        cls._keys_at = time.time()
        return cls._keys

    def _signed_url(self, ctx, params: dict) -> str:
        img_key, sub_key = self._wbi_keys(ctx)
        signed = enc_wbi(params, img_key, sub_key)
        from urllib.parse import urlencode
        return f"{PLAYURL}?{urlencode(signed)}"

    # ---------- 元数据 ----------
    def fetch_info(self, ctx, ref) -> VideoInfo:
        self.require_video_ref(ref)
        data = ctx.net.api(f"{VIEW}?{self.ident(ref)}")
        return self._to_info(data, ref)

    @staticmethod
    def _to_info(data: dict, ref) -> VideoInfo:
        pages = [
            Page(cid=int(p.get("cid") or 0), page=int(p.get("page") or i + 1),
                 part=p.get("part") or "", duration=int(p.get("duration") or 0))
            for i, p in enumerate(data.get("pages") or [])
        ]
        owner = data.get("owner") or {}
        return VideoInfo(
            bvid=data.get("bvid") or ref.bvid,
            aid=int(data.get("aid") or ref.aid or 0),
            title=data.get("title") or "",
            cover=data.get("pic") or "",
            desc=data.get("desc") or "",
            duration=int(data.get("duration") or 0),
            pubdate=int(data.get("pubdate") or 0),
            tname=data.get("tname") or "",
            owner=Owner(mid=int(owner.get("mid") or 0), name=owner.get("name") or "",
                        face=owner.get("face") or ""),
            pages=pages,
            stat=data.get("stat") or {},
            engine=WebWbiEngine.id,
        )

    # ---------- 播放地址 ----------
    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        self.require_video_ref(ref)
        info = self._cached_info(ctx, ref)
        cid = self.page_cid(info, ref, page)
        if not cid:
            raise EngineUnavailable("未取得 cid")

        params = {
            "bvid": ref.bvid or info.bvid,
            "cid": cid,
            "qn": qn or 127,
            "fnval": FNVAL_DASH,
            "fnver": 0,
            "fourk": 1,
        }
        url = self._signed_url(ctx, params)
        payload = ctx.net.api(url, referer=f"https://www.bilibili.com/video/{ref.bvid or ''}")
        play = self.make_play(payload, qn, note="WBI 签名校验通过")
        if not play.videos and not play.durl:
            raise EngineUnavailable("接口返回了空的播放地址")
        return play

    def _cached_info(self, ctx, ref) -> VideoInfo:
        cached = getattr(ctx, "_info_cache", None)
        key = ref.key()
        if cached and key in cached:
            return cached[key]
        info = self.fetch_info(ctx, ref)
        if not hasattr(ctx, "_info_cache"):
            ctx._info_cache = {}
        ctx._info_cache[key] = info
        return info

    # 供 /api/probe 使用的轻量自检
    def probe(self, ctx, ref) -> dict:
        self._wbi_keys(ctx)
        return {"ok": True, "detail": "WBI 密钥获取成功"}
