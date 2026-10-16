"""小红书适配器。

实测（2026-10）可用的抓取路径：
  1. xhslink.com 短链 307 跳转，Location 里就是带 xsec_token 的笔记长链（短链有效期很短）。
  2. www.xiaohongshu.com/explore/{id}：桌面 UA 下服务端只回登录/404 壳
     （__INITIAL_STATE__ 里 noteDetailMap 是空的），必须带 App 分享链接里的 xsec_token
     才可能直出笔记数据；没 token 时这一路走不通。
  3. 兜底走小红书国际版 RedNote 的移动端页面：
     https://www.rednote.com/explore/{id}（会自动跳到 /discovery/item/{id}），
     只认移动端 Safari UA，页面 __INITIAL_STATE__.noteData.data.noteData 里
     有完整的笔记 JSON（title/desc/user/interactInfo/video.media.stream）。
     RedNote 与小红书共用同一套 24 位笔记 ID，CDN 不校验 Referer。

取不到时（笔记已删、仅国内可见、图文笔记没有视频）如实抛 SiteUnavailable。
"""
from __future__ import annotations

import json
import re
import urllib.parse

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info, single_file_play

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# RedNote 移动端页面只对手机浏览器 UA 直出数据（桌面 UA 会被踢到登录页）
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

_XHS_REFERER = "https://www.xiaohongshu.com/"
_REDNOTE_REFERER = "https://www.rednote.com/"
# 社交爬虫 UA：RedNote 会给这类客户端直出 OG 元数据，og:video 就是完整成品 mp4
_OG_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
_XHS_EXPLORE = "https://www.xiaohongshu.com/explore/{ident}"
_REDNOTE_EXPLORE = "https://www.rednote.com/explore/{ident}"
_REDNOTE_ITEM = "https://www.rednote.com/discovery/item/{ident}"

_SHORT_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:xhslink\.com|xiaohongshu\.com)/[A-Za-z0-9_\-./?=&%]+", re.I)
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|note|board)/([0-9a-fA-F]{24})")
_NOTE_ID_ALT = (
    re.compile(r"note_?[Ii]d=([0-9a-fA-F]{24})"),
    re.compile(r"\b([0-9a-f]{24})\b"),
)


def _headers(ctx, mobile: bool = False) -> dict:
    if mobile:
        ua = _MOBILE_UA
        referer = _REDNOTE_REFERER
    else:
        ua = (getattr(ctx.settings, "user_agent", "") or "").strip() or _DESKTOP_UA
        referer = _XHS_REFERER
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
    }


def _note_id(text: str) -> str:
    m = _NOTE_ID_RE.search(text or "")
    if m:
        return m.group(1).lower()
    for pat in _NOTE_ID_ALT:
        m = pat.search(text or "")
        if m:
            return m.group(1).lower()
    return ""


def _initial_state(html: str):
    """把 window.__INITIAL_STATE__ 抠出来；里面的 undefined 要换成 null。"""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return json.loads(re.sub(r"\bundefined\b", "null", raw))
    except Exception:
        return None


def _get(node, *names):
    if not isinstance(node, dict):
        return None
    for name in names:
        value = node.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _streams(video) -> list:
    """从 note.video 里抽出所有可直接下载的成品流（含音轨的 mp4）。"""
    media = _get(video, "media")
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except Exception:
            media = None
    if not isinstance(media, dict):
        raw = _get(video, "mediaV2", "media_v2")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, dict):
            media = _get(raw, "video") or raw
    if not isinstance(media, dict):
        return []
    streams = _get(media, "stream")
    if not isinstance(streams, dict):
        return []
    out = []
    for codec, items in streams.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = _get(item, "masterUrl", "master_url")
            if not url:
                continue
            backups = _get(item, "backupUrls", "backup_urls") or []
            width = int(_get(item, "width") or 0)
            height = int(_get(item, "height") or 0)
            out.append({
                "url": url,
                "backups": [u for u in backups if isinstance(u, str)][:3],
                "size": int(_get(item, "size") or 0),
                "width": width,
                "height": height,
                # 竖屏笔记的 height 是长边，画质档按短边叫才符合习惯
                "qh": min(width, height) if width and height else max(width, height),
                "codec": str(codec).lower(),
                "video_codec": str(_get(item, "videoCodec", "video_codec") or codec),
                "audio_codec": str(_get(item, "audioCodec", "audio_codec") or ""),
                "format": str(_get(item, "format") or "mp4"),
                "bitrate": int(_get(item, "avgBitrate", "avg_bitrate") or 0),
                "duration": int(_get(item, "duration") or 0),
            })
    return out


