"""抖音适配器。

短链 v.douyin.com 会 302 到 www.douyin.com/video/{id} 或
www.iesdouyin.com/share/video/{id}，从落地地址里取出 19 位作品 ID。

详情按下面的顺序逐条尝试，全部失败才报错（实测 2026-10）：
  1. douyin.com/shipin/{话题号}?modal_id={作品ID} —— 主路径。
     SEO 落地页服务端直出（React Server Components / pace），__pace_f.push 的
     字符串参数里带着这个作品的完整 JSON（desc / createTime / authorInfo /
     video.bitRateList…）。
  2. iesdouyin.com/shipin/{话题号}?modal_id={作品ID} —— 同一套落地页换到
     iesdouyin 这个域名（另一套网关），其中一套被限流时另一套往往还能出数据。
  3. douyin.com/hashtag/{话题号}?modal_id={作品ID} —— 话题页模板，同样是 RSC 直出。
     实测话题号只是占位，作品由 modal_id 决定（随便给个号也能出数据）。
  三条通路的落地页不同、域名不同，提取逻辑共用同一套 RSC 解析。

播放地址取 video.bitRateList 里 format=mp4 的成品档（H.264/H.265 + AAC 单文件，免合并）。
注意：douyinvod CDN 校验 Referer，必须是 https://www.douyin.com/，否则一律 403。

实测走不通、不要再花时间的路径：
  * www.douyin.com/video/{id}、/discover、/note、/search —— 一律回 JS 反爬壳（jsvmprt）。
  * /aweme/v1/web/aweme/detail/（douyin 与 iesdouyin 两个域名）—— 补上 ttwid/msToken
    Cookie 后依然 403，它要 a_bogus 签名。
  * 分享页 www.iesdouyin.com/share/video/{id}、m.douyin.com/share/video/{id}
    —— _ROUTER_DATA 里 video_layout / videoInfoRes 都是空壳。
  * App 接口 aweme.snssdk.com / api.amemv.com / api5-normal-c-lf.amemv.com 的
    /aweme/v1/aweme/detail/ —— HTTP 200 但响应体是空的。
  * 老接口 www.iesdouyin.com/web/api/v2/aweme/iteminfo/ —— HTTP 200 空响应体。
  * 蜘蛛 UA（Baiduspider 等）请求落地页 —— HTTP 503。

取不到时（作品已删/私密、地区限制、落地页没有缓存这条作品）如实抛 SiteUnavailable。
"""
from __future__ import annotations

import json
import re
import urllib.parse

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info, single_file_play

# 桌面 Chrome：抖音网页端就是这么发的，保持和其它平台一致
_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_REFERER = "https://www.douyin.com/"
# SEO 落地页的“话题”占位，实测随便给个值都行，作品由 modal_id 决定
_SEED_TOPICS = ("0", "1")
# 话题页(hashtag)的占位号，同样只是占位：实测连 hashtag/1 这种不存在的号也能直出 modal_id 的作品
_SEED_HASHTAGS = ("1577802263618561", "1")

# 详情通路表：(名称, URL 模板, 占位种子, Referer)
#   {ident} = 19 位作品 ID，{seed} = 该模板自己的占位参数，逐个种子重试。
# 三条落地页互不相同（douyin 视频聚合页 / iesdouyin 网关 / douyin 话题页），
# 解析共用同一套 RSC(__pace_f) 提取逻辑；第 1 条是实测主路径。
_DETAIL_ROUTES = (
    ("douyin.com/shipin?modal_id",
     "https://www.douyin.com/shipin/{seed}?modal_id={ident}", _SEED_TOPICS, _REFERER),
    ("iesdouyin.com/shipin?modal_id",
     "https://www.iesdouyin.com/shipin/{seed}?modal_id={ident}", _SEED_TOPICS,
     "https://www.iesdouyin.com/"),
    ("douyin.com/hashtag?modal_id",
     "https://www.douyin.com/hashtag/{seed}?modal_id={ident}", _SEED_HASHTAGS, _REFERER),
)

