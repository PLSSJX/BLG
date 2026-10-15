"""优酷适配器。

实测可用路径（2026-10 逐一验证）：
  1. 元数据 —— 优酷官方开放接口
     https://openapi.youku.com/v2/videos/show.json?client_id=...&video_id={vid}&ext=show
     公开 client_id 即可，返回标题 / 封面 / 时长 / 分类 / 发布时间 / UP 主 /
     清晰度档位 streamtypes / 付费状态 download_status。
  2. 播放地址 —— 移动页 https://m.youku.com/v_show/id_{vid}.html
     页面 SSR 出 window.__INITIAL_DATA__，播放节点的 player.upsStream 里直接就是
     真实 mp4 直链（单段给 cdnUrl，多段给 firstSegment + segUrls）。
     实测直链支持 Range，返回 206 + Content-Range，可断点续传、可多线程分片。

实测走不通的路径：
  * PC 页 v.youku.com/v_show/id_{vid}.html —— 换任何请求头都直接回风控页
    （_____tmd_____/punish），一个字节的内嵌数据都拿不到。
  * ups.youku.com/ups/get.json —— 匿名（ck=null，以及按 _m_h5_tk 自算 md5 的 ck）
    一律返回 -6005「用户账户异常、请重新登录」；它要的是真实登录 Cookie。
  * acs.youku.com .../mtop.youku.play.ups.appinfo.get —— FAIL_SYS_API_NOT_FOUNDED。

限制：移动页只对匿名可播放的内容下发播放源。会员 / 付费内容（noPlaySource）
不会给任何地址，此时如实抛 SiteUnavailable，只保证元数据可用。
注意移动页必须用移动端 UA：桌面 UA 访问 m.youku.com 会被判成爬虫直接风控（实测）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse

from ..models import Durl, Owner, Page, PlayInfo, VideoInfo
from ..quality import rank_from_resolution
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, single_file_play

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 移动页只认移动端 UA：桌面 UA 会被风控页拦下，这是实测结论而不是偷懒。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)
MOBILE_REFERER = "https://m.youku.com/"
OPENAPI = "https://openapi.youku.com/v2/videos/show.json"
# 优酷官方开放平台的公开 client_id（网页播放器 partnerid），只用于读公开元数据
CLIENT_ID = "53e6cc67237fc59a"

# 页面里的 vid：优酷 vid 一律以大写 X 开头，id_z... 是专辑 show id，不要误收
_VID_IN_TEXT = re.compile(r"id_(X[A-Za-z0-9]+={0,2})")
_VID_BARE = re.compile(r"X[A-Za-z0-9]{10,22}={0,2}")
_INITIAL_DATA = re.compile(r"window\.__INITIAL_DATA__\s*=\s*")

# 优酷 stream_type -> 中文画质名（具体分辨率优先用接口给的 width/height）
STREAM_LABELS = {
    "3gphd": "极速",
    "flvhd": "流畅",
    "sd": "标清",
    "hd": "高清",
    "hd2": "超清",
    "hd3": "超清",
    "mp4sd": "流畅",
    "mp4hd": "高清",
    "mp4hd2": "超清",
    "mp4hd2v2": "超清",
    "mp4hd3": "超清",
    "mp4hd3v2": "超清",
    "mp4bd": "蓝光",
    "mp5hd4v3": "H.265 超清",
}

_CACHE_TTL = 60.0


def _node_list(initial: dict):
    """把 __INITIAL_DATA__ 里所有 component 的 dataNode 摊平成一个列表。"""
    for comp in initial.get("componentList") or []:
        if not isinstance(comp, dict):
            continue
        for key in ("dataNode", "dataNode_player", "dataNode_recommend"):
            for node in comp.get(key) or []:
                if isinstance(node, dict) and isinstance(node.get("data"), dict):
                    yield node["data"]


def _json_after(text: str, match: re.Match) -> dict | None:
    """从赋值语句后面抠出一个完整的 JSON 对象（带字符串/转义感知的括号匹配）。"""
    start = match.end()
    depth = 0
    in_str = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except ValueError:
                    return None
    return None


def _timestamp(text: str) -> int:
    try:
        return int(time.mktime(time.strptime((text or "").strip(), "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError):
        return 0


def _vid_to_cid(vid: str) -> int:
    """Page.cid 要 int，vid 是字符串，取哈希前 7 字节保证稳定且够分散。"""
    digest = hashlib.md5(vid.encode("utf-8")).digest()
    return int.from_bytes(digest[:7], "big")


class YoukuSite(SiteAdapter):
    id = "youku"
    name = "优酷"
    home = "https://www.youku.com"
    color = "#1BA6FF"
    examples = [
        "https://v.youku.com/v_show/id_XNjU1Nzk5NjI0OA==.html",
        "https://m.youku.com/v_show/id_XNjU1Nzk5NjI0OA==.html",
        "vid：XNjU1Nzk5NjI0OA==",
    ]
    note = "元数据走官方开放接口；播放地址靠移动页内嵌数据，会员/付费内容不下发播放源"
    needs_login = False

    def __init__(self) -> None:
        self._pages: dict[str, tuple[float, dict]] = {}

    # ---------------- 识别与解析 ----------------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if "youku.com" in raw.lower():
            return True
        return bool(_VID_BARE.fullmatch(raw))

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise SiteUnavailable("请输入优酷视频地址或 vid")
        plain = urllib.parse.unquote(raw)
        vid = ""
        found = _VID_IN_TEXT.search(plain)
        if found:
            vid = found.group(1)
        elif _VID_BARE.fullmatch(raw):
            vid = raw
        if not vid:
            raise SiteUnavailable(
                "这个优酷链接里找不到 vid（形如 id_XNjU1Nzk5NjI0OA==）。"
                "专辑页 / 频道页需要先点进具体某一集再复制地址。"
            )
        page = 1
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            plain if "://" in plain else "https://" + plain.lstrip("/")).query)
        for key in ("p", "page", "ep"):
            values = query.get(key)
            if values and str(values[0]).isdigit():
                page = max(1, int(values[0]))
                break
        ref = Ref(raw=raw, site=self.id, ident=vid, kind="video", p=page)
        ref.extra = {"vid": vid}
        return ref

    # ---------------- 内部：两个数据源 ----------------
    @staticmethod
    def _vid(ref: Ref) -> str:
        vid = (ref.extra or {}).get("vid") or ref.ident
        if not vid:
            raise SiteUnavailable("Ref 里没有优酷 vid，无法继续")
        return vid

    def _openapi(self, ctx, vid: str) -> dict | None:
        """官方开放接口，拿元数据。失败返回 None，交给移动页兜底。"""
        url = (OPENAPI + "?client_id=" + CLIENT_ID +
               "&video_id=" + urllib.parse.quote(vid, safe="") + "&ext=show")
        try:
            data = ctx.net.get_json(url, headers={
                "User-Agent": DESKTOP_UA,
                "Accept": "application/json, */*",
            }, referer="https://www.youku.com/", retries=2, timeout=20)
        except Exception:
            return None
        if not isinstance(data, dict) or not data.get("title"):
            return None
        return data

    def _initial_data(self, ctx, vid: str) -> dict:
        """移动页 window.__INITIAL_DATA__，播放地址的唯一来源。带 60 秒缓存。"""
        cached = self._pages.get(vid)
        now = time.time()
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

        url = "https://m.youku.com/v_show/id_" + vid + ".html"
        try:
            res = ctx.net.request(url, headers={
                "User-Agent": MOBILE_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }, referer=MOBILE_REFERER, retries=2, timeout=20)
        except Exception as exc:
            raise SiteUnavailable(f"优酷移动页请求失败：{str(exc)[:120]}") from None
        if res.status != 200:
            raise SiteUnavailable(f"优酷移动页返回 HTTP {res.status}，vid={vid}")
        html = res.text()
        if "_____tmd_____" in html or "punish" in html[:4000]:
            raise SiteUnavailable(
                "优酷移动页触发了风控验证（跳转到 punish 页），当前网络下拿不到数据，"
                "稍等几分钟或换个网络再试"
            )
        found = _INITIAL_DATA.search(html)
        data = _json_after(html, found) if found else None
        if not isinstance(data, dict):
            raise SiteUnavailable(
                "优酷移动页没有再内嵌 window.__INITIAL_DATA__（页面结构可能已改版），"
                "当前取不到播放地址"
            )
        self._pages[vid] = (now, data)
        return data

    @staticmethod
    def _main_node(initial: dict, vid: str) -> dict:
        """在所有 component 节点里挑出属于目标 vid 的那一个。"""
        fallback = None
        for data in _node_list(initial):
            preview = data.get("preview") or {}
            match = (preview.get("vid") == vid
                     or (data.get("player") or {}).get("upsStream", {}).get("encodedId") == vid)
            if not match:
                continue
            stream = ((data.get("player") or {}).get("upsStream") or {})
            if stream.get("cdnUrl") or stream.get("segUrls") or stream.get("firstSegment"):
                return data
            if fallback is None:
                fallback = data
        if fallback is not None:
            return fallback
        raise SiteUnavailable(
            f"优酷移动页里没有 vid={vid} 的播放节点，可能是直播回看或已下架的内容"
        )

    # ---------------- 元数据 ----------------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        vid = self._vid(ref)
        data = self._openapi(ctx, vid)

        title = cover = desc = tname = ""
        duration = 0
        pubdate = 0
        owner = Owner()
        stat: dict = {}
        extra: dict = {"vid": vid}

        if data:
            title = (data.get("title") or "").strip()
            cover = data.get("bigThumbnail") or data.get("thumbnail") or ""
            desc = data.get("description") or ""
            tname = data.get("category") or ""
            try:
                duration = int(float(data.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
            pubdate = _timestamp(data.get("published") or data.get("created") or "")
            user = data.get("user") or {}
            owner = Owner(mid=int(user.get("id") or 0), name=user.get("name") or "")
            # 公开接口把播放/点赞数一律返回 0，宁可留空也不要在界面上显示假的 0
            for key, field in (("view", "view_count"), ("favorite", "favorite_count"),
                               ("comment", "comment_count"), ("like", "up_count")):
                value = int(data.get(field) or 0)
                if value:
                    stat[key] = value
            extra.update({
                "streamtypes": data.get("streamtypes") or [],
                "download_status": data.get("download_status") or [],
                "operation_limit": data.get("operation_limit") or [],
                "public_type": data.get("public_type") or "",
                "show": data.get("show") or {},
                "source": (data.get("source") or {}).get("name") or "",
            })

        # 开放接口挂了就退到移动页，至少要把标题/封面/时长凑齐
        if not title:
            initial = self._initial_data(ctx, vid)
            node = self._main_node(initial, vid)
            share = initial.get("data", {}).get("shareInfo") or {}
            title = (node.get("title") or share.get("shareTitle") or "").strip()
            cover = (initial.get("data", {}).get("coverUrl")
                     or ((node.get("poster") or {}).get("img") or ""))
            ups = ((node.get("player") or {}).get("upsStream") or {})
            duration = duration or int((ups.get("milliSeconds") or 0) / 1000)
            uploader = node.get("uploader") or {}
            owner = owner if owner.name else Owner(
                mid=int(ups.get("ownerId") or 0),
                name=uploader.get("name") or uploader.get("userName") or "",
                face=uploader.get("icon") or "",
            )
            views = (node.get("videoView") or {}).get("count")
            if views is not None:
                stat["view"] = int(views)
            extra["from"] = "mobile_page"

        if not title:
            raise SiteUnavailable(f"优酷没有返回这个视频的信息（vid={vid}），可能已下架")

        info = VideoInfo(
            title=title,
            cover=cover,
            desc=desc,
            duration=duration,
            pubdate=pubdate,
            tname=tname,
            owner=owner,
            pages=[Page(cid=_vid_to_cid(vid), page=1, part=title, duration=duration)],
            stat=stat,
            kind="video",
            extra=extra,
        )
        info.site = self.id
        return info

    # ---------------- 播放地址 ----------------
    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        vid = self._vid(ref)
        initial = self._initial_data(ctx, vid)
        node = self._main_node(initial, vid)
        ups = (node.get("player") or {}).get("upsStream") or {}

        urls: list[str] = []
        if ups.get("cdnUrl"):
            urls = [ups["cdnUrl"]]
        else:
            first = ups.get("firstSegment")
            if first:
                urls.append(first)
            for seg in ups.get("segUrls") or []:
                if isinstance(seg, dict):
                    one = seg.get("segCdnUrl") or seg.get("cdnUrl")
                    if one:
                        urls.append(one)

        if not urls:
            raise SiteUnavailable(
                "优酷没有给这个视频下发播放地址：移动页返回 noPlaySource / 空的 upsStream，"
                "通常说明它是会员或付费内容（需要登录且开通会员），"
                "也可能是地区限制或已下线。未登录状态下只能取到标题、封面这类元数据。"
            )

        width = int(ups.get("width") or 0)
        height = int(ups.get("height") or 0)
        stream_type = (ups.get("streamType") or "").lower()
        size = int(ups.get("size") or 0)
        seconds = int((ups.get("milliSeconds") or 0) / 1000)
        short_side = min([x for x in (width, height) if x] or [0])
        label = STREAM_LABELS.get(stream_type, "")
        if short_side:
            label = (label + f" {short_side}P").strip()
        label = label or (stream_type or "优酷直链")

        if len(urls) == 1:
            play = single_file_play(
                self, urls[0], size=size, duration=seconds, quality=label,
                note=(f"优酷移动页内嵌直链（{stream_type or '未知档位'}），"
                      f"音视频已封装，下完即播；清晰度由优酷按匿名身份下发，不可选"),
            )
            # 辅助函数默认 accept_quality=[0]，这里补上真实档位，跨平台比画质才准
            if short_side:
                play.accept_quality = [rank_from_resolution(short_side, short_side)]
        else:
            segments = ups.get("segUrls") or []
            lengths = [int((seg.get("segMilliseconds") or 0) / 1000)
                       for seg in segments if isinstance(seg, dict)]
            first_len = max(0, seconds - sum(lengths)) or (lengths[0] if lengths else 0)
            play = PlayInfo(engine=self.id, engine_name=self.name, duration=seconds)
            for index, url in enumerate(urls):
                length = first_len if index == 0 else (
                    lengths[index - 1] if index - 1 < len(lengths) else 0)
                play.durl.append(Durl(order=index + 1, url=url, size=0, length=length))
            play.accept_quality = [rank_from_resolution(short_side, short_side)] if short_side else [0]
            play.accept_desc = [label]
            play.note = (f"优酷移动页内嵌直链，共 {len(urls)} 段，按顺序拼接即可播放；"
                         f"接口只给总大小 {size} 字节，单段大小未下发")

        if width and height:
            play.raw = {"width": width, "height": height, "streamType": stream_type,
                        "size": size, "vid": vid}
        if page and page > 1:
            play.note += f"（优酷单集视频没有分 P，已忽略 page={page}）"
        return play
