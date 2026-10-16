"""QQ音乐适配器。

实测（2026-10）：
  * 元数据免登录可用：u.y.qq.com/cgi-bin/musicu.fcg 的 music.pf_song_detail_svr /
    get_song_detail_yqq 会返回曲名、时长、专辑、歌手，以及 file.size_128mp3 /
    size_320mp3 / size_flac / size_hires 这张"这首歌有哪些版本"的尺寸表 ——
    它同时也是判断"虚标档位"的依据；
  * 播放地址免登录**能拿到标准 128K mp3**，但有个坑：CgiGetVkey 必须显式带 filename，
    且文件名是"{前缀}{media_mid}.{扩展名}"（media_mid 只写一遍）。不传 filename、
    或把 media_mid 写成两遍（M500{mid}{mid}.mp3），接口都只回 retcode=104009、
    purl 全空，看起来就像"必须登录"；
  * 免费歌曲（file.pay.pay_play=0）能拿到 M500（128K mp3）与 C400（96K m4a）；
    付费 / 会员歌曲（pay_play=1）连 128K 都不下发；320K（M800）、无损（F000）、
    Hi-Res（Q000）免登录一律不下发。实测热歌榜 20 首里只有 2 首 pay_play=0 的歌拿得到；
  * 直链 = sip[0] + purl（aqqmusic.tc.qq.com 与 dl.stream.qqmusic.qq.com 都认，
    http / https 都行），整首下完的字节数与 file.size_128mp3 完全一致；
  * 填了 qqmusic_cookie 后按账号身份（uin + qqmusic_key）请求，服务端才会按会员等级
    下发 320K / 无损 / Hi-Res。

输入：y.qq.com/n/ryqq/songDetail/{mid}、i.y.qq.com/v8/playsong.html?songmid={mid}、
老版 y.qq.com/n/yqq/song/{mid}.html、c6.y.qq.com 短链。
"""
from __future__ import annotations

import json
import random
import re
import urllib.parse

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info, single_file_play

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HOME = "https://y.qq.com"
_REFERER = _HOME + "/"
_MUSICU = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_CDN_BASE = "https://dl.stream.qqmusic.qq.com"
_COVER = "https://y.gtimg.cn/music/photo_new/T002R300x300M000{}.jpg"

_HOSTS = ("y.qq.com", "c.y.qq.com", "c6.y.qq.com", "u.y.qq.com", "qqmusic.qq.com")
_SHORT_HOSTS = ("c6.y.qq.com",)

# qn -> (文件名前缀, 落盘扩展名, 界面文案, 详情 file 里对应的尺寸字段)
_TIERS = {
    1: ("M500", "mp3", "标准 128K mp3", "size_128mp3"),
    2: ("M800", "mp3", "高品 320K mp3", "size_320mp3"),
    3: ("F000", "flac", "无损 FLAC", "size_flac"),
    4: ("Q000", "flac", "Hi-Res 母带 FLAC", "size_hires"),
}
# 文件名前缀 -> 档位。服务端会按账号权限换一个前缀回来，只有前缀能说明实际档位
_PREFIX_TIER = {"M500": 1, "M800": 2, "F000": 3,
                "Q000": 4, "Q001": 4, "Q002": 4, "Q003": 4}

_MID_RE = re.compile(r"[0-9A-Za-z]{14}")
_MID_IN_PATH = re.compile(r"/(?:songDetail|song)/([0-9A-Za-z]{14})")


def _ua(ctx) -> str:
    return (getattr(ctx.settings, "user_agent", "") or "").strip() or _DESKTOP_UA


