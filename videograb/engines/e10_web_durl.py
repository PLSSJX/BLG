"""通道 4：单文件直取通道（fnval=1，durl）。

实测结论：未登录时 DASH 最高只下发 480P，而 fnval=1 的 durl 反而能给到
720P 的、音视频已封装好的 MP4。想要免登录拿到最好的画质，这条通道是关键。
"""
from __future__ import annotations

from urllib.parse import urlencode

from ..models import PlayInfo, VideoInfo
from .base import Engine, EngineUnavailable
from .e1_web_wbi import VIEW, WebWbiEngine

PLAYURL = "https://api.bilibili.com/x/player/playurl"


class WebDurlEngine(Engine):
    id = "web_durl"
    name = "单文件直取（fnval=1）"
    desc = "专门请求 durl 单文件 MP4。B 站在未登录状态下通过这条路径会下发到 720P（比 DASH 的 480P 更高），且音视频已封装，下完即可播放。"
    kind = "api"
    badge = "免合并 720P"

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

        # qn 留空时直接要最高，服务端会给它愿意给的最高档
        params = {
            "bvid": ref.bvid or info.bvid,
            "cid": cid,
            "qn": qn or 127,
            "fnval": 1,
            "fnver": 0,
            "fourk": 1,
        }
        payload = ctx.net.api(
            f"{PLAYURL}?{urlencode(params)}",
            referer=f"https://www.bilibili.com/video/{ref.bvid or ''}",
        )
        play = self.make_play(payload, qn, note="durl 单文件 MP4（含音轨，免合并）")
        if not play.durl:
            raise EngineUnavailable("该视频没有 durl 单文件档位")
        # durl 只有一个 quality 字段，把它翻成可读标签
        declared = int(payload.get("quality") or 0)
        if declared:
            play.accept_quality = [declared]
            from ..quality import QUALITY_TABLE, short as q_short
            play.accept_desc = [
                QUALITY_TABLE.get(declared, ("", q_short(declared), 0))[0]
            ]
        return play
