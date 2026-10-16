"""通道 8：第三方公开解析服务聚合（如实标注：取流接口实测已全部失效）。

本机实测 13 个公开解析服务，能返回视频直链的：**0 个**。
仍保留这条通道的原因有两点：
  1. 这些服务经常「死而复生」，通道会自动跳过失效项、命中可用项；
  2. 体检页面会把每个服务的实时存活情况列出来，方便判断值不值得依赖。
"""
from __future__ import annotations

from urllib.parse import quote

from ..models import Durl, PlayInfo, VideoInfo
from .base import Engine, EngineUnavailable
from .e1_web_wbi import VIEW, WebWbiEngine

PROVIDERS = [
    {"id": "injahow", "name": "Injahow bparse",
     "url": "https://api.injahow.cn/bparse/?bv={bvid}&p={p}&q={q}&format=mp4", "kind": "json"},
    {"id": "xingzhige", "name": "星之阁 API",
     "url": "https://api.xingzhige.com/API/bilibili/?url={bvid}", "kind": "json"},
    {"id": "vmy", "name": "52vmy 解析",
     "url": "https://api.52vmy.cn/api/video/bilibili?url={bvid}", "kind": "json"},
    {"id": "pearktrue", "name": "Pearktrue 解析",
     "url": "https://api.pearktrue.cn/api/bilibili/?url=https://www.bilibili.com/video/{bvid}",
     "kind": "json"},
    {"id": "xmflv", "name": "xmflv 解析",
     "url": "https://jx.xmflv.com/?url=https://www.bilibili.com/video/{bvid}", "kind": "text"},
    {"id": "tenapi", "name": "TenAPI 哔哩哔哩",
     "url": "https://tenapi.cn/v2/bilibilivideo?url=https://www.bilibili.com/video/{bvid}",
     "kind": "json"},
    {"id": "cenguigui", "name": "层桂桂元数据",
     "url": "https://api.cenguigui.cn/api/bilibili/bilibili.php?action=search&query={bvid}&page=1&limit=1",
     "kind": "json", "metadata_only": True},
]

_MEDIA_HINTS = (".mp4", ".m3u8", ".flv", ".m4s", "bilivideo.com", "akamaized", "upos-")
_SKIP_KEYS = {"cover", "pic", "face", "avatar", "image", "img", "poster", "head", "thumb"}


def _find_media_url(node, depth: int = 0) -> str:
    """在结构未知的 JSON 里递归挖第一个像视频直链的地址。"""
    if depth > 6:
        return ""
    if isinstance(node, str):
        low = node.lower()
        if not low.startswith("http"):
            return ""
        if any(low.endswith(ext) for ext in (".jpg", ".png", ".webp", ".gif", ".jpeg")):
            return ""
        if "cover" in low or "/bfs/face" in low:
            return ""
        if any(hint in low for hint in _MEDIA_HINTS):
            return node
        return ""
    if isinstance(node, dict):
        for key in ("url", "video", "src", "mp4", "play_url", "video_url", "data", "result", "vurl"):
            if key in node and key.lower() not in _SKIP_KEYS:
                found = _find_media_url(node[key], depth + 1)
                if found:
                    return found
        for key, value in node.items():
            if str(key).lower() in _SKIP_KEYS:
                continue
            found = _find_media_url(value, depth + 1)
            if found:
                return found
        return ""
    if isinstance(node, list):
        for item in node:
            found = _find_media_url(item, depth + 1)
            if found:
                return found
    return ""