def _headers(ctx, json_body: bool = False) -> dict:
    headers = {
        "User-Agent": _ua(ctx),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": _REFERER,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _cookie(ctx) -> str:
    return (getattr(ctx.settings, "qqmusic_cookie", "") or "").strip()


def _uin_from_cookie(cookie: str) -> str:
    """Cookie 里的 uin 常写成 o0123456789 / wxuin=…，vkey 接口只要数字。"""
    for name in ("uin", "wxuin", "luin", "p_uin"):
        m = re.search(r"(?:^|;\s*)" + name + r"=o?(\d{4,})", cookie or "")
        if m:
            return m.group(1)
    return ""


def _guid() -> str:
    return str(random.randint(10 ** 9, 10 ** 10 - 1))


def _host_of(text: str) -> str:
    raw = (text or "").strip()
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        return (urllib.parse.urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _known(host: str, names) -> bool:
    return any(host == h or host.endswith("." + h) for h in names)


def _mid_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    blob = (parsed.path or "") + "?" + (parsed.query or "") + "&" + (parsed.fragment or "")
    m = _MID_IN_PATH.search(parsed.path or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&#]songmid=([0-9A-Za-z]{14})", blob)
    if m:
        return m.group(1)
    m = re.search(r"[?&#](?:song|mid|id)=([0-9A-Za-z]{14})", blob)
    return m.group(1) if m else ""


class QQMusicSite(SiteAdapter):
    id = "qqmusic"
    name = "QQ音乐"
    media = "music"
    home = _HOME
    color = "#31C27C"
    examples = [
        "https://y.qq.com/n/ryqq/songDetail/003AucCh2TDjs0",
        "https://i.y.qq.com/v8/playsong.html?songmid=003AucCh2TDjs0",
        "https://y.qq.com/n/yqq/song/003AucCh2TDjs0.html",
        "c6.y.qq.com 分享短链",
    ]
    note = "免登录只有免费歌曲的 128K mp3；付费歌与 320K / 无损都要填 qqmusic_cookie"
    needs_login = False
    credentials = [
        {
            "key": "cookie",
            "field": "qqmusic_cookie",
            "label": "QQ音乐 Cookie（含 qqmusic_key）",
            "secret": True,
            "purpose": "解锁 320K mp3、无损 FLAC 与 Hi-Res。不填时只有免费歌曲能拿到 128K mp3，"
                       "付费 / 会员歌曲连 128K 都不会下发。",
            "howto": [
                "用浏览器打开 <code>y.qq.com</code> 并<strong>登录</strong>你的 QQ 或微信账号",
                "随便点开一首歌的播放页，确认能正常播放",
                "按 <code>F12</code> 打开开发者工具（或右键页面 → 检查）",
                "切换到 <strong>Application</strong>（应用 / 应用程序）标签页",
                "左侧展开 <strong>Cookies</strong>，点选 <code>https://y.qq.com</code>",
                "把整串 Cookie 复制过来（至少要有 <code>uin</code> 和 <code>qqmusic_key</code> 两项）",
            ],
            "tip": "在 Console 里执行 <code>document.cookie</code> 就能把整串 Cookie 打印出来，"
                   "直接整段粘贴即可。",
            "warning": "这串 Cookie 等同于你的登录凭证，只保存在本机的 data/config.json，"
                       "请不要分享给别人；在 QQ音乐退出登录后它会立即失效。",
        },
    ]

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        host = _host_of(raw)
        return bool(host) and _known(host, _HOSTS)

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请输入 QQ音乐的单曲链接")
        host = _host_of(raw)
        if not host or not _known(host, _HOSTS):
            raise ValueError("不是 QQ音乐的链接：" + raw[:60])
        url = raw if "://" in raw else "https://" + raw.lstrip("/")
        parsed = urllib.parse.urlparse(url)
        path = (parsed.path or "") + (parsed.fragment or "")

        if host == "c6.y.qq.com" and "/base/fcgi-bin/u" in path:
            # 分享短链里没有 mid，交给 normalize 联网展开
            return Ref(raw=raw, site=self.id, kind="music", ident="",
                       extra={"short_url": url})

        mid = _mid_from_url(url)
        if not mid:
            if any(k in path for k in ("albumDetail", "playlist", "singer", "toplist", "mv")):
                raise ValueError("QQ音乐只支持单曲链接：专辑 / 歌单 / 歌手页请先打开具体一首歌")
            raise ValueError("这个 QQ音乐链接里没有歌曲 mid：" + raw[:60])
        return Ref(raw=raw, site=self.id, kind="music", ident=mid, extra={"song_mid": mid})

    def normalize(self, ctx, ref: Ref) -> Ref:
        """c6.y.qq.com 分享短链要跟一次跳转才拿得到 mid。"""
        if ref.ident or not ref.extra.get("short_url"):
            return ref
        url = ref.extra["short_url"]
        try:
            res = ctx.net.request(url, headers=_headers(ctx), referer=_REFERER,
                                  timeout=20, retries=3)
        except Exception as exc:
            raise SiteUnavailable(f"QQ音乐短链展开失败：{exc}") from None
        mid = _mid_from_url(res.url)
        if not mid:
            raise SiteUnavailable(f"QQ音乐短链没有跳到歌曲页（{res.url[:90]}），可能已失效")
        ref.ident = mid
        ref.extra["song_mid"] = mid
        ref.extra["resolved"] = res.url
        return ref

    # ---------- 取内容 ----------
    def _musicu(self, payload: dict, ctx, tag: str) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = _headers(ctx, json_body=True)
        cookie = _cookie(ctx)
        if cookie:
            headers["Cookie"] = cookie
        try:
            return ctx.net.request(_MUSICU, method="POST", data=body, headers=headers,
                                   referer=_REFERER, timeout=20, retries=3).json()
        except Exception as exc:
            raise SiteUnavailable(f"QQ音乐{tag}接口请求失败：{exc}") from None

    def _track(self, ctx, ref: Ref) -> dict:
        mid = (ref.ident or "").strip()
        if not _MID_RE.fullmatch(mid):
            raise SiteUnavailable("QQ音乐歌曲 mid 无效：" + mid)
        data = self._musicu({
            "comm": {"ct": 24, "cv": 0, "format": "json",
                     "uin": _uin_from_cookie(_cookie(ctx)) or "0"},
            "req_0": {"module": "music.pf_song_detail_svr", "method": "get_song_detail_yqq",
                      "param": {"song_mid": mid, "song_type": 0}},
        }, ctx, "歌曲详情")
        req = data.get("req_0") or {}
        track = ((req.get("data") or {}).get("track_info")) or {}
        if req.get("code") != 0 or not track.get("mid"):
            raise SiteUnavailable(f"QQ音乐没有这首歌（mid={mid}），可能已下架或有版权限制")
        return track

    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        track = self._track(ctx, ref)
        album = track.get("album") or {}
        singers = [s.get("name") for s in (track.get("singer") or []) if s.get("name")]
        duration = int(track.get("interval") or 0)
        info = make_info(
            ref,
            title=track.get("name") or ref.ident,
            cover=_COVER.format(album.get("mid")) if album.get("mid") else "",
            duration=duration,
            album=album.get("name") or "",
            owner=Owner(name=" / ".join(singers)),
            desc=f"专辑：{album.get('name') or '-'}　歌手：{' / '.join(singers) or '-'}"
                 f"　发行：{track.get('time_public') or '-'}",
            media="music",
            kind="music",
        )
        info.engine = self.id
        info.pages = [Page(page=1, part="", duration=duration)]
        info.extra = {
            "song_mid": track.get("mid"),
            "song_id": track.get("id"),
            "pay_play": (track.get("pay") or {}).get("pay_play"),
            "levels": [qn for qn in sorted(_TIERS)
                       if ((track.get("file") or {}).get(_TIERS[qn][3]) or 0) > 0],
        }
        return info

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        track = self._track(ctx, ref)
        cookie = _cookie(ctx)
        uin = _uin_from_cookie(cookie) or "0"
        mid = track.get("mid") or ref.ident
        file_info = track.get("file") or {}
        media_mid = file_info.get("media_mid") or mid
        duration = int(track.get("interval") or 0)
        want = qn if qn in _TIERS else 0

        # 一次请求把四档全要一遍。filename 必须显式给，且 media_mid 只写一遍，
        # 否则接口会装作"你没有权限"直接回 104009。
        tiers = sorted(_TIERS, reverse=True)
        filenames = [f"{_TIERS[t][0]}{media_mid}.{_TIERS[t][1]}" for t in tiers]
        data = self._musicu({
            "comm": {"ct": 24, "cv": 0, "format": "json", "uin": uin},
            "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                      "param": {
                          "guid": _guid(),
                          "songmid": [mid] * len(filenames),
                          "songtype": [0] * len(filenames),
                          "uin": uin,
                          "loginflag": 1,
                          "platform": "20",
                          "filename": filenames,
                      }},
        }, ctx, "取流")
        req = data.get("req_0") or {}
        payload = req.get("data") or {}
        infos = payload.get("midurlinfo") or []
        sip = [s for s in (payload.get("sip") or []) if s]
        base = (sip[0] if sip else _CDN_BASE).rstrip("/")

        got = {}
        for item in infos:
            purl = item.get("purl") or ""
            if not purl:
                continue
            tier = _PREFIX_TIER.get((item.get("filename") or "")[:4], 0)
            if not tier:
                continue
            ext = _TIERS[tier][1]
            url = purl if purl.startswith("http") else base + "/" + purl.lstrip("/")
            url = url.replace("http://", "https://", 1)
            got[tier] = (tier, url, int(file_info.get(_TIERS[tier][3]) or 0), ext)

        if not got:
            detail = "code=%s retcode=%s msg=%s" % (req.get("code"), payload.get("retcode"),
                                                   str(payload.get("msg"))[:40])
            if cookie:
                raise SiteUnavailable(
                    f"QQ音乐没有为这个账号下发任何播放地址（{detail}）。Cookie 可能已过期，"
                    f"或账号没有这首歌的权限。")
            pay = (track.get("pay") or {}).get("pay_play")
            raise SiteUnavailable(
                f"QQ音乐没有给匿名身份下发播放地址（{detail}）。"
                + ("这首歌是付费 / 会员歌曲（pay_play=1），免登录连 128K 都不下发；"
                   if pay else "这首歌当前没有可用的免费档；")
                + "请在「我的」→「QQ音乐」填 qqmusic_cookie 后重试。")

        if want and want not in got:
            available = "、".join(_TIERS[t][2] for t in sorted(got))
            raise SiteUnavailable(
                f"QQ音乐没有下发「{_TIERS[want][2]}」（免登录只有免费歌曲的 128K；"
                f"320K / 无损 / Hi-Res 需要登录且要有对应会员），当前只能拿到：{available}。"
                "请在「我的」→「QQ音乐」填 qqmusic_cookie。")

        chosen = want or max(got)
        tier_qn, url, size, ext = got[chosen]
        play = single_file_play(self, url, size, duration,
                                quality=_TIERS[chosen][2], ext=ext, referer=_REFERER)
        play.media = "music"
        play.ext = ext
        play.accept_quality = sorted(got)
        play.accept_desc = [_TIERS[t][2] for t in sorted(got)]

        missing = [t for t in sorted(_TIERS) if t not in got]
        absent = [t for t in missing if (file_info.get(_TIERS[t][3]) or 0) <= 0]
        phantom = [t for t in missing if (file_info.get(_TIERS[t][3]) or 0) > 0]
        play.phantom_quality = phantom

        if cookie:
            notes = [f"已按 Cookie 里的 uin={uin} 身份请求，档位由服务端按会员等级下发"]
        else:
            notes = ["免登录实测只有免费歌曲（pay_play=0）的 128K mp3，直链由 vkey 接口下发"]
        if absent:
            notes.append("这首歌本身没有 " + "、".join(_TIERS[t][2] for t in absent) + " 版本")
        if phantom:
            if cookie:
                notes.append("账号没拿到 " + "、".join(_TIERS[t][2] for t in phantom)
                             + "（多半是没有对应会员）")
            else:
                notes.append("填 qqmusic_cookie 后由服务端按会员等级下发 "
                             + "、".join(_TIERS[t][2] for t in phantom))
        notes.append("直链带 vkey 签名，有效期约几十分钟，过期重新取流即可")
        play.note = "；".join(notes)
        play.raw = {"uin": uin, "retcode": payload.get("retcode"), "sip": sip}
        return play
