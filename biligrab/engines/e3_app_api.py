"""通道 3：移动端 APP 接口（appkey + appsec 签名）。"""
from __future__ import annotations

from urllib.parse import urlencode

from ..appsign import APP_EXTRA, APP_KEYS, sign_params
from ..models import VideoInfo
from .base import Engine, EngineUnavailable
from .e1_web_wbi import FNVAL_DASH, FNVAL_MP4, VIEW, WebWbiEngine

APP_VIEW = "https://app.bilibili.com/x/v2/view"
APP_PLAYURL = "https://api.bilibili.com/x/player/playurl"


class AppApiEngine(Engine):
    id = "app_api"
    name = "移动端 APP 接口"
    desc = "伪装成哔哩哔哩 Android 客户端，用固定 appkey/appsec 对全部参数做 MD5 签名。元数据走 app.bilibili.com，播放地址走签名后的 playurl。"
    kind = "app"
    badge = "APP 签名"

    def available(self, ctx) -> tuple[bool, str]:
        return True, ""

    # ---------- 元数据（真正的 APP 端点，失败再退回 Web）----------
    def fetch_info(self, ctx, ref) -> VideoInfo:
        self.require_video_ref(ref)
        appkey, appsec = APP_KEYS["android"]
        params = {"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid}
        params.update(APP_EXTRA["android"])
        signed = sign_params(params, appkey, appsec)
        try:
            data = ctx.net.api(f"{APP_VIEW}?{urlencode(signed)}",
                               referer="https://app.bilibili.com/")
            if data.get("bvid") or data.get("title"):
                return WebWbiEngine._to_info(data, ref)
        except Exception:
            pass
        # 退回网页端 view 接口，保证元数据总能拿到
        data = ctx.net.api(f"{VIEW}?{self.ident(ref)}")
        info = WebWbiEngine._to_info(data, ref)
        info.extra["fallback"] = "app.bilibili.com 不可达，元数据改由 Web view 接口提供"
        return info

    # ---------- 播放地址 ----------
    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        self.require_video_ref(ref)
        info = self.fetch_info(ctx, ref)
        cid = self.page_cid(info, ref, page)
        if not cid:
            raise EngineUnavailable("未取得 cid")

        appkey, appsec = APP_KEYS["android"]
        errors = []
        for fnval, note in ((FNVAL_DASH, "APP 签名 · DASH 音视频分离"),
                            (FNVAL_MP4, "APP 签名 · 单文件 MP4（durl）")):
            params = {
                "bvid": ref.bvid or info.bvid,
                "cid": cid,
                "qn": qn or 127,
                "fnval": fnval,
                "fnver": 0,
                "fourk": 1,
                **APP_EXTRA["android"],
            }
            signed = sign_params(params, appkey, appsec)
            try:
                payload = ctx.net.api(f"{APP_PLAYURL}?{urlencode(signed)}",
                                      referer="https://www.bilibili.com/")
            except Exception as exc:
                errors.append(str(exc))
                continue
            play = self.make_play(payload, qn, note=note)
            if play.videos or play.durl:
                return play
            errors.append(f"fnval={fnval} 返回为空")
        raise EngineUnavailable("；".join(errors) or "APP 接口无有效返回")