class ThirdPartyJxEngine(Engine):
    id = "thirdparty_jx"
    name = "第三方解析服务聚合"
    desc = ("轮询 7 个公开解析接口挖直链。实测取流接口已全部失效。"
            "【隐私提醒】启用后会把你在解析的 B 站地址发送给这 7 个第三方服务"
            "（api.injahow.cn / api.xingzhige.com / api.52vmy.cn / api.pearktrue.cn / "
            "jx.xmflv.com / tenapi.cn / api.cenguigui.cn）。因为它们已经全部失效，"
            "默认是关闭的；确实想试可以在这里打开。")
    kind = "thirdparty"
    badge = "已失效"

    def available(self, ctx) -> tuple[bool, str]:
        return True, f"共 {len(PROVIDERS)} 个候选服务，实测取流接口当前均已失效"

    def fetch_info(self, ctx, ref) -> VideoInfo:
        self.require_video_ref(ref)
        data = ctx.net.api(f"{VIEW}?{self.ident(ref)}")
        info = WebWbiEngine._to_info(data, ref)
        info.engine = self.id
        return info

    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        self.require_video_ref(ref)
        info = self.fetch_info(ctx, ref)
        bvid = ref.bvid or info.bvid
        if not bvid:
            raise EngineUnavailable("第三方解析需要 BV 号")

        tried: list[str] = []
        for provider in PROVIDERS:
            if provider.get("metadata_only"):
                continue
            url = provider["url"].format(bvid=quote(bvid), p=page or 1,
                                         q=min(int(qn or 1080), 1080) or 1080)
            try:
                res = ctx.net.request(url, retries=1, timeout=9,
                                      referer="https://www.bilibili.com/",
                                      headers={"Accept": "application/json, text/plain, */*"})
            except Exception as exc:
                tried.append(f"{provider['name']}：请求失败")
                continue
            text = res.text()
            if res.status != 200 or not text.strip():
                tried.append(f"{provider['name']}：HTTP {res.status}")
                continue

            media = ""
            if provider["kind"] == "json":
                try:
                    media = _find_media_url(res.json())
                except ValueError:
                    media = _find_media_url(text)
            else:
                media = _find_media_url(text)
                if not media:
                    import re
                    hit = re.search(r"https?://[^\s\"'<>\\]+\.(?:mp4|m3u8)[^\s\"'<>\\]*", text)
                    media = hit.group(0) if hit else ""

            if not media:
                tried.append(f"{provider['name']}：响应里没有视频直链")
                continue

            size = ctx.net.content_length(media, referer="https://www.bilibili.com/")
            if size is None:
                tried.append(f"{provider['name']}：直链被防盗链拦截")
                continue

            play = PlayInfo(engine=self.id, engine_name=self.name, duration=info.duration)
            play.durl = [Durl(order=1, url=media, size=size, length=info.duration)]
            play.accept_quality = [int(qn or 1080)]
            play.accept_desc = ["第三方解析（画质由服务方决定）"]
            play.note = f"由「{provider['name']}」提供直链，单文件 MP4，无需合并"
            return play

        raise EngineUnavailable("所有第三方解析服务均不可用：" + "；".join(tried[:5]))

    def probe_all(self, ctx, bvid: str) -> list[dict]:
        """体检页面用：逐个报告第三方服务的存活情况。"""
        results = []
        for provider in PROVIDERS:
            url = provider["url"].format(bvid=quote(bvid), p=1, q=1080)
            entry = {"id": provider["id"], "name": provider["name"],
                     "ok": False, "detail": "", "metadata_only": bool(provider.get("metadata_only"))}
            try:
                res = ctx.net.request(url, retries=1, timeout=12)
                payload = None
                try:
                    payload = res.json()
                except ValueError:
                    payload = None
                if provider.get("metadata_only"):
                    entry["ok"] = bool(payload and payload.get("code") == 200)
                    entry["detail"] = "元数据接口存活（不提供播放地址）" if entry["ok"] \
                        else f"HTTP {res.status}"
                elif payload is not None and _find_media_url(payload):
                    entry["ok"] = True
                    entry["detail"] = "已取得视频直链"
                else:
                    body = res.text()[:80].replace("\n", " ")
                    entry["detail"] = f"HTTP {res.status} · {body}"
            except Exception as exc:
                entry["detail"] = str(exc)[:110]
            results.append(entry)
        return results
