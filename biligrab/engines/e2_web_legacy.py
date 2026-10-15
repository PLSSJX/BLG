"""通道 6：Web 经典未签名接口（api.bilibili.com/x/player/playurl）。

实测：这个接口完全不校验签名，返回结果与 WBI 版**逐字段一致**。
因此它最大的价值是当 WBI 签名因为密钥轮换而失效时的兜底通道。
"""
from __future__ import annotations

from urllib.parse import urlencode

from ..models import PlayInfo, VideoInfo
from .base import Engine, EngineUnavailable
from .e1_web_wbi import FNVAL_DASH, VIEW, WebWbiEngine

PLAYURL = "https://api.bilibili.com/x/player/playurl"


class WebLegacyEngine(Engine):
    id = "web_legacy"
    name = "Web 经典接口（免签名）"
    desc = "不带任何签名参数，只靠 Referer 与 UA 直接调 playurl 取 DASH 流。实测与 WBI 版返回一致，用来在签名密钥轮换失效时兜底。"
    kind = "api"
    badge = "兜底"

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
            "bvid": ref.bvid or info.bvid, "cid": cid,
            "qn": qn or 127, "fnval": FNVAL_DASH, "fnver": 0, "fourk": 1,
        }
        payload = ctx.net.api(
            f"{PLAYURL}?{urlencode(params)}",
            referer=f"https://www.bilibili.com/video/{ref.bvid or ''}",
        )
        play = self.make_play(payload, qn, note="未签名接口，返回与 WBI 版一致")
        if not play.videos and not play.durl:
            raise EngineUnavailable("接口返回了空的播放地址")
        return play
