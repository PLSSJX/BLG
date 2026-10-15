"""通道 9：云视听小电视 TV 端接口（如实标注：实测已失效）。

本机实测（16 组参数组合，含完全正确的 appkey+ts+sign）：
/x/tv/playurl 在 api.snm0516.aisee.tv 与 api.bilibili.com 上**一律**返回
HTTP 200 + code=-400「请求错误」。对照组同 host 的 /x/v2/rank 用同一套签名
可以正常返回，POST 返回 405、/x/tv/playurl2 返回 404 —— 说明路由还在，
但被网关无条件拒绝，不是参数或签名的问题。

这里把通道完整保留：一旦 B 站重新放开 TV 端，它无需改动即可恢复工作。
"""
from __future__ import annotations

from urllib.parse import urlencode

from ..appsign import APP_EXTRA, APP_KEYS, sign_params
from ..models import PlayInfo, VideoInfo
from .base import Engine, EngineUnavailable
from .e1_web_wbi import FNVAL_DASH, VIEW, WebWbiEngine

TV_HOSTS = [
    "https://api.snm0516.aisee.tv/x/tv/playurl",
    "https://api.bilibili.com/x/tv/playurl",
]


class TvApiEngine(Engine):
    id = "tv_api"
    name = "云视听 TV 端接口"
    desc = "用电视端 appkey 签名请求 TV 播放接口，本应下发已封装好音轨的单文件 MP4。当前 B 站网关对该路由无条件返回 code=-400，实测已失效，通道保留待其恢复。"
    kind = "tv"
    badge = "已失效"

    def available(self, ctx) -> tuple[bool, str]:
        return True, "网关当前对该路由返回 -400，实测不可用"

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

        appkey, appsec = APP_KEYS["tv"]
        errors = []
        for host in TV_HOSTS:
            for fnval, note in ((FNVAL_DASH, "TV 端 DASH"), (1, "TV 端单文件 MP4")):
                params = {
                    "cid": cid, "qn": qn or 127, "fnval": fnval, "fnver": 0, "fourk": 1,
                    **APP_EXTRA["tv"],
                }
                if ref.bvid or info.bvid:
                    params["bvid"] = ref.bvid or info.bvid
                else:
                    params["avid"] = ref.aid
                signed = sign_params(params, appkey, appsec)
                try:
                    payload = ctx.net.api(f"{host}?{urlencode(signed)}",
                                          referer="https://www.bilibili.com/")
                except Exception as exc:
                    errors.append(f"{host.split('/')[2]}：{exc}")
                    continue
                play = self.make_play(payload, qn, note=note)
                if play.videos or play.durl:
                    return play
        raise EngineUnavailable(
            "TV 端路由被网关拒绝（实测 16 组参数组合全部返回 code=-400）："
            + "；".join(errors[:2])
        )