def _find_note(state):
    """兼容网页版 note.noteDetailMap 和移动版 noteData.data.noteData 两种结构。"""
    if not isinstance(state, dict):
        return None
    store = state.get("note")
    maps = []
    if isinstance(store, dict):
        maps.append(store.get("noteDetailMap"))
    maps.append(state.get("noteDetailMap"))
    for mapping in maps:
        if isinstance(mapping, dict):
            for value in mapping.values():
                if isinstance(value, dict) and isinstance(value.get("note"), dict):
                    return value["note"]
    note_data = state.get("noteData")
    if isinstance(note_data, dict):
        data = note_data.get("data")
        if isinstance(data, dict) and isinstance(data.get("noteData"), dict):
            return data["noteData"]
    # 兜底：深度找“带 video 的笔记对象”
    stack = [state]
    fallback = None
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get("video"), dict) and (_get(cur, "title", "desc") is not None):
                if _streams(cur["video"]):
                    return cur
                fallback = fallback or cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return fallback


def _meta(html: str, prop: str) -> str:
    """取 <meta property="..."> 的 content，属性前后顺序两种写法都认。"""
    for pattern in (
        r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop),
        r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % re.escape(prop),
    ):
        found = re.search(pattern, html, re.I)
        if found:
            return found.group(1).strip()
    return ""


