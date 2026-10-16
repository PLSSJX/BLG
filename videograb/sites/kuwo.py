"""酷我音乐适配器。

实测（2026-10，无 Cookie）：
  * 元数据有两个来源，都免登录：
      1. 单曲页 https://www.kuwo.cn/play_detail/{rid} 是服务端直出（Nuxt SSR），
         页面里的 window.__NUXT__ 带完整 songinfo：name / artist / duration /
         pic / hasLossless / payInfo，字符串解析即可（不发第二条请求拿详情）。
      2. http://m.kuwo.cn/newh5/singles/songinfoandlrc?musicId={rid} 额外给出
         formats（这首歌到底有哪些版本，如 MP3128|MP3H|ALFLAC），
         但它偶发 status=301「音乐查询失败」，要重试几次。
  * 播放地址走 http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_{rid}
    &format=mp3&response=url：免登录直接返回一条整曲 128K mp3 直链
    （实测 3.01 MB / 188 秒，≈128 kbps）。
  * 加 br=320kmp3 只会拿到十几秒的试听片段（实测 181 KB），所以这里不带 br。
  * 需要 csrf 头的 https://www.kuwo.cn/api/v1/www/music/playUrl 对匿名请求一律回
    "The request is illegal!"：酷我新版已不再给匿名访客下发 kw_token，
    只有填自己账号的 Cookie（里面带 kw_token）才可能问到 320K / 无损。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info, single_file_play

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HOME = "https://www.kuwo.cn"
_REFERER = _HOME + "/"
_PAGE_URL = _HOME + "/play_detail/"
_PLAY_API = "http://antiserver.kuwo.cn/anti.s"
_SONGINFO_API = "http://m.kuwo.cn/newh5/singles/songinfoandlrc"
_VIP_PLAY_API = _HOME + "/api/v1/www/music/playUrl"

# qn -> (界面文案, 落盘扩展名)
_TIERS = {
    1: ("标准 128K mp3", "mp3"),
    2: ("高品 320K mp3", "mp3"),
    3: ("无损 FLAC", "flac"),
    4: ("Hi-Res 母带 FLAC", "flac"),
}

_RID_PATTERNS = (
    re.compile(r"/play_detail/(\d+)", re.I),
    re.compile(r"[?&]musicId=(\d+)", re.I),
    re.compile(r"[?&]rid=(\d+)", re.I),
    re.compile(r"MUSIC_(\d+)", re.I),
    re.compile(r"/(\d{4,12})(?:\.html)?(?:[?#]|$)", re.I),
)
_DIGITS = re.compile(r"^\d{4,12}$")


def _ua(ctx) -> str:
    return (getattr(ctx.settings, "user_agent", "") or "").strip() or _DESKTOP_UA


def _headers(ctx, cookie: str = "", json_want: bool = True) -> dict:
    headers = {
        "User-Agent": _ua(ctx),
        "Accept": "application/json, text/plain, */*" if json_want else
                  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": _REFERER,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _cookie(ctx) -> str:
    return (getattr(ctx.settings, "kuwo_cookie", "") or "").strip()


def _token_of(cookie: str) -> str:
    """从 Cookie 串里取 kw_token；用户只粘了一个 token 值时也认。"""
    if not cookie:
        return ""
    found = re.search(r"(?:^|;\s*)kw_token=([^;]+)", cookie)
    if found:
        return found.group(1).strip()
    if "=" not in cookie and ";" not in cookie:
        return cookie.strip()
    return ""


def _host_of(text: str) -> str:
    raw = (text or "").strip()
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        return (urllib.parse.urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _is_kuwo(host: str) -> bool:
    return bool(host) and (host == "kuwo.cn" or host.endswith(".kuwo.cn"))


# ---------- window.__NUXT__ 里的 JS 字面量解析（不 eval，纯字符串） ----------
def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _js_string(text: str, i: int):
    start = i
    i += 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            i += 1
            break
        i += 1
    raw = text[start:i]
    try:
        return json.loads(raw), i
    except Exception:
        return raw.strip('"'), i


def _js_parse(text: str, i: int, env: dict):
    """解析一小段 JS 字面量：对象 / 数组 / 字符串 / 数字 / 标识符。"""
    n = len(text)
    i = _skip_ws(text, i)
    if i >= n:
        raise ValueError("JS 字面量提前结束")
    ch = text[i]
    if ch == "{":
        obj = {}
        i += 1
        while True:
            i = _skip_ws(text, i)
            if i < n and text[i] == "}":
                return obj, i + 1
            if i >= n:
                raise ValueError("对象没有闭合")
            if text[i] == '"':
                key, i = _js_string(text, i)
            else:
                found = re.match(r"[A-Za-z_$][\w$]*", text[i:])
                if not found:
                    raise ValueError("键名无法解析")
                key = found.group(0)
                i += len(key)
            i = _skip_ws(text, i)
            if i < n and text[i] == ":":
                i += 1
            value, i = _js_parse(text, i, env)
            obj[key] = value
            i = _skip_ws(text, i)
            if i < n and text[i] == ",":
                i += 1
                continue
            if i < n and text[i] == "}":
                return obj, i + 1
            raise ValueError("对象里出现意外字符")
    if ch == "[":
        arr = []
        i += 1
        while True:
            i = _skip_ws(text, i)
            if i < n and text[i] == "]":
                return arr, i + 1
            if i >= n:
                raise ValueError("数组没有闭合")
            value, i = _js_parse(text, i, env)
            arr.append(value)
            i = _skip_ws(text, i)
            if i < n and text[i] == ",":
                i += 1
                continue
            if i < n and text[i] == "]":
                return arr, i + 1
            raise ValueError("数组里出现意外字符")
    if ch == '"':
        return _js_string(text, i)
    found = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text[i:])
    if found:
        raw = found.group(0)
        number = float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw)
        return number, i + len(raw)
    found = re.match(r"[A-Za-z_$][\w$]*", text[i:])
    if found:
        name = found.group(0)
        if name == "true":
            return True, i + 4
        if name == "false":
            return False, i + 5
        if name in ("null", "undefined"):
            return None, i + len(name)
        return env.get(name), i + len(name)
    raise ValueError("无法解析的 JS 字面量：" + text[i:i + 24])


def _nuxt_songinfo(html: str) -> dict:
    """从单曲页的 window.__NUXT__ 里取出 songinfo 字典。"""
    pos = html.find("__NUXT__=")
    if pos < 0:
        return {}
    end = html.find("</script>", pos)
    seg = html[pos:end if end > 0 else pos + 40000]
    head = re.match(r"__NUXT__=\(function\(([^)]*)\)\{", seg)
    if not head:
        return {}
    params = [p.strip() for p in head.group(1).split(",") if p.strip()]
    body = seg[head.end():]
    tail = re.search(r"\}\((.*)\)\)\s*;?\s*$", body, re.S)
    if not tail:
        return {}
    args, i = [], 0
    arg_text = tail.group(1)
    while i < len(arg_text):
        i = _skip_ws(arg_text, i)
        if i >= len(arg_text):
            break
        if arg_text[i] == ",":
            i += 1
            continue
        value, i = _js_parse(arg_text, i, {})
        args.append(value)
    env = dict(zip(params, args))
    obj_text = body[:tail.start()].strip()
    if obj_text.startswith("return"):
        obj_text = obj_text[len("return"):]
    start = obj_text.find("{")
    if start < 0:
        return {}
    obj, _ = _js_parse(obj_text, start, env)
    data = (obj or {}).get("data") or []
    for block in data:
        if isinstance(block, dict) and isinstance(block.get("songinfo"), dict):
            return block["songinfo"]
    return {}


def _regex_songinfo(html: str) -> dict:
    """__NUXT__ 解析失败时的兜底：从直出 HTML 里抠几个字段。"""
    out = {}
    found = re.search(r'<input id="songinfo-name"[^>]*value="([^"]*)"', html)
    if found:
        out["name"] = found.group(1).strip()
    found = re.search(r'class="artist_name[^"]*"[^>]*>\s*<span class="name"[^>]*>([^<]*)<', html)
    if found:
        out["artist"] = found.group(1).strip()
    found = re.search(r'songTimeMinutes:"(\d+):(\d+)"', html)
    if found:
        out["duration"] = int(found.group(1)) * 60 + int(found.group(2))
    return out


def _standard_label(song: dict) -> str:
    """酷我有些歌只有低码率版本（formats 里没有 MP3128），标签要如实写。"""
    formats = str(song.get("formats") or "").upper()
    if not formats or "MP3128" in formats or "MP3H" in formats:
        return _TIERS[1][0]
    for token, bitrate in (("MP3192", 192), ("MP396", 96), ("MP364", 64), ("MP348", 48)):
        if token in formats:
            return f"标准 {bitrate}K mp3（酷我这首歌只有低码率版本）"
    return _TIERS[1][0]


def _tiers_of(song: dict) -> set:
    """这首歌到底有哪些版本 —— 只列平台自己说有、或者我们真拿到的档。"""
    formats = str(song.get("formats") or "").upper()
    tiers = {1}
    if "MP3H" in formats or "MP3320" in formats or "320" in str(song.get("minfo") or ""):
        tiers.add(2)
    if "ALFLAC" in formats or "HIRFLAC" in formats or "FLAC" in formats or song.get("has_lossless"):
        tiers.add(3)
    if "HIRFLAC" in formats or "HIRES" in formats:
        tiers.add(4)
    return tiers


class KuwoSite(SiteAdapter):
    id = "kuwo"
    name = "酷我音乐"
    media = "music"
    home = _HOME
    color = "#FFD200"
    examples = [
        "https://www.kuwo.cn/play_detail/303316117",
        "https://m.kuwo.cn/newh5app/play_detail/303316117",
        "303316117（酷我歌曲 rid）",
    ]
    note = ("免登录只有标准 128K mp3（anti.s 整曲直链）；320K 与无损要填带 kw_token 的账号 Cookie，"
            "酷我新版已不给匿名访客下发 kw_token。")
    needs_login = False
    credentials = [
        {
            "key": "cookie",
            "field": "kuwo_cookie",
            "label": "酷我 Cookie（含 kw_token）",
            "secret": True,
            "purpose": "酷我的 www 接口要 csrf 头，而 csrf 的取值就是 Cookie 里的 kw_token；"
                       "匿名访客拿不到它，因此只有填自己账号的 Cookie 才可能问到 320K / 无损。",
            "howto": [
                "用浏览器打开 <code>https://www.kuwo.cn/</code> 并<strong>登录</strong>账号",
                "按 <code>F12</code> 打开开发者工具（或右键页面 → 检查）",
                "切换到 <strong>Application</strong>（应用）标签页 → 左侧 <strong>Cookies</strong> → <code>https://www.kuwo.cn</code>",
                "找到 <code>kw_token</code> 那一行，复制它的 Value",
                "把 <code>kw_token=你复制的值</code> 整串粘到下面的输入框（也可以整串粘浏览器里的 Cookie）",
                "保存后重试；只有会员账号才能真正拿到 320K / 无损",
            ],
            "tip": "在开发者工具的 Console 里执行 <code>document.cookie</code> 就能看到全部 Cookie，直接整串复制即可。",
            "warning": "Cookie 等同于你的登录凭证，只保存在本机的 data/config.json，请不要分享给别人；"
                       "在酷我退出登录后它会立即失效。",
        },
    ]

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if _DIGITS.fullmatch(raw):
            return True
        return "kuwo.cn" in raw.lower()

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请输入酷我音乐的单曲链接或歌曲 rid")
        ref = Ref(raw=raw, site=self.id, kind="music")
        if _DIGITS.fullmatch(raw):
            ref.ident = raw
            return ref
        if "kuwo.cn" in raw.lower() or "MUSIC_" in raw.upper():
            for pattern in _RID_PATTERNS:
                found = pattern.search(raw)
                if found:
                    ref.ident = found.group(1)
                    return ref
            raise ValueError("这个酷我链接里没有歌曲 rid：" + raw[:80])
        raise ValueError("无法从输入中识别酷我歌曲：" + raw[:80])

    def normalize(self, ctx, ref: Ref) -> Ref:
        return ref

    # ---------- 接口 ----------
    def _song(self, ctx, ref: Ref) -> dict:
        """详情：SSR 单曲页 + 移动接口（formats），结果缓存进 ref.extra。"""
        cached = ref.extra.get("_song")
        if isinstance(cached, dict):
            return cached
        rid = (ref.ident or "").strip()
        if not rid.isdigit():
            raise SiteUnavailable("酷我歌曲 rid 无效：" + (ref.ident or "(空)"))
        song: dict = {"rid": rid}
        errors = []

        try:
            html = ctx.net.get_text(f"{_PAGE_URL}{rid}", headers=_headers(ctx, _cookie(ctx), json_want=False),
                                    referer=_REFERER, timeout=20, retries=2)
            page = _nuxt_songinfo(html) or _regex_songinfo(html)
            if page:
                song.update({
                    "name": (page.get("name") or "").strip(),
                    "artist": (page.get("artist") or "").strip(),
                    "album": (page.get("album") or "").strip(),
                    "duration": int(page.get("duration") or 0),
                    "cover": page.get("pic") or page.get("pic120") or page.get("albumpic") or "",
                    "has_lossless": bool(page.get("hasLossless")),
                    "listen_fee": bool(page.get("isListenFee")),
                })
            else:
                errors.append("单曲页没有内嵌数据")
        except Exception as exc:
            errors.append(f"单曲页请求失败：{exc}")

        # 移动接口给 formats（这首歌有哪些版本），偶发失败要重试
        for attempt in range(3):
            try:
                data = ctx.net.get_json(f"{_SONGINFO_API}?musicId={rid}",
                                        headers=_headers(ctx, _cookie(ctx)),
                                        referer="http://m.kuwo.cn/", timeout=20, retries=2)
            except Exception as exc:
                errors.append(f"移动接口请求失败：{exc}")
                break
            info = (data.get("data") or {}).get("songinfo") if isinstance(data, dict) else None
            if isinstance(info, dict) and info:
                if info.get("formats"):
                    song["formats"] = info.get("formats") or ""
                if info.get("coopFormats"):
                    song["coop_formats"] = info.get("coopFormats") or []
                if not song.get("name"):
                    song["name"] = (info.get("songName") or "").strip()
                if not song.get("artist"):
                    song["artist"] = (info.get("artist") or "").strip()
                if not song.get("album"):
                    song["album"] = (info.get("album") or "").strip()
                if not song.get("duration"):
                    raw_duration = str(info.get("duration") or "0")
                    song["duration"] = int(raw_duration) if raw_duration.isdigit() else 0
                song["free"] = str(info.get("pay") or "") in ("", "0")
                break
            time.sleep(0.4)

        if not song.get("name"):
            reason = "；".join(errors) or "接口没有返回歌曲信息"
            raise SiteUnavailable(f"酷我取不到这首歌（{reason}），可能 rid 不对或歌曲已下架")
        if song.get("duration"):
            song["duration"] = int(song["duration"])
        ref.extra["_song"] = song
        return song

    def _free_stream(self, ctx, rid: str, duration: int, label: str = ""):
        """免登录路径：anti.s 直接给整曲 128K mp3 直链。"""
        url = f"{_PLAY_API}?type=convert_url&rid=MUSIC_{rid}&format=mp3&response=url"
        try:
            text = ctx.net.get_text(url, headers=_headers(ctx, _cookie(ctx)),
                                    referer=_REFERER, timeout=20, retries=3).strip()
        except Exception as exc:
            raise SiteUnavailable(f"酷我取流接口请求失败：{exc}") from None
        if not text.lower().startswith("http"):
            return None, (text[:120] or "接口没有返回地址")
        size = 0
        try:
            size = ctx.net.content_length(text, referer=_REFERER) or 0
        except Exception:
            size = 0
        # 酷我对部分资源只放试听片段（实测 188 秒的歌只给 181 KB ≈ 11 秒），
        # 必须如实识别，不能当整曲交出去。阈值取 4 KB/s（≈32 kbps），
        # 免得把本来就只有 48K 的低码率整曲误判成片段。
        if duration > 30 and size and size < duration * 4000:
            raise SiteUnavailable(
                f"酷我只给出试听片段（{size / 1024:.0f} KB，约 {size // 16000} 秒，"
                f"整曲约 {duration} 秒），完整版需要会员或购买。")
        return {"url": text, "size": size, "ext": "mp3", "qn": 1, "route": "anti.s",
                "label": label or _TIERS[1][0]}, ""

    def _vip_stream(self, ctx, rid: str, cookie: str):
        """带 Cookie 才可能走通的 www 接口；拿到什么档就报什么档。"""
        token = _token_of(cookie)
        if not token:
            return None, "Cookie 里没有 kw_token"
        headers = _headers(ctx, cookie)
        headers["csrf"] = token
        try:
            data = ctx.net.get_json(f"{_VIP_PLAY_API}?mid={rid}&type=music&httpsStatus=1",
                                    headers=headers, referer=_REFERER, timeout=20, retries=2)
        except Exception as exc:
            return None, f"www 取流接口请求失败：{exc}"
        if not isinstance(data, dict) or not data.get("success"):
            message = (data or {}).get("message") or (data or {}).get("msg") or "接口拒绝"
            return None, str(message)[:80]
        item = data.get("data") or {}
        url = (item.get("url") or "").strip()
        if not url:
            return None, "接口没有返回地址"
        br = str(item.get("br") or "").lower()
        ext = "flac" if ("flac" in br or url.lower().endswith(".flac")) else "mp3"
        if ext == "flac":
            qn = 4 if ("2000" in br or "hires" in br or "hir" in br) else 3
        else:
            qn = 2 if ("320" in br or "320" in url.lower()) else 1
        size = 0
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return {"url": url, "size": size, "ext": ext, "qn": qn, "route": "www-api",
                "bitrate": br}, ""

    # ---------- 取内容 ----------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        ref = self.normalize(ctx, ref)
        song = self._song(ctx, ref)
        duration = int(song.get("duration") or 0)
        title = (song.get("name") or ref.ident or "").strip()
        artist = (song.get("artist") or "").replace("&nbsp;", " ").strip()
        album = (song.get("album") or "").replace("&nbsp;", " ").strip()
        cover = song.get("cover") or ""
        if cover.startswith("http://"):
            cover = "https://" + cover[7:]
        vinfo = make_info(
            ref,
            title=title or ("酷我 " + (ref.ident or "")),
            cover=cover,
            duration=duration,
            album=album,
            owner=Owner(name=artist),
            desc=f"歌手：{artist or '-'}　专辑：{album or '-'}　时长：{duration} 秒",
            media="music",
            kind="music",
        )
        vinfo.engine = self.id
        vinfo.pages = [Page(page=1, part=title, duration=duration)]
        vinfo.extra = {
            "rid": ref.ident,
            "formats": song.get("formats") or "",
            "has_lossless": bool(song.get("has_lossless")),
            "levels": sorted(_tiers_of(song)),
        }
        return vinfo

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        ref = self.normalize(ctx, ref)
        song = self._song(ctx, ref)
        rid = (ref.ident or "").strip()
        duration = int(song.get("duration") or 0)
        want = qn if qn in _TIERS else 0
        cookie = _cookie(ctx)
        advertised = _tiers_of(song)

        got: dict = {}
        reasons = []
        item, why = self._free_stream(ctx, rid, duration, _standard_label(song))
        if item:
            got.setdefault(item["qn"], item)
        elif why:
            reasons.append(why)

        if cookie:
            vip, why = self._vip_stream(ctx, rid, cookie)
            if vip:
                got.setdefault(vip["qn"], vip)
            elif why:
                reasons.append("带 Cookie 也没问到更高档：" + why)

        if not got:
            raise SiteUnavailable(
                "酷我没有下发可用的播放地址（" + ("；".join(reasons) or "接口没有返回地址") + "）。"
                "多半是版权下架、地区限制，或者这首只有会员能听。")

        if want and want not in got:
            have = "、".join(_TIERS[q][0] for q in sorted(got))
            raise SiteUnavailable(
                f"酷我没有下发「{_TIERS[want][0]}」，当前身份实际只能拿到：{have}。"
                + ("320K / 无损需要带 kw_token 的会员 Cookie。" if want >= 2 else ""))

        chosen = want or max(got)
        item = got[chosen]
        label = item.get("label") or _TIERS[chosen][0]
        play = single_file_play(self, item["url"], item["size"], duration,
                                quality=label, ext=item["ext"], referer=_REFERER)
        play.media = "music"
        play.ext = item["ext"]
        play.duration = duration
        play.accept_quality = sorted(got)
        play.accept_desc = [got[q].get("label") or _TIERS[q][0] for q in sorted(got)]
        play.phantom_quality = sorted(q for q in advertised if q not in got)

        size_text = f"{item['size'] / 1048576:.2f} MB" if item.get("size") else "接口未给大小"
        notes = []
        if item.get("route") == "anti.s":
            notes.append("整曲直链由 antiserver 的 anti.s 接口下发（免登录档）")
            if "低码率" in label:
                notes.append("酷我返回的 formats=" + (song.get("formats") or "-")
                             + "，这首歌没有 128K 版本")
        else:
            notes.append(f"{_TIERS[chosen][0]} 来自酷我 www 接口（接口自报 br={item.get('bitrate') or '未知'}）")
        notes.append("实测 " + size_text)
        if play.phantom_quality:
            notes.append("这首歌存在 " + "、".join(_TIERS[q][0] for q in play.phantom_quality)
                         + " 版本，但当前身份没拿到")
        if not cookie:
            notes.append("免登录只有 128K mp3；320K / 无损要填带 kw_token 的酷我 Cookie")
        if reasons:
            notes.append("；".join(reasons))
        notes.append("直链带时效签名，过期重新取流即可")
        play.note = "；".join(notes)
        play.raw = {"rid": rid, "route": item.get("route"), "size": item.get("size"),
                    "formats": song.get("formats") or ""}
        return play
