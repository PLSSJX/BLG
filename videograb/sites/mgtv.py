"""芒果TV适配器。

实测可用的取流链路（免登录，2026 年仍在用）：
  1. pcweb 播放接口 player/video 拿元数据，顺带拿到 atc.pm2；
     这个接口要一个 tk2 参数，内容就是 "did=xxx|pno=1030|ver=0.3.0301|clit=时间戳"
     做一次 base64 再整体倒序，属于公开的客户端参数，不涉及签名破解。
  2. 用 pm2 换 player/getSource，接口按当前身份下发各档清晰度的相对地址；
     url 为空的档位就是当前身份没有权限的（比如未登录时的 1080P）。
  3. 相对地址拼上 stream_domain 再请求一次，返回 JSON 里的 info 才是真正的
     HLS(m3u8) 地址，带时效。

分集列表走 episode/list，一次能拿全，标题、时长、VIP 标记都有。
"""
from __future__ import annotations

import base64
import re
import time
import urllib.parse
import uuid

from ..models import Owner, Page, PlayInfo, Stream, VideoInfo
from ..quality import rank_from_resolution
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info

_PLAYER_API = "https://pcweb.api.mgtv.com/player/video"
_EPISODE_API = "https://pcweb.api.mgtv.com/episode/list"
_SOURCE_API = "https://tinker.glb.mgtv.com/player/getSource"
_REFERER = "https://www.mgtv.com/"

_COLLECTION_RE = re.compile(r"mgtv\.com/(?:b|h)/(\d+)/(\d+)", re.I)
_SHORT_RE = re.compile(r"mgtv\.com/s/(\d+)", re.I)
_TIME_RE = re.compile(r"^(\d+):(\d{1,2})$")

# 芒果自己的档位：definition -> (宽, 高)
_DEFS = {1: (854, 480), 2: (1024, 576), 3: (1280, 720), 4: (1920, 1080)}


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _human(size: float) -> str:
    num = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return "-"


def _tk2() -> str:
    raw = "did={}|pno=1030|ver=0.3.0301|clit={}".format(uuid.uuid4(), int(time.time()))
    return base64.urlsafe_b64encode(raw.encode())[::-1].decode()


def _blocked(message: str) -> bool:
    """芒果把地区/版权限制也塞在 msg 里，认出来才能给出对的提示。"""
    text = (message or "").lower()
    return any(word in text for word in (
        "region", "country", "copyright", "area", "地区", "版权", "地域",
    ))