def _absolute(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def _og_note(html: str, ident: str):
    """把 RedNote 给社交爬虫直出的 OG 元数据拼成和网页版同构的笔记对象。

    实测 og:video 就是完整成品 mp4（Range 请求回 206，总字节数与网页版直链一致），
    og:title / og:image 也齐全；但这条通路拿不到时长、作者和互动数，一律留空，
    界面上会看到时长为 0，这是通路的真实能力，不编造。
    """
    video = _absolute(_meta(html, "og:video"))
    title = _meta(html, "og:title")
    if title.endswith("- rednote"):
        title = title[:-len("- rednote")].strip()
    if title.endswith("- 小红书"):
        title = title[:-len("- 小红书")].strip()
    desc = _meta(html, "og:description")
    image = _absolute(_meta(html, "og:image"))
    if not (video or title or image):
        return None
    note = {
        "title": title,
        "desc": desc or title,
        "type": "video" if video else "normal",
        "interactInfo": {},
        "user": {},
        "imageList": [{"urlDefault": image}] if image else [],
        "noteId": ident,
        "_og_only": True,
        "_og_url": _meta(html, "og:url"),
    }
    if video:
        note["video"] = {
            "media": {"stream": {"h264": [{
                "masterUrl": video,
                "format": "mp4",
                "videoCodec": "h264",
                "audioCodec": "aac",
            }]}}
        }
    return note


def _cover_of(note) -> str:
    cover = _get(note, "cover")
    if isinstance(cover, str) and cover.startswith("http"):
        return cover
    if isinstance(cover, dict):
        url = _get(cover, "urlDefault", "url_default", "url")
        if isinstance(url, str):
            return url
    images = _get(note, "imageList", "image_list") or []
    if images and isinstance(images[0], dict):
        url = _get(images[0], "urlDefault", "url_default", "urlPre", "url_pre", "url")
        if isinstance(url, str):
            return url
    return ""


class XiaohongshuSite(SiteAdapter):
    id = "xiaohongshu"
    name = "小红书"
    home = "https://www.xiaohongshu.com"
    color = "#FF2442"
    examples = [
        "https://xhslink.com/a/xxxxxxx",
        "https://www.xiaohongshu.com/explore/xxxxxxxxxxxxxxxxxxxxxxxx",
        "https://www.xiaohongshu.com/discovery/item/xxxxxxxxxxxxxxxxxxxxxxxx?xsec_token=...",
    ]
    note = ("五条通路依次重试：网页版带 xsec_token → 网页版裸链 → RedNote 国际版移动页 "
            "→ RedNote 详情页直连 → RedNote 给社交爬虫直出的 OG 元数据；"
            "网页版没有 token 时必失败，实际可用的都在 RedNote 那三条，仅支持视频笔记。")

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        low = raw.lower()
        if "xiaohongshu.com" in low or "xhslink.com" in low or "rednote.com" in low:
            return True
        if "xsec_token=" in low:
            return True
        return bool(re.fullmatch(r"[0-9a-f]{24}", low))

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请输入小红书笔记链接或分享文本")
        ref = Ref(raw=raw, site=self.id, kind="video")

        token = re.search(r"xsec_token=([A-Za-z0-9_\-]+)", raw)
        if token:
            ref.extra["xsec_token"] = token.group(1)
        source = re.search(r"xsec_source=([A-Za-z0-9_\-]+)", raw)
        if source:
            ref.extra["xsec_source"] = source.group(1)

        if "xhslink.com" in raw.lower():
            m = _SHORT_RE.search(raw)
            url = m.group(0) if m else ""
            if url and not url.lower().startswith("http"):
                url = "https://" + url
            if url:
                ref.short_url = url
                ref.extra["short_url"] = url
            ident = _note_id(raw)
            if ident:
                ref.ident = ident
            else:
                ref.note = "小红书短链，待展开"
            return ref

        ident = _note_id(raw)
        if not ident:
            raise ValueError(f"无法从输入中识别小红书笔记 ID：{raw[:80]}")
        ref.ident = ident
        return ref

    def normalize(self, ctx, ref: Ref) -> Ref:
        if ref.ident:
            return ref
        url = ref.extra.get("short_url") or ref.short_url
        if not url:
            raise SiteUnavailable("输入里没有小红书短链，也没有笔记 ID")
        try:
            res = ctx.net.request(url, headers=_headers(ctx), referer=_XHS_REFERER,
                                  retries=2, timeout=20)
            final = res.url or ""
        except Exception as exc:
            raise SiteUnavailable(f"小红书短链展开失败（{exc}），请改用完整分享链接") from None
        ident = _note_id(final)
        if not ident:
            raise SiteUnavailable(
                "小红书短链没有跳到笔记页（短链有效期很短，常见就是已过期），"
                "请在 App 里重新分享一次再复制"
            )
        ref.ident = ident
        ref.extra["resolved_url"] = final
        token = re.search(r"xsec_token=([A-Za-z0-9_\-]+)", final)
        if token:
            ref.extra["xsec_token"] = token.group(1)
        return ref

    # ---------- 取内容 ----------
    @staticmethod
    def _page(ctx, url: str, mobile: bool = False, referer: str = "", ua: str = ""):
        """抓一个笔记页面，返回 HTML；请求失败抛出来让上层记成这条通路的原因。"""
        headers = _headers(ctx, mobile=mobile)
        if ua:
            headers["User-Agent"] = ua
        res = ctx.net.request(url, headers=headers,
                              referer=referer or (headers.get("Referer") or _XHS_REFERER),
                              retries=2, timeout=20)
        return res.text()

    @classmethod
    def _from_xhs(cls, ctx, url: str):
        state = _initial_state(cls._page(ctx, url, referer=_XHS_REFERER))
        return _find_note(state)

    @classmethod
    def _from_rednote(cls, ctx, ident: str):
        """国际版移动页 /explore：实测会 307 到 /discovery/item 后直出数据。"""
        url = _REDNOTE_EXPLORE.format(ident=ident)
        state = _initial_state(cls._page(ctx, url, mobile=True, referer=_REDNOTE_REFERER))
        return _find_note(state)

    @classmethod
    def _from_rednote_item(cls, ctx, ident: str):
        """国际版详情页直连（不依赖 /explore 的跳转）。实测 iPhone UA 下直出数据。"""
        url = _REDNOTE_ITEM.format(ident=ident)
        state = _initial_state(cls._page(ctx, url, mobile=True, referer=_REDNOTE_REFERER))
        return _find_note(state)

    @classmethod
    def _from_rednote_og(cls, ctx, ident: str):
        """最后一条：让 RedNote 用 OG 元数据回答，解析 <meta> 而不是页面内嵌 JSON。"""
        url = _REDNOTE_ITEM.format(ident=ident)
        html = cls._page(ctx, url, referer=_REDNOTE_REFERER, ua=_OG_UA)
        return _og_note(html, ident)

    def _detail_routes(self, ctx, ref: Ref, ident: str):
        """有序通路表：(名称, 取值函数)。调用方逐个试，失败原因拼起来报给用户。"""
        routes = []
        token = ref.extra.get("xsec_token")
        if token:
            source = ref.extra.get("xsec_source") or "pc_share"
            url = (_XHS_EXPLORE.format(ident=ident)
                   + f"?xsec_token={urllib.parse.quote(token)}&xsec_source={source}")
            routes.append(("xiaohongshu.com/explore + xsec_token",
                           lambda u=url: self._from_xhs(ctx, u)))
        routes.append(("xiaohongshu.com/explore（无 token）",
                       lambda: self._from_xhs(ctx, _XHS_EXPLORE.format(ident=ident))))
        routes.append(("rednote.com/explore（国际版移动页）",
                       lambda: self._from_rednote(ctx, ident)))
        routes.append(("rednote.com/discovery/item（国际版详情页直连）",
                       lambda: self._from_rednote_item(ctx, ident)))
        routes.append(("rednote.com OG 元数据（og:video）",
                       lambda: self._from_rednote_og(ctx, ident)))
        return routes

    def _note(self, ctx, ref: Ref):
        ident = (ref.ident or "").strip()
        if not ident:
            ref = self.normalize(ctx, ref)
            ident = ref.ident
        if not ident:
            raise SiteUnavailable("缺少小红书笔记 ID，请重新复制分享链接")

        reasons = []
        for name, load in self._detail_routes(ctx, ref, ident):
            try:
                note = load()
            except Exception as exc:        # 网络、风控、页面改版都算这条通路失败
                reasons.append(f"{name}：{str(exc)[:80]}")
                continue
            if note:
                ref.extra["detail_route"] = name
                return note
            reasons.append(f"{name}：没有拿到笔记数据")
        raise SiteUnavailable(
            f"小红书笔记 {ident} 所有通路都没取到数据（" + "；".join(reasons) + "）。"
            "网页版要求登录或带 xsec_token；国际版 RedNote 也没有这条笔记时，"
            "多半是已删除、仅国内可见或为图文笔记。"
            "可以试试在 App 里重新分享、把带 xsec_token 的完整链接粘进来。"
        )

    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        note = self._note(ctx, ref)
        video = note.get("video") if isinstance(note.get("video"), dict) else {}
        streams = _streams(video)
        note_type = str(_get(note, "type") or ("video" if streams else "normal")).lower()
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        interact = _get(note, "interactInfo", "interact_info") or {}
        images = _get(note, "imageList", "image_list") or []
        desc = str(_get(note, "desc") or "")
        title = str(_get(note, "title") or "").strip()
        if not title:
            title = desc.strip().splitlines()[0][:80] if desc.strip() else f"小红书笔记 {ref.ident}"
        duration = 0
        if streams:
            best_ms = max((s["duration"] or 0) for s in streams)
            duration = best_ms // 1000
        if not duration:
            capa = _get(video, "capa") or {}
            if isinstance(capa, dict):
                duration = int(_get(capa, "duration") or 0)
        if not duration:
            duration = int(_get(note, "videoDuration", "video_duration") or 0)
        pubdate = int(_get(note, "time", "lastUpdateTime", "last_update_time") or 0)
        if pubdate > 10 ** 12:
            pubdate //= 1000

        stat = {}
        for key, label in (("likedCount", "like"), ("collectedCount", "collect"),
                           ("commentCount", "comment"), ("shareCount", "share")):
            value = _get(interact, key, key[0].lower() + key[1:])
            if value is not None:
                try:
                    stat[label] = int(value)
                except (TypeError, ValueError):
                    pass

        info = make_info(
            ref,
            title=title,
            cover=_cover_of(note),
            desc=desc,
            duration=duration,
            pubdate=pubdate,
            tname="小红书",
            owner=Owner(mid=0, name=str(_get(user, "nickname", "nickName", "nick_name") or ""),
                        face=str(_get(user, "avatar", "image") or "")),
            pages=[Page(cid=0, page=1, part=title, duration=duration)],
            stat=stat,
            kind="video" if note_type == "video" else "image",
            extra={
                "note_id": ref.ident,
                "note_type": note_type,
                "user_id": str(_get(user, "userId", "user_id", "userIdStr") or ""),
                "images": [i for i in images[:20] if isinstance(i, dict)],
                "detail_route": ref.extra.get("detail_route", ""),
                "og_only": bool(note.get("_og_only")),
                "referer": _REDNOTE_REFERER,
            },
        )
        return info

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        note = self._note(ctx, ref)
        video = note.get("video") if isinstance(note.get("video"), dict) else {}
        streams = _streams(video)
        if not streams:
            note_type = str(_get(note, "type") or "normal").lower()
            if note_type != "video":
                raise SiteUnavailable(
                    "这条小红书笔记是图文笔记，没有视频可下载"
                    "（图片直链在 extra.images 里，本适配器只负责视频）"
                )
            raise SiteUnavailable(
                "小红书这条笔记没有下发视频直链（可能仍在转码、已删除或需要登录）"
            )

        h264 = [s for s in streams if "264" in s["codec"]]
        pool = h264 or streams
        pick = max(pool, key=lambda s: (s["size"] or s["width"] * s["height"], s["height"]))
        duration = (pick["duration"] or 0) // 1000
        if not duration:
            duration = int(_get(video, "capa") and (_get(video.get("capa"), "duration") or 0) or 0)
        label = f"{pick['qh']}P" if pick["qh"] else "原始画质"
        codec_tag = pick["video_codec"] or pick["codec"]

        play = single_file_play(
            self, pick["url"], size=pick["size"], duration=duration, quality=label,
            note=(f"来自{ref.extra.get('detail_route') or '小红书'}，"
                  f"{pick['width']}x{pick['height']} 成品单文件（{codec_tag} + "
                  f"{pick['audio_codec'] or 'aac'}），免合并；CDN 不校验 Referer。"),
        )
        play.declared_quality = sorted({s["qh"] for s in streams if s["qh"]}, reverse=True)
        play.accept_desc = [f"{h}P" for h in play.declared_quality] or [label]
        play.duration = duration
        if play.durl:
            durl = play.durl[0]
            durl.referer = _REDNOTE_REFERER
            durl.backups = [u for u in pick["backups"] if u != durl.url]
        return play