_SHORT_RE = re.compile(r"(?:https?://)?(?:v|v\.ies)\.douyin\.com/[A-Za-z0-9_\-]+", re.I)
_ID_PATTERNS = (
    re.compile(r"/(?:video|note|slides|shipin)/(\d{6,25})"),
    re.compile(r"/share/(?:video|note)/(\d{6,25})"),
    re.compile(r"modal_id=(\d{6,25})"),
    re.compile(r"item_ids?=(\d{6,25})"),
    re.compile(r"(?<!\d)(\d{19})(?!\d)"),
)
# B 站画质码 → 期望高度，用来响应 qn 参数（0 表示要最高）
_QN_HEIGHT = {120: 2160, 116: 1080, 112: 1080, 80: 1080, 74: 720, 64: 720, 32: 480, 16: 360}


def _headers(ctx) -> dict:
    ua = (getattr(ctx.settings, "user_agent", "") or "").strip() or _DESKTOP_UA
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": _REFERER,
    }


def _first_id(text: str) -> str:
    for pat in _ID_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1)
    return ""


def _flight_strings(html: str):
    """把 __pace_f.push([...]) 里的字符串参数逐个吐出来。"""
    for m in re.finditer(r"__pace_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            items = json.loads(m.group(1))
        except Exception:
            continue
        for item in items:
            if isinstance(item, str):
                yield item


def _innermost_object(text: str, pos: int):
    """返回包住 pos 的最内层 JSON 对象的 [start, end]。"""
    stack = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}":
            if not stack:
                continue
            start = stack.pop()
            if start < pos < i:
                return start, i
    return None


def _loads(chunk: str):
    try:
        return json.loads(chunk)
    except Exception:
        pass
    try:  # RSC 里未定义的值会写成 $undefined
        return json.loads(chunk.replace('"$undefined"', "null"))
    except Exception:
        return None


def _find_aweme(node, ident: str):
    stack = [node]
    best = None
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if str(cur.get("awemeId") or "") == ident and isinstance(cur.get("video"), dict):
                video = cur["video"]
                if video.get("playApi") or video.get("playAddr") or video.get("bitRateList"):
                    return cur
                best = best or cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return best


def extract_aweme(html: str, ident: str):
    """从落地页 HTML 里抠出目标作品的对象。"""
    pattern = '"awemeId":"%s"' % ident
    for text in _flight_strings(html):
        decoded = urllib.parse.unquote(text)
        start = 0
        while True:
            pos = decoded.find(pattern, start)
            if pos < 0:
                break
            span = _innermost_object(decoded, pos)
            if span:
                obj = _loads(decoded[span[0]:span[1] + 1])
                if obj is not None:
                    hit = _find_aweme(obj, ident)
                    if hit:
                        return hit
            start = pos + 1
    return None


