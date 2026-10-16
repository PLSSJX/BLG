"""腾讯视频适配器。

实测可用的取流链路（免登录）：
  1. vv.video.qq.com/getinfo 是留给网页/客户端的公开接口，不签名也能调。
     返回的 vl.vi[0] 里有 fn(文件名)、fvkey(播放凭证)、ul.ui[](各 CDN 根地址)，
     拼成 "根地址 + fn + ?vkey=fvkey" 就是可以 Range 直接下载的 MP4。
  2. 剧集/综艺的封面信息走 node.video.qq.com/x/api/float_vinfo2，
     标题、简介、封面、正片 vid 列表(video_ids)都在里面；纯 UGC 视频没有这个接口的数据，
     这时只用 getinfo 的标题和时长。

限制：非会员只有试看片段。接口会给 preview（当前身份能看的秒数）和 vi.td（总时长），
preview 明显小于总时长就是试看，实测会员内容普遍只给 180 秒左右。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import zlib

from ..models import Durl, Owner, Page, PlayInfo, VideoInfo
from ..quality import rank_from_resolution
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info

# 取流通路表：(名称, getinfo 网关)。两条网关的协议完全一样，但域名/机房不同，
# 实测两条都能出 fvkey + ul（第 1 条是主路径，第 2 条是 H5 网关，被限流时顶上）。
_GETINFO_HOSTS = (
    ("vv.video.qq.com/getinfo（PC 网关）", "https://vv.video.qq.com/getinfo"),
    ("h5vv.video.qq.com/getinfo（H5 网关）", "https://h5vv.video.qq.com/getinfo"),
)
_COVER_API = "https://node.video.qq.com/x/api/float_vinfo2"
_REFERER = "https://v.qq.com/"

_COVER_EP_RE = re.compile(r"v\.qq\.com/x/cover/(?P<cid>[A-Za-z0-9]+)/(?P<vid>[A-Za-z0-9]+)", re.I)
_COVER_RE = re.compile(r"v\.qq\.com/x/cover/(?P<cid>[A-Za-z0-9]+)", re.I)
_PAGE_RE = re.compile(r"v\.qq\.com/x/(?:page|detail)/(?P<vid>[A-Za-z0-9]+)", re.I)
_M_PLAY_RE = re.compile(r"v\.qq\.com/x/m/play", re.I)

# 分集标题只能一集一集问，太多了会拖慢取信息，只补前这么多集，其余留空由界面显示 P 号
_TITLE_LIMIT = 16


def _vid_to_cid(vid: str) -> int:
    """腾讯的 vid 是字符串，Page.cid 只收 int，这里做一次稳定的 crc32 映射。"""
    return zlib.crc32(vid.encode("utf-8")) & 0x7FFFFFFF


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _seconds(value) -> int:
    return int(_number(value))


def _human(size: float) -> str:
    num = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return "-"


class VqqSite(SiteAdapter):
    id = "vqq"
    name = "腾讯视频"
    home = "https://v.qq.com"
    color = "#FF7C1E"
    examples = [
        "https://v.qq.com/x/cover/mzc002001bk7moh/z41023tinp7.html",
        "https://v.qq.com/x/page/q326831cny0.html",
    ]
    needs_login = False
    note = ("取流走两条 getinfo 网关依次重试（vv.video.qq.com → h5vv.video.qq.com）："
            "免登录可拿 480P/720P 的 MP4 直链；"
            "会员内容只下发 180 秒左右试看，1080P 及以上要登录并开通会员")

    def __init__(self) -> None:
        # 上次成功的取流网关（优先复用）与本条数据实际走的通路名（写进报告）
        self._preferred = ""
        self._route = ""

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        text = (text or "").strip()
        if "v.qq.com" not in text.lower():
            return False
        return bool(_COVER_EP_RE.search(text) or _COVER_RE.search(text)
                    or _PAGE_RE.search(text) or _M_PLAY_RE.search(text))

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        cid = vid = ""
        m = _COVER_EP_RE.search(raw)
        if m:
            cid, vid = m.group("cid"), m.group("vid")
        else:
            m = _PAGE_RE.search(raw)
            if m:
                vid = m.group("vid")
            else:
                m = _COVER_RE.search(raw)
                if m:
                    cid = m.group("cid")
        if _M_PLAY_RE.search(raw):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
            vid = vid or (query.get("vid") or [""])[0]
            cid = cid or (query.get("cid") or [""])[0]
        if not vid and not cid:
            raise ValueError(f"认不出这是腾讯视频的哪一集：{raw[:80]}")
        return Ref(raw=raw, site=self.id, ident=vid, kind="video", p=1,
                   extra={"cid": cid, "page_url": raw})

    def normalize(self, ctx, ref: Ref) -> Ref:
        """补齐缺的那一半：只给了合集号就顺手取第一集，只有 vid 就问一次 cid。"""
        if not ref.ident and ref.extra.get("cid"):
            try:
                vids = self._episode_vids(self._cover(ctx, ref.extra["cid"]), "")
            except SiteUnavailable as exc:
                ref.note = str(exc)
                vids = []
            if not vids:
                raise SiteUnavailable(
                    "这个腾讯视频合集里没有可播放的正片（可能只有预告，或者已下架），"
                    "打开具体一集的页面再试"
                )
            ref.ident = vids[0]
            ref.extra["vids"] = vids
        elif ref.ident and not ref.extra.get("cid"):
            try:
                data, vi = self._play(ctx, ref.ident, self._referer(ref))
                cid = str(vi.get("cid") or "").strip()
                if cid:
                    ref.extra["cid"] = cid
            except SiteUnavailable as exc:
                ref.note = str(exc)
        return ref

    # ---------- 网络 ----------
    @staticmethod
    def _referer(ref: Ref) -> str:
        cid = ref.extra.get("cid") or ""
        if cid and ref.ident:
            return f"https://v.qq.com/x/cover/{cid}/{ref.ident}.html"
        if ref.ident:
            return f"https://v.qq.com/x/page/{ref.ident}.html"
        return _REFERER

    def _ordered_hosts(self) -> list:
        """上次成功的网关优先，减少故障切换时的等待。"""
        if self._preferred:
            return ([h for h in _GETINFO_HOSTS if h[1] == self._preferred]
                    + [h for h in _GETINFO_HOSTS if h[1] != self._preferred])
        return list(_GETINFO_HOSTS)

    @staticmethod
    def _parse_getinfo(raw: str, vid: str) -> dict:
        m = re.search(r"QZOutputJson=(\{.*\})\s*;?\s*$", raw.strip(), re.S)
        if not m:
            raise SiteUnavailable("返回的不是预期格式，接口可能改了")
        try:
            data = json.loads(m.group(1))
        except ValueError:
            raise SiteUnavailable("返回的 JSON 解析失败") from None
        if str(data.get("s") or "") != "o":
            message = str(data.get("msg") or "").strip()
            if "区域" in message or "area" in message.lower():
                raise SiteUnavailable(f"提示地区限制：{message}")
            raise SiteUnavailable(
                f"取不到这一集（vid={vid}）：{message or '接口没有返回内容'}"
                "（常见原因：需要会员、内容已下线，或链接里的 vid 不对）"
            )
        return data

    def _getinfo(self, ctx, vid: str, referer: str, defn: str = "") -> dict:
        """取一路播放信息，defn 为空表示“当前身份能看到的最好那一档”。

        两条网关按顺序试，全部失败时把每条的原因一起抛出去。
        """
        query = {"vids": vid, "platform": "101001", "charge": "0",
                 "defaultfmt": "auto", "otype": "json", "sphttps": "1"}
        if defn:
            query["defn"] = defn
        headers = {"Accept": "*/*"}
        encoded = urllib.parse.urlencode(query)
        reasons: list[str] = []
        for name, base in self._ordered_hosts():
            try:
                raw = ctx.net.get_text(f"{base}?{encoded}", headers=headers,
                                       referer=referer, retries=2, timeout=20)
            except Exception as exc:
                reasons.append(f"{name}：请求失败（{str(exc)[:70]}）")
                continue
            try:
                data = self._parse_getinfo(raw, vid)
            except SiteUnavailable as exc:
                reasons.append(f"{name}：{exc}")
                continue
            self._preferred = base
            self._route = name
            return data
        raise SiteUnavailable(
            f"腾讯视频所有取流通路都失败（vid={vid}）：" + "；".join(reasons)
        )

    def _play(self, ctx, vid: str, referer: str, defn: str = "") -> tuple[dict, dict]:
        data = self._getinfo(ctx, vid, referer, defn)
        items = ((data.get("vl") or {}).get("vi")) or []
        if not items:
            raise SiteUnavailable(f"腾讯视频没有返回 vid={vid} 的播放信息，可能已下线")
        return data, items[0]

    def _cover(self, ctx, cid: str) -> dict:
        try:
            data = ctx.net.get_json(f"{_COVER_API}?cid={urllib.parse.quote(cid)}",
                                    headers={"Accept": "application/json, text/plain, */*"},
                                    referer=_REFERER, retries=3, timeout=20)
        except Exception as exc:
            raise SiteUnavailable(f"腾讯视频封面接口请求失败：{exc}") from None
        return data.get("c") or {}

    @staticmethod
    def _episode_vids(cover: dict, vid: str) -> list:
        """合集里的正片 vid 顺序；当前 vid 不在合集里（比如花絮）就补在最后。"""
        vids = [str(v) for v in (cover.get("video_ids") or []) if v]
        if vid and vid not in vids:
            vids.append(vid)
        return vids

    # ---------- 元数据 ----------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        vid = ref.ident
        if not vid:
            raise SiteUnavailable(
                "这条腾讯视频链接里没有 vid，请用 /x/cover/合集号/集号.html 形式的地址"
            )
        referer = self._referer(ref)
        data, vi = self._play(ctx, vid, referer)
        cid = str(vi.get("cid") or ref.extra.get("cid") or "").strip()
        if cid:
            ref.extra["cid"] = cid

        cover: dict = {}
        if cid:
            try:
                cover = self._cover(ctx, cid)
            except SiteUnavailable:
                cover = {}     # 纯 UGC 视频本来就没有合集信息，不算错误
        vids = self._episode_vids(cover, vid)

        duration = _seconds(vi.get("td"))
        preview = int(_number(data.get("preview")))
        trial = bool(preview and duration and preview + 1 < duration)
        pages = self._pages(ctx, vids, vid, vi, referer)
        title = str(cover.get("title") or vi.get("ti") or "").strip() or vid
        info = make_info(
            ref,
            title=title,
            cover=str(cover.get("pic") or ""),
            desc=str(cover.get("description") or ""),
            duration=duration,
            pubdate=0,
            tname="",
            owner=Owner(),
            pages=pages,
            stat={"episodes": len(pages)},
            kind="video",
            extra={
                "vid": vid,
                "cid": cid,
                "detail_route": self._route,
                "vids": vids,
                "year": cover.get("year") or "",
                "cast": self._cast(cover.get("nam")),
                "preview_seconds": preview,
                "access": f"试看 {preview} 秒" if trial else "完整",
                "quality": str(vi.get("br") or ""),
            },
        )
        return info

    def _pages(self, ctx, vids: list, vid: str, vi: dict, referer: str) -> list:
        pages: list[Page] = []
        for index, page_vid in enumerate(vids, start=1):
            part = ""
            seconds = 0
            if page_vid == vid:
                part = str(vi.get("ti") or "").strip()
                seconds = _seconds(vi.get("td"))
            elif index <= _TITLE_LIMIT:
                try:
                    _, one = self._play(ctx, page_vid, referer)
                    part = str(one.get("ti") or "").strip()
                    seconds = _seconds(one.get("td"))
                except SiteUnavailable:
                    part = ""
            pages.append(Page(cid=_vid_to_cid(page_vid), page=index, part=part,
                              duration=seconds, from_ep=True))
        if not pages:
            pages = [Page(cid=_vid_to_cid(vid), page=1,
                          part=str(vi.get("ti") or ""), duration=_seconds(vi.get("td")))]
        return pages

    # ---------- 播放地址 ----------
    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        referer = self._referer(ref)
        vid = self._page_vid(ctx, ref, page)
        data, vi = self._play(ctx, vid, referer)

        # 默认档位之外，再试着要一次接口声称支持的最高档，确实更高就用它
        for defn in self._upgrade_names(data, vi):
            try:
                better_data, better = self._play(ctx, vid, referer, defn)
            except SiteUnavailable:
                continue
            if _number(better.get("br")) > _number(vi.get("br")):
                data, vi = better_data, better
                break

        urls = self._urls(vi)
        if not urls:
            raise SiteUnavailable(
                f"腾讯视频没有下发播放地址（vid={vid}）：这一集在当前身份下没有可用清晰度，"
                "通常说明它是会员/付费内容，或需要登录。标题、封面这类元数据仍能取到。"
            )

        duration = _seconds(vi.get("td"))
        preview = int(_number(data.get("preview")))
        trial = bool(preview and duration and preview + 1 < duration)
        length = preview if trial else duration

        size = 0
        try:
            size = ctx.net.content_length(urls[0], referer=referer) or 0
        except Exception:
            size = 0
        size = size or int(_number(vi.get("fs")))

        width = int(_number(vi.get("vw")))
        height = int(_number(vi.get("vh")))
        label, declared = self._quality(data, vi, width, height)

        play = PlayInfo(engine=self.id, engine_name=self.name, duration=duration)
        play.durl = [Durl(order=1, url=urls[0], size=size, length=length, backups=urls[1:])]
        play.is_dash = False
        play.support_formats = ["mp4"]
        play.accept_quality = [rank_from_resolution(width, height)]
        play.accept_desc = [label]
        play.declared_quality = declared
        play.phantom_quality = [q for q in declared if q not in play.accept_quality]
        play.note = self._note(vi, label, size, trial, preview, duration, len(urls),
                               self._route)
        play.raw = {"vid": vid, "quality": label, "trial": trial, "preview": preview,
                    "route": self._route}
        return play

    def _page_vid(self, ctx, ref: Ref, page: int) -> str:
        page = max(1, int(page or 1))
        if page == 1:
            return ref.ident
        cid = ref.extra.get("cid") or ""
        if not cid:
            raise SiteUnavailable(f"这个腾讯视频不是剧集，没有第 {page} 集")
        try:
            vids = self._episode_vids(self._cover(ctx, cid), ref.ident)
        except SiteUnavailable as exc:
            raise SiteUnavailable(f"取分集列表失败：{exc}") from None
        if len(vids) < page:
            raise SiteUnavailable(f"腾讯视频这个合集只有 {len(vids)} 集，没有第 {page} 集")
        return vids[page - 1]

    @staticmethod
    def _upgrade_names(data: dict, vi: dict) -> list:
        """接口声称支持的档位里比当前这条更高的，最多再试两个。"""
        current = _number(vi.get("br"))
        formats = [f for f in ((data.get("fl") or {}).get("fi") or []) if f.get("name")]
        formats.sort(key=lambda f: _number(f.get("bandwidth")), reverse=True)
        names = [str(f["name"]) for f in formats if _number(f.get("br")) > current]
        return names[:2]

    @staticmethod
    def _urls(vi: dict) -> list:
        """根地址 + 文件名 + vkey，第一个之外的都算备用 CDN。"""
        fvkey = str(vi.get("fvkey") or "")
        name = str(vi.get("fn") or "")
        if not fvkey or not name:
            return []
        urls: list[str] = []
        for item in ((vi.get("ul") or {}).get("ui") or []):
            base = str(item.get("url") or "")
            if base:
                one = f"{base}{name}?vkey={fvkey}"
                if one not in urls:
                    urls.append(one)
        return urls

    @staticmethod
    def _quality(data: dict, vi: dict, width: int, height: int) -> tuple[str, list]:
        current = _number(vi.get("br"))
        formats = ((data.get("fl") or {}).get("fi")) or []
        picked = None
        for item in formats:
            if _number(item.get("br")) == current:
                picked = item
                break
        if picked:
            short = str(picked.get("sname") or picked.get("name") or "").strip()
            resolution = str(picked.get("resolution") or "").strip()
            label = " ".join(x for x in (short, resolution) if x) or (f"{height}P" if height else "自动")
            width = int(_number(picked.get("width"))) or width
            height = int(_number(picked.get("height"))) or height
        else:
            label = f"{height}P" if height else "自动"
        declared = sorted({rank_from_resolution(int(_number(f.get("width"))),
                                                int(_number(f.get("height"))))
                           for f in formats if f.get("width")}, reverse=True)
        if not declared and height:
            declared = [rank_from_resolution(width, height)]
        return label, declared

    @staticmethod
    def _note(vi: dict, label: str, size: int, trial: bool, preview: int,
              duration: int, cdn: int, route: str = "") -> str:
        parts = [
            f"画质来自腾讯视频官方 getinfo 接口（匿名身份，通路：{route or '未知'}）：{label}，"
            f"{vi.get('vw')}x{vi.get('vh')}，{_human(size)}",
            f"MP4 直链，另有 {cdn - 1} 个 CDN 备用地址" if cdn > 1 else "MP4 直链",
        ]
        if trial:
            parts.append(
                f"当前身份只有试看：接口给 {preview} 秒，正片约 {max(1, int(duration / 60))} 分钟，"
                "完整内容需要登录并开通腾讯视频会员"
            )
        else:
            parts.append("当前身份能拿到完整文件")
        parts.append("地址带 vkey 凭证且有有效期，拿到后尽快下载")
        return "；".join(parts)

    @staticmethod
    def _cast(nam) -> str:
        if isinstance(nam, list) and nam and isinstance(nam[0], list):
            return "、".join(str(x) for x in nam[0][:8])
        return ""
