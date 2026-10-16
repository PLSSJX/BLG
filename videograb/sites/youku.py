"""优酷适配器。

元数据按下面的顺序逐条尝试（实测 2026-10）：
  1. openapi.youku.com/v2/videos/show.json?client_id=...&video_id={vid}&ext=show —— 主路径。
     公开 client_id 即可，返回标题/封面/时长/分类/发布时间/UP主/清晰度档位/付费状态。
  2. 同一套开放接口的精简版 show_basic.json —— 字段少一些（没有 bigThumbnail、show），
     但标题/封面/时长/分类/发布时间/streamtypes 都在，作为第 1 条被限流时的备份。
  3. m.youku.com/v_show/id_{vid}.html 的 window.__INITIAL_DATA__ —— 页面内嵌数据，
     播放地址和它同源，等于顺带把元数据取回来。
  4. v.youku.com/v_show/id_{vid}.html 的 window.__INITIAL_DATA__.videoMap ——
     另一套页面模板（PC 页），只有标题/封面/分类/发布时间，没有时长。

播放地址：
  * 只有移动页 https://m.youku.com/v_show/id_{vid}.html 一条通路下发地址。
    页面 SSR 出 window.__INITIAL_DATA__，播放数据有两处：componentList 里的播放节点
    （player.upsStream，主路径）和页面顶层 data.upsStream（节点结构变了也能兜住）。
    单段给 cdnUrl，多段给 firstSegment + segUrls；实测直链支持 Range（206），可多线程分片。
  * 注意移动页必须用移动端 UA：桌面 UA 拿到的是 PC 那套模板，一个播放地址都没有。

实测走不通的路径（不要再试）：
  * v.youku.com PC 页（桌面 UA 与移动 UA 都试过）—— 页面有数据，但没有 upsStream，
    一个媒体地址都不下发。
  * m.youku.com/video/id_{vid}.html、alipay_video/id_{vid}.html —— 同上，没有播放地址。
  * ups.youku.com/ups/get.json —— 匿名（ck=null）返回 -6005「用户账户异常、请重新登录」。
  * acs.youku.com .../mtop.youku.play.ups.appinfo.get —— FAIL_SYS_API_NOT_FOUNDED，
    连 _m_h5_tk 令牌都不下发。
  * www.youku.com/v_show/id_{vid}.html —— 只回一个 3KB 的跳转壳。

限制：移动页只对匿名可播放的内容下发播放源。会员 / 付费内容（noPlaySource）
不会给任何地址，此时如实抛 SiteUnavailable，只保证元数据可用。
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
# 同一套开放接口的精简版：字段少一些，但标题/封面/时长/分类/发布时间都在，作为元数据备用通路
OPENAPI_BASIC = "https://openapi.youku.com/v2/videos/show_basic.json"
PC_REFERER = "https://v.youku.com/"
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
    note = ("元数据四条通路依次重试（官方开放接口 show.json → show_basic.json → "
            "移动页内嵌数据 → PC 页内嵌 videoMap）；播放地址只有移动页内嵌数据这一条"
            "（PC 页/开放接口/ups/mtop 实测都不下发地址），会员/付费内容不下发播放源")
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

    def _openapi(self, ctx, vid: str, base: str = OPENAPI, ext: str = "show") -> dict | None:
        """官方开放接口（show.json / show_basic.json 两套字段都吃）。失败返回 None。"""
        params = [("client_id", CLIENT_ID), ("video_id", vid)]
        if ext:
            params.append(("ext", ext))
        url = base + "?" + urllib.parse.urlencode(params)
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
                     # 注意是 or {} 而不是 .get(..., {})：键存在但值为 null 时，
                     # 默认值不会生效，再 .get 就是 AttributeError
                     or ((data.get("player") or {}).get("upsStream") or {}).get("encodedId") == vid)
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

    # ---------------- 通路辅助 ----------------
    @staticmethod
    def _normalize_api(data: dict) -> dict:
        """把 show.json / show_basic.json 两套字段合成同一份元数据。"""
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        try:
            duration = int(float(data.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        stat = {}
        # 公开接口把播放/点赞数一律返回 0，宁可留空也不要在界面上显示假的 0
        for key, field in (("view", "view_count"), ("favorite", "favorite_count"),
                           ("comment", "comment_count"), ("like", "up_count")):
            value = int(data.get(field) or 0)
            if value:
                stat[key] = value
        return {
            "title": (data.get("title") or "").strip(),
            "cover": (data.get("bigThumbnail") or data.get("thumbnail_v2")
                      or data.get("thumbnail") or ""),
            "desc": data.get("description") or "",
            "tname": data.get("category") or "",
            "duration": duration,
            "pubdate": _timestamp(data.get("published") or data.get("created") or ""),
            "owner": Owner(mid=int(user.get("id") or data.get("userid") or 0),
                           name=user.get("name") or ""),
            "stat": stat,
            "extra": {
                "streamtypes": data.get("streamtypes") or [],
                "download_status": data.get("download_status") or [],
                "operation_limit": data.get("operation_limit") or [],
                "public_type": data.get("public_type") or "",
                "show": data.get("show") or {},
                "source": source.get("name") or "",
            },
        }

    def _mobile_meta(self, ctx, vid: str) -> dict | None:
        """移动页内嵌数据的元数据部分（播放地址和它同源，等于顺带取回）。"""
        initial = self._initial_data(ctx, vid)
        ups, route, node = self._stream_of(ctx, vid, initial)
        share = (initial.get("data") or {}).get("shareInfo") or {}
        title = cover = ""
        owner = Owner()
        stat: dict = {}
        if node is not None:
            title = (node.get("title") or share.get("shareTitle") or "").strip()
            cover = ((node.get("poster") or {}).get("img") or "")
            uploader = node.get("uploader") or {}
            owner = Owner(mid=int(ups.get("ownerId") or 0),
                          name=uploader.get("name") or uploader.get("userName") or "",
                          face=uploader.get("icon") or "")
            views = (node.get("videoView") or {}).get("count")
            if views is not None:
                stat["view"] = int(views)
        title = (title or share.get("shareTitle") or "").strip()
        cover = cover or (initial.get("data") or {}).get("coverUrl") or ""
        if not title:
            return None
        return {
            "title": title,
            "cover": cover,
            "desc": "",
            "tname": "",
            "duration": int((ups.get("milliSeconds") or 0) / 1000),
            "pubdate": 0,
            "owner": owner,
            "stat": stat,
            "extra": {"from": "mobile_page", "play_route": route},
        }

    def _pc_meta(self, ctx, vid: str) -> dict | None:
        """PC 页内嵌 videoMap。

        注意：桌面 UA 请求 v.youku.com 会拿到另一套页面结构（pageMap/moduleList，
        没有 videoMap），所以这条通路固定用移动端 UA 去要 PC 页。
        """
        url = "https://v.youku.com/v_show/id_" + vid + ".html"
        try:
            res = ctx.net.request(url, headers={
                "User-Agent": MOBILE_UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }, referer=PC_REFERER, retries=2, timeout=20)
        except Exception:
            return None
        if res.status != 200:
            return None
        html = res.text()
        if "_____tmd_____" in html:
            raise SiteUnavailable("PC 页触发了风控验证（punish 页）")
        found = _INITIAL_DATA.search(html)
        data = _json_after(html, found) if found else None
        if not isinstance(data, dict):
            return None
        video = data.get("videoMap") or {}
        title = (video.get("videoTitle") or "").strip()
        if not title:
            return None
        return {
            "title": title,
            "cover": video.get("videoImgV") or video.get("videoImg") or "",
            "desc": "",
            "tname": video.get("videoCategory") or "",
            "duration": 0,
            "pubdate": _timestamp(video.get("videoPublishTime") or ""),
            "owner": Owner(),
            "stat": {},
            "extra": {"from": "pc_page",
                      "video_long_id": video.get("videoLongId") or ""},
        }

    def _meta_routes(self, ctx, vid: str):
        """元数据通路表：(名称, 取值函数)，按顺序尝试。"""

        def from_openapi(base: str, ext: str):
            data = self._openapi(ctx, vid, base, ext)
            return self._normalize_api(data) if data else None

        yield "openapi show.json（官方开放接口）", lambda: from_openapi(OPENAPI, "show")
        yield "openapi show_basic.json（官方开放接口精简版）", \
            lambda: from_openapi(OPENAPI_BASIC, "")
        yield "m.youku.com 移动页内嵌数据", lambda: self._mobile_meta(ctx, vid)
        yield "v.youku.com PC 页内嵌 videoMap", lambda: self._pc_meta(ctx, vid)

    def _stream_of(self, ctx, vid: str, initial: dict | None = None):
        """播放数据在页面里有两处，先按目标 vid 找播放节点，再退回页面顶层。

        返回 (upsStream, 通路名, 节点或 None)。
        """
        if initial is None:
            initial = self._initial_data(ctx, vid)
        node = None
        try:
            node = self._main_node(initial, vid)
        except SiteUnavailable:
            node = None
        if node is not None:
            ups = (node.get("player") or {}).get("upsStream") or {}
            if ups.get("cdnUrl") or ups.get("segUrls") or ups.get("firstSegment"):
                return ups, "移动页 componentList 播放节点", node
        top = (initial.get("data") or {}).get("upsStream") or {}
        if (str(top.get("encodedId") or "") == vid
                and (top.get("cdnUrl") or top.get("segUrls") or top.get("firstSegment"))):
            return top, "移动页顶层 upsStream", node
        if node is not None:
            return ((node.get("player") or {}).get("upsStream") or {},
                    "移动页 componentList 播放节点", node)
        return {}, "移动页（没有播放节点）", None

    @staticmethod
    def _urls_of(ups: dict) -> list:
        """单段给 cdnUrl，多段给 firstSegment + segUrls。"""
        if ups.get("cdnUrl"):
            return [ups["cdnUrl"]]
        urls: list[str] = []
        first = ups.get("firstSegment")
        if first:
            urls.append(first)
        for seg in ups.get("segUrls") or []:
            if isinstance(seg, dict):
                one = seg.get("segCdnUrl") or seg.get("cdnUrl")
                if one:
                    urls.append(one)
        return urls


    # ---------------- 元数据 ----------------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        vid = self._vid(ref)
        reasons: list[str] = []
        base: dict | None = None
        route = ""
        for name, load in self._meta_routes(ctx, vid):
            # 标题、时长、封面凑齐就可以收工，剩下的通路不再打扰
            if base and base.get("title") and base.get("duration") and base.get("cover"):
                break
            try:
                got = load()
            except SiteUnavailable as exc:
                reasons.append(f"{name}：{exc}")
                continue
            if not got or not got.get("title"):
                reasons.append(f"{name}：没有返回可用标题")
                continue
            if base is None:
                base, route = got, name
                continue
            # 后面这条通路只用来补前一条缺的字段
            for key in ("cover", "desc", "tname", "duration", "pubdate", "stat", "owner"):
                if not base.get(key) and got.get(key):
                    base[key] = got[key]
                    reasons.append(f"{name}：补充了 {key}")

        if not base or not base.get("title"):
            raise SiteUnavailable(
                f"优酷所有元数据通路都没取到这个视频的信息（vid={vid}）："
                + "；".join(reasons or ["没有可用通路"])
            )

        duration = int(base.get("duration") or 0)
        extra = dict(base.get("extra") or {})
        extra.update({"vid": vid, "detail_route": route})
        info = VideoInfo(
            title=base["title"],
            cover=base.get("cover") or "",
            desc=base.get("desc") or "",
            duration=duration,
            pubdate=int(base.get("pubdate") or 0),
            tname=base.get("tname") or "",
            owner=base.get("owner") or Owner(),
            pages=[Page(cid=_vid_to_cid(vid), page=1, part=base["title"], duration=duration)],
            stat=base.get("stat") or {},
            kind="video",
            extra=extra,
        )
        info.site = self.id
        return info

    # ---------------- 播放地址 ----------------
    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        vid = self._vid(ref)
        ups, route, _node = self._stream_of(ctx, vid)
        urls = self._urls_of(ups)
        ref.extra["detail_route"] = route

        if not urls:
            raise SiteUnavailable(
                "优酷没有给这个视频下发播放地址：移动页返回 noPlaySource / 空的 upsStream，"
                "通常说明它是会员或付费内容（需要登录且开通会员），"
                "也可能是地区限制或已下线。未登录状态下只能取到标题、封面这类元数据。"
                f"（本次走的通路：{route}）"
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
                note=(f"优酷移动页内嵌直链（通路：{route}；{stream_type or '未知档位'}），"
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
            play.note = (f"优酷移动页内嵌直链（通路：{route}），共 {len(urls)} 段，"
                         f"按顺序拼接即可播放；"
                         f"接口只给总大小 {size} 字节，单段大小未下发")

        if width and height:
            play.raw = {"width": width, "height": height, "streamType": stream_type,
                        "size": size, "vid": vid, "route": route}
        if page and page > 1:
            play.note += f"（优酷单集视频没有分 P，已忽略 page={page}）"
        return play