class DouyinSite(SiteAdapter):
    id = "douyin"
    name = "抖音"
    home = "https://www.douyin.com"
    color = "#FE2C55"
    examples = [
        "https://v.douyin.com/xxxxxxx/",
        "https://www.douyin.com/video/7xxxxxxxxxxxxxxxxxx",
        "7xxxxxxxxxxxxxxxxxx（19 位作品 ID）",
        "复制打开抖音的整段分享文本",
    ]
    note = ("详情有三条通路依次重试：douyin 视频聚合页 → iesdouyin 同款落地页 → "
            "douyin 话题页，都是 SEO 页内嵌数据；下载必须带 Referer: "
            "https://www.douyin.com/，否则 CDN 回 403。私密/已删除作品取不到。")

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        low = raw.lower()
        if "douyin.com" in low or "iesdouyin.com" in low:
            return True
        return bool(re.fullmatch(r"\d{19}", raw))

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请输入抖音视频链接、分享文本或 19 位作品 ID")
        ref = Ref(raw=raw, site=self.id, kind="video")

        m = _SHORT_RE.search(raw)
        if m:
            url = m.group(0)
            if not url.lower().startswith("http"):
                url = "https://" + url
            ref.short_url = url
            ref.extra["short_url"] = url
            ref.note = "抖音短链，待展开"
            return ref

        ident = _first_id(raw)
        if not ident:
            raise ValueError(f"无法从输入中识别抖音作品 ID：{raw[:80]}")
        ref.ident = ident
        return ref

    def normalize(self, ctx, ref: Ref) -> Ref:
        if ref.ident:
            return ref
        url = ref.extra.get("short_url") or ref.short_url
        if not url:
            raise SiteUnavailable("输入里没有抖音短链，也没有作品 ID")
        try:
            res = ctx.net.request(url, headers=_headers(ctx), referer=_REFERER,
                                  retries=2, timeout=20)
            final = res.url or ""
        except Exception as exc:
            raise SiteUnavailable(f"抖音短链展开失败（{exc}），请改用完整分享链接") from None
        ident = _first_id(final)
        if not ident:
            raise SiteUnavailable("抖音短链没有跳到作品页（多半已过期），请重新复制分享链接")
        ref.ident = ident
        ref.extra["resolved_url"] = final
        return ref

    # ---------- 取内容 ----------
    def _aweme(self, ctx, ref: Ref) -> dict:
        ident = (ref.ident or "").strip()
        if not ident:
            ref = self.normalize(ctx, ref)
            ident = ref.ident
        if not ident:
            raise SiteUnavailable("缺少抖音作品 ID，请重新复制分享链接")
        reasons = []
        for name, template, seeds, referer in _DETAIL_ROUTES:
            last = ""
            for seed in seeds:
                url = template.format(seed=seed, ident=ident)
                try:
                    res = ctx.net.request(url, headers=_headers(ctx), referer=referer,
                                          retries=2, timeout=20)
                except Exception as exc:
                    last = f"HTTP 请求失败（{str(exc)[:80]}）"
                    continue
                if res.status != 200:
                    last = f"HTTP {res.status}"
                    continue
                aweme = extract_aweme(res.text(), ident)
                if aweme:
                    ref.extra["detail_route"] = name
                    return aweme
                last = "落地页里没有这条作品的内嵌数据"
            reasons.append(f"{name}：{last or '未知原因'}")
        raise SiteUnavailable(
            f"抖音取不到作品 {ident} 的数据（" + "；".join(reasons) + "）。"
            "常见原因：作品已删除/设为私密、地区限制、或该作品暂未被 SEO 落地页收录。"
        )

    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        aweme = self._aweme(ctx, ref)
        video = aweme.get("video") or {}
        author = aweme.get("authorInfo") or {}
        duration = int((video.get("duration") or 0) // 1000)
        title = (aweme.get("desc") or aweme.get("itemTitle") or "").strip()
        if not title:
            title = f"抖音视频 {ref.ident}"
        cover = ""
        for key in ("cover", "originCover", "dynamicCover"):
            if video.get(key):
                cover = video[key]
                break
        if not cover:
            covers = video.get("coverUrlList") or []
            if covers:
                cover = covers[0]
        images = (aweme.get("images") or [])
        stat = {}
        if author.get("followerCount") is not None:
            stat["follower"] = author.get("followerCount")
        if author.get("totalFavorited") is not None:
            stat["total_favorited"] = author.get("totalFavorited")
        info = make_info(
            ref,
            title=title,
            cover=cover,
            desc=aweme.get("desc") or "",
            duration=duration,
            pubdate=int(aweme.get("createTime") or 0),
            tname="抖音",
            owner=Owner(mid=int(author.get("uid") or 0) if str(author.get("uid") or "").isdigit() else 0,
                        name=author.get("nickname") or "",
                        face=author.get("avatarUri") or ""),
            pages=[Page(cid=int(ref.ident) if ref.ident.isdigit() else 0, page=1,
                        part=title, duration=duration)],
            stat=stat,
            kind="image" if images else "video",
            extra={
                "aweme_id": ref.ident,
                "detail_route": ref.extra.get("detail_route") or "",
                "sec_uid": author.get("secUid") or "",
                "video_uri": video.get("uri") or "",
                "width": video.get("width") or 0,
                "height": video.get("height") or 0,
                "images": images[:20],
                "referer": _REFERER,
            },
        )
        return info

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        aweme = self._aweme(ctx, ref)
        video = aweme.get("video") or {}
        duration = int((video.get("duration") or 0) // 1000)
        if not duration:
            duration = int((video.get("bitRateList") or [{}])[0].get("duration") or 0) // 1000

        candidates = []
        for item in (video.get("bitRateList") or []):
            if (item.get("format") or "").lower() != "mp4":
                continue  # dash 是分离流，这里只取成品
            url = item.get("playApi") or ""
            if not url:
                addrs = item.get("playAddr") or []
                url = (addrs[0] or {}).get("src") if addrs and isinstance(addrs[0], dict) else ""
            if not url:
                continue
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            candidates.append({
                "url": url,
                "height": height,
                "width": width,
                # 竖屏作品的 height 是长边，画质档按短边叫才符合习惯
                "qh": min(width, height) if width and height else max(width, height),
                "bitrate": int(item.get("bitRate") or 0),
                "size": int(item.get("dataSize") or 0),
                "gear": item.get("gearName") or "",
                "h265": bool(item.get("isH265")),
                "fps": item.get("fps") or 0,
            })
        if not candidates and video.get("playApi"):
            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
            candidates.append({
                "url": video["playApi"], "height": height, "width": width,
                "qh": min(width, height) if width and height else max(width, height),
                "bitrate": 0, "size": int(video.get("dataSize") or 0),
                "gear": "default", "h265": False, "fps": 0,
            })
        if not candidates:
            for addr in (video.get("playAddr") or []):
                if isinstance(addr, dict) and addr.get("src"):
                    width = int(video.get("width") or 0)
                    height = int(video.get("height") or 0)
                    candidates.append({
                        "url": addr["src"], "height": height, "width": width,
                        "qh": min(width, height) if width and height else max(width, height),
                        "bitrate": 0, "size": int(video.get("dataSize") or 0),
                        "gear": "playAddr", "h265": False, "fps": 0,
                    })
                    break
        if not candidates:
            raise SiteUnavailable(
                "抖音这条作品没有下发任何可下载的播放地址（可能已删除、需要登录或地区限制）"
            )

        candidates.sort(key=lambda c: (c["qh"], c["bitrate"], c["size"]), reverse=True)
        pick = candidates[0]
        target = _QN_HEIGHT.get(int(qn or 0), 0)
        if target:
            fit = [c for c in candidates if c["qh"] and c["qh"] <= target]
            if fit:
                pick = fit[0]
        codec_tag = "hevc" if pick["h265"] else "h264"
        label = f"{pick['qh']}P" if pick["qh"] else "原始画质"
        if target and pick["qh"] < target:
            label += "（该作品没有更高档）"

        play = single_file_play(
            self, pick["url"], size=pick["size"], duration=duration,
            quality=label,
            note=(f"来自抖音 SEO 落地页内嵌数据（通路：{ref.extra.get('detail_route') or '未知'}；"
                  f"{pick['gear'] or '默认档'}，{codec_tag}，"
                  f"{pick['width']}x{pick['height']}）。成品单文件含音轨，免合并；"
                  f"下载时必须带 Referer: {_REFERER}，否则 douyinvod CDN 回 403。"),
        )
        # 同作品其它档位只做展示，避免下载器把多档当成多段去拼
        play.declared_quality = sorted({c["qh"] for c in candidates if c["qh"]}, reverse=True)
        play.accept_desc = [f"{h}P" for h in play.declared_quality] or [label]
        play.duration = duration
        if play.durl:
            durl = play.durl[0]
            durl.referer = _REFERER          # 供下载器挑选 per-host Referer（tasks.py 目前写死 B 站）
            addrs = video.get("playAddr") or []
            backups = [a.get("src") for a in addrs if isinstance(a, dict) and a.get("src")]
            durl.backups = [u for u in backups if u != durl.url][:2]
        return play
