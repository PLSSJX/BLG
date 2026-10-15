"""通道 5：HTML5 / 移动端网页播放接口（platform=html5）。

这条链路是给不支持 DASH 的移动端网页用的，服务端会把音视频封成一个 MP4 直链下发，
参数组合与桌面接口完全不同，因此单独算一条通道。
"""
from __future__ import annotations

from urllib.parse import urlencode

from ..models import PlayInfo, VideoInfo
from .base import Engine, EngineUnavailable
from .e1_web_wbi import VIEW, WebWbiEngine

PLAYURL = "https://api.bilibili.com/x/player/playurl"


class Html5DurlEngine(Engine):
    id = "html5_durl"
    name = "HTML5 移动端接口"
    desc = "以 platform=html5 & high_quality=1 请求播放地址。这条链路面向不支持 DASH 的移动端浏览器，服务端直接回一个封装好音轨的 MP4，免去合并。"
    kind = "api"
    badge = "移动端"

    def available(self, ctx) -> tuple[bool, str]:
        return True, ""

    def fetch_info(self, ctx, ref) -> VideoInfo:
        self.require_video_ref(ref)
        data = ctx.net.api(f"{VIEW}?{self.ident(ref)}")
        return WebWbiEngine._to_info(data, ref)

    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        self.require_video_ref(ref)
        info = self.fetch_info(ctx, ref)
        cid = self.page_cid(info, ref, page)
        if not cid:
            raise EngineUnavailable("未取得 cid")

        params = {
            "bvid": ref.bvid or info.bvid,
            "cid": cid,
            "qn": qn or 127,
            "fnval": 1,
            "fnver": 0,
            "fourk": 1,
            "platform": "html5",
            "high_quality": 1,
        }
        payload = ctx.net.api(
            f"{PLAYURL}?{urlencode(params)}",
            referer=f"https://www.bilibili.com/video/{ref.bvid or ''}",
            headers={"User-Agent": _MOBILE_UA},
        )
        play = self.make_play(payload, qn, note="HTML5 移动端参数组合，单文件下发")
        if not play.durl and not play.videos:
            raise EngineUnavailable("HTML5 接口没有返回可播放地址")
        return play


_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