def _seconds(value) -> int:
    """芒果给时长有两种写法：纯秒数，或者 "82:51"。"""
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    m = _TIME_RE.match(text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


class MgtvSite(SiteAdapter):
    id = "mgtv"
    name = "芒果TV"
    home = "https://www.mgtv.com"
    color = "#FF6A00"
    examples = [
        "https://www.mgtv.com/b/325963/4686416.html",
        "https://www.mgtv.com/b/377996/12834526.html",
    ]
    needs_login = False
    note = ("官方 pcweb 接口取流：元数据免登录；播放地址是 HLS(m3u8) 分片，"
            "未登录只有 480P/576P/720P，1080P 要芒果TV会员")

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        text = (text or "").strip()
        if "mgtv.com" not in text.lower():
            return False
        return bool(_COLLECTION_RE.search(text) or _SHORT_RE.search(text))

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        m = _COLLECTION_RE.search(raw)
        if m:
            collection, video = m.group(1), m.group(2)
        else:
            m = _SHORT_RE.search(raw)
            if not m:
                raise ValueError(f"认不出这是芒果TV的哪一集：{raw[:80]}")
            collection, video = "", m.group(1)
        return Ref(raw=raw, site=self.id, ident=video, kind="video", p=1,
                   extra={"collection_id": collection})

    def normalize(self, ctx, ref: Ref) -> Ref:
        """/s/xxxx.html 这类短地址没有合集号，联网问一次接口补上。"""
        if ref.extra.get("collection_id") or not ref.ident:
            return ref
        try:
            data = self._player(ctx, ref.ident, _REFERER)
            collection = str((data.get("info") or {}).get("collection_id") or "").strip()
            if collection and collection != "0":
                ref.extra["collection_id"] = collection
        except Exception as exc:      # 补全失败不影响后面单独取这一集
            ref.note = f"合集号补全失败：{exc}"
        return ref

    # ---------- 网络 ----------
    @staticmethod
    def _referer(ref: Ref) -> str:
        collection = ref.extra.get("collection_id") or ""
        if collection and ref.ident:
            return f"https://www.mgtv.com/b/{collection}/{ref.ident}.html"
        return "https://www.mgtv.com/"

    def _json(self, ctx, url: str, referer: str, what: str) -> dict:
        headers = {"Accept": "application/json, text/plain, */*"}
        try:
            return ctx.net.get_json(url, headers=headers, referer=referer,
                                    retries=3, timeout=20)
        except Exception as exc:
            raise SiteUnavailable(f"芒果TV {what} 请求失败：{exc}") from None

    def _player(self, ctx, video_id: str, referer: str) -> dict:
        """播放接口：元数据 + atc.pm2 都在这里。"""
        query = urllib.parse.urlencode({"tk2": _tk2(), "video_id": video_id, "type": "pch5"})
        data = self._json(ctx, f"{_PLAYER_API}?{query}", referer, "播放接口")
        code = int(data.get("code") or 0)
        if code != 200:
            message = str(data.get("msg") or data.get("err_msg") or "接口没有返回内容")
            if _blocked(message):
                raise SiteUnavailable(f"芒果TV 提示地区/版权限制：{message}")
            raise SiteUnavailable(
                f"芒果TV 取不到这一集（video_id={video_id}）：{message}。"
                "常见原因是该内容已下线、地区限制，或接口改了参数。"
            )
        return data.get("data") or {}

    def _episodes(self, ctx, collection_id: str, referer: str) -> list:
        """分集列表：标题、时长、VIP 标记一次拿全。"""
        if not collection_id:
            return []
        query = urllib.parse.urlencode({
            "collection_id": collection_id, "page": 1, "size": 200,
            "_support": 10000000, "tk": 0,
        })
        data = self._json(ctx, f"{_EPISODE_API}?{query}", referer, "分集接口")
        if int(data.get("code") or 0) != 200:
            return []
        return ((data.get("data") or {}).get("list")) or []

    def _source(self, ctx, video_id: str, pm2: str, tk2: str, referer: str) -> dict:
        """用 pm2 换各档清晰度的相对地址。"""
        query = urllib.parse.urlencode({
            "_support": 10000000, "tk2": tk2, "pm2": pm2, "video_id": video_id,
            "type": "pch5", "auth_mode": 1, "cxid": "", "supportMse": 1,
            "src": "intelmgtv", "abroad": 10, "allowedRC": 1,
        })
        data = self._json(ctx, f"{_SOURCE_API}?{query}", referer, "取流接口")
        code = int(data.get("code") or 0)
        if code != 200:
            message = str(data.get("msg") or data.get("err_msg") or "接口没有返回内容")
            if _blocked(message):
                raise SiteUnavailable(
                    f"芒果TV 提示地区/版权限制：{message}"
                    "（这条链接在当前网络所在地不提供播放，换网络也未必能解）"
                )
            raise SiteUnavailable(
                f"芒果TV 没有下发播放地址（video_id={video_id}）：{message}。"
                "该内容多半需要登录且开通会员，未登录只能取到标题、封面这类元数据。"
            )
        return data.get("data") or {}

    def _resolve(self, ctx, domain: str, path: str, referer: str) -> str:
        """相对地址再换一次，返回真正的 m3u8 地址。"""
        url = urllib.parse.urljoin(domain.rstrip("/") + "/", path.lstrip("/"))
        headers = {"Accept": "application/json, text/plain, */*"}
        try:
            body = ctx.net.get_json(url, headers=headers, referer=referer,
                                    retries=2, timeout=20)
        except Exception:
            return ""
        if isinstance(body, dict):
            target = body.get("info") or body.get("url") or ""
            return target if isinstance(target, str) and target.startswith("http") else ""
        return ""

    # ---------- 元数据 ----------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        video_id = ref.ident
        if not video_id:
            raise SiteUnavailable("这条芒果TV链接里没有视频 ID，请用 /b/合集号/集号.html 形式的地址")
        referer = self._referer(ref)
        data = self._player(ctx, video_id, referer)
        info = data.get("info") or {}
        collection = str(info.get("collection_id") or ref.extra.get("collection_id") or "").strip()
        if collection:
            ref.extra["collection_id"] = collection

        try:
            episodes = self._episodes(ctx, collection, referer)
        except SiteUnavailable:
            episodes = []          # 分集列表挂了不影响这一集的元数据
        pages: list[Page] = []
        pubdate = 0
        for index, ep in enumerate(episodes, start=1):
            page_video = str(ep.get("video_id") or "").strip()
            if not page_video.isdigit():
                continue
            title = (ep.get("t3") or ep.get("t2") or "").strip()
            pages.append(Page(cid=int(page_video), page=index, part=title,
                              duration=_seconds(ep.get("time")), from_ep=True))
            if page_video == video_id:
                pubdate = self._pubdate(ep)

        duration = _seconds(info.get("duration"))
        part = (info.get("partName") or "").strip()
        if not pages:
            pages = [Page(cid=int(video_id) if video_id.isdigit() else 0, page=1,
                          part=part, duration=duration)]

        title = (info.get("title") or "").strip()
        if not title:
            raise SiteUnavailable(f"芒果TV没有返回这个视频的标题（video_id={video_id}），可能已下架")

        extra = {
            "video_id": video_id,
            "collection_id": collection,
            "part": part,
            "series": info.get("series") or "",
            "clip_type": info.get("clip_type") or "",
            "paymark": info.get("paymark") or "",
            "user": self._user_text(data.get("user") or {}),
        }
        return make_info(
            ref,
            title=title,
            cover=info.get("thumb") or "",
            desc=(info.get("desc") or part).strip(),
            duration=duration,
            pubdate=pubdate,
            tname="",
            owner=Owner(),
            pages=pages,
            stat={"episodes": len(pages)},
            kind="video",
            extra=extra,
        )

    @staticmethod
    def _pubdate(ep: dict) -> int:
        raw = str(ep.get("ts") or ep.get("t4") or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(time.mktime(time.strptime(raw[:26], fmt)))
            except (ValueError, TypeError):
                continue
        return 0

    @staticmethod
    def _user_text(user: dict) -> str:
        if user.get("login"):
            return "已登录" + ("（会员）" if str(user.get("isvip")) == "1" else "（非会员）")
        return "未登录"

    # ---------- 播放地址 ----------
    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        referer = self._referer(ref)
        video_id = self._page_video(ctx, ref, page, referer)

        data = self._player(ctx, video_id, referer)
        info = data.get("info") or {}
        pm2 = str(((data.get("atc") or {}).get("pm2")) or "").strip()
        if not pm2:
            raise SiteUnavailable(
                f"芒果TV 没有下发播放凭证（video_id={video_id}），"
                "通常是该内容需要登录或会员，未登录取不到地址。"
            )
        tk2 = str(((data.get("atc") or {}).get("tk2")) or "").strip() or _tk2()
        source = self._source(ctx, video_id, pm2, tk2, referer)

        domains = [d for d in (source.get("stream_domain") or []) if isinstance(d, str) and d]
        domain = domains[0] if domains else ""
        duration = _seconds(info.get("duration"))

        videos: list[Stream] = []
        declared: list[int] = []
        locked: list[str] = []
        for index, item in enumerate(source.get("stream") or []):
            if not isinstance(item, dict):
                continue
            definition = self._definition(item)
            width, height = self._size(item, definition)
            label = self._label(item, definition, height)
            qn_value = rank_from_resolution(width, height)
            declared.append(qn_value)
            path = str(item.get("url") or "")
            if not path or not domain:
                locked.append(label + ("（会员）" if str(item.get("vip")) == "1" else ""))
                continue
            target = self._resolve(ctx, domain, path, referer)
            if not target:
                locked.append(label)
                continue
            bandwidth = int(float(item.get("filebitrate") or 0) * 1000)
            videos.append(Stream(
                id=index + 1,
                qn=qn_value,
                label=label,
                width=width,
                height=height,
                codecs=str(item.get("videoFormat") or ""),
                bandwidth=bandwidth,
                size=int(float(item.get("fileSize") or 0)),
                # 取流接口只给码率不给体积，按 码率 × 时长 估一个，仅供界面显示
                size_hint=int(bandwidth * duration / 8) if bandwidth and duration else 0,
                url=target,
                mime="application/vnd.apple.mpegurl",
                kind="video",
                note=(item.get("tips") or "").strip(),
            ))

        if not videos:
            raise SiteUnavailable(
                "芒果TV 只返回了空地址：这一集在当前身份下没有可播放的清晰度，"
                "一般说明它是会员/付费内容（未登录连试看档位都不下发），"
                "也可能撞上了地区限制。标题、封面这类元数据仍可正常获取。"
            )

        best = self._pick(videos, qn)
        videos.sort(key=lambda s: (s.height, s.bandwidth), reverse=True)
        play = PlayInfo(engine=self.id, engine_name=self.name, duration=duration)
        play.videos = videos
        play.is_dash = False
        play.support_formats = ["hls"]
        play.accept_quality = sorted({v.qn for v in videos}, reverse=True)
        play.accept_desc = [v.label for v in videos]
        play.declared_quality = sorted(set(declared), reverse=True)
        play.phantom_quality = [q for q in play.declared_quality if q not in play.accept_quality]
        play.note = self._note(data, best, videos, locked, duration)
        play.raw = {"video_id": video_id, "picked": best.label,
                    "declared": play.declared_quality, "locked": locked}
        return play

    def _page_video(self, ctx, ref: Ref, page: int, referer: str) -> str:
        """第 N 集的 video_id：芒果的分集号就是接口里的 video_id。"""
        page = max(1, int(page or 1))
        if page == 1 or not ref.extra.get("collection_id"):
            return ref.ident
        episodes = self._episodes(ctx, ref.extra["collection_id"], referer)
        for index, ep in enumerate(episodes, start=1):
            if index == page:
                video = str(ep.get("video_id") or "").strip()
                if video:
                    return video
        raise SiteUnavailable(
            f"芒果TV这个合集里没有第 {page} 集（共 {len(episodes)} 集），换个集号再试"
        )

    @staticmethod
    def _definition(item: dict) -> int:
        """芒果把档位放在 def 里（有时候是字符串），另外留一个 definition 兜底。"""
        for key in ("def", "definition"):
            raw = str(item.get(key) or "").strip()
            if raw.isdigit():
                return int(raw)
        return 0

    @classmethod
    def _size(cls, item: dict, definition: int) -> tuple[int, int]:
        width = int(_number(item.get("videoWidth")))
        height = int(_number(item.get("videoHeight")))
        if not width or not height:
            width, height = _DEFS.get(definition, (0, 0))
        return width, height

    @staticmethod
    def _label(item: dict, definition: int, height: int) -> str:
        label = str(item.get("standardName") or item.get("barName")
                    or item.get("name") or "").strip()
        if label:
            return label
        if height:
            return f"{height}P"
        return f"档位{definition}" if definition else "未知档位"

    @staticmethod
    def _pick(videos: list[Stream], qn: int) -> Stream:
        """qn 命中就按 qn 挑，否则挑画质最高、码率最大的一条。"""
        if qn:
            same = [v for v in videos if v.qn == qn]
            if same:
                videos = same
        return max(videos, key=lambda s: (s.height, s.bandwidth))

    @staticmethod
    def _note(data: dict, best: Stream, videos: list, locked: list[str], duration: int) -> str:
        user = data.get("user") or {}
        who = "已登录" + ("（会员）" if str(user.get("isvip")) == "1" else "（非会员）") \
            if user.get("login") else "未登录"
        got = "、".join(f"{v.label}" for v in videos)
        parts = [
            f"画质来自芒果TV官方 pcweb 取流接口，当前身份：{who}",
            f"下发的档位：{got}",
            f"建议取 {best.label}（{best.width}x{best.height}，{best.codecs or '未知编码'}，"
            f"约 {_human(best.size or best.size_hint)}"
            f"{'，体积按码率估算' if not best.size else ''}）",
            "地址是 HLS 分片（m3u8），需要用支持 m3u8 的下载器或 ffmpeg 落地",
            "地址带时效，拿到后尽快下载",
        ]
        if locked:
            parts.append("接口没给地址的档位："
                         + "、".join(locked) + "（多为需要芒果TV会员）")
        return "；".join(parts)
