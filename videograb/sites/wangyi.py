"""网易云音乐适配器。

实测（2026-10，无 Cookie）：
  * 免登录能拿到的只有前两档：标准 128K mp3（level=standard）与高品 320K mp3
    （level=exhigh）；
  * 无损（lossless）与 Hi-Res（hires）在无 Cookie 时**不会报错** —— 服务端直接把
    type/level 降级回 exhigh 的 320K mp3 返回。所以档位一律以响应里的 level 字段
    为准，绝不能看我们请求了什么档位；
  * 会员歌曲（fee=1）匿名请求一律 code=-110、url=null，如实抛 SiteUnavailable；
  * 详情接口里的 lMusic / mMusic / hMusic / sqMusic / hrMusic 说明了"这首歌到底有没有
    无损 / Hi-Res 版本"，用它区分"本来就没有这一版"和"有但当前身份拿不到"；
  * 直链带 expi≈1200 秒的签名，过期后重新取流即可。

输入：music.163.com 单曲页（含 /#/song?id= 这种前端路由写法）与 163cn.tv 短链。
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
_HOME = "https://music.163.com"
_REFERER = _HOME + "/"
_DETAIL_API = _HOME + "/api/song/detail/"
_PLAY_API = _HOME + "/api/song/enhance/player/url/v1"

_HOSTS = ("music.163.com",)      # 含 y.music.163.com 这类子域
_SHORT_HOSTS = ("163cn.tv",)     # 分享短链，要联网展开

# qn -> (接口 level 名, 界面文案, 落盘扩展名, 详情里对应的版本字段)
_TIERS = {
    1: ("standard", "标准 128K mp3", "mp3", "lMusic"),
    2: ("exhigh", "高品 320K mp3", "mp3", "hMusic"),
    3: ("lossless", "无损 FLAC", "flac", "sqMusic"),
    4: ("hires", "Hi-Res 母带 FLAC", "flac", "hrMusic"),
}
# 这两档免登录一律拿不到：要 MUSIC_U，账号还得有对应会员
_LOGIN_TIERS = (3, 4)

_ID_IN_BLOB = re.compile(r"[?&#]id=(\d+)")
_SONG_IN_PATH = re.compile(r"/song/(\d+)")
_NON_SONG = ("playlist", "album", "artist", "toplist", "discover", "dj", "radio", "mv")


def _ua(ctx) -> str:
    return (getattr(ctx.settings, "user_agent", "") or "").strip() or _DESKTOP_UA


def _headers(ctx) -> dict:
    return {
        "User-Agent": _ua(ctx),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": _REFERER,
    }


def _cookie(ctx) -> str:
    """读 ncm_cookie。用户可能整串粘 Cookie，也可能只粘 MUSIC_U 的值。"""
    raw = (getattr(ctx.settings, "ncm_cookie", "") or "").strip()
    if not raw:
        return ""
    if ";" in raw or "=" in raw:
        return raw
    return "MUSIC_U=" + raw


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


def _song_id_from_url(url: str) -> str:
    """从单曲页地址里取歌曲 ID（查询串和 #/ 前端路由都要看）。"""
    parsed = urllib.parse.urlparse(url or "")
    path = (parsed.path or "") + (parsed.fragment or "")
    blob = "&" + (parsed.fragment or "") + "&" + (parsed.query or "")
    m = _ID_IN_BLOB.search(blob)
    if m and "song" in path:
        return m.group(1)
    m = _SONG_IN_PATH.search(parsed.path or "")
    return m.group(1) if m else ""


class NeteaseSite(SiteAdapter):
    id = "netease"
    name = "网易云音乐"
    media = "music"
    home = _HOME
    color = "#C20C0C"
    examples = [
        "https://music.163.com/song?id=1973665667",
        "https://music.163.com/#/song?id=1973665667",
        "https://music.163.com/m/song?id=1973665667",
        "163cn.tv 分享短链",
    ]
    note = "免登录最高 320K mp3；无损 FLAC 与 Hi-Res 要填 MUSIC_U Cookie，且账号得有对应会员"
    needs_login = False
    credentials = [
        {
            "key": "cookie",
            "field": "ncm_cookie",
            "label": "MUSIC_U Cookie",
            "secret": True,
            "purpose": "解锁无损 FLAC 与 Hi-Res 母带档。不填时无损 / Hi-Res 请求会被服务端降级成 320K mp3。",
            "howto": [
                "用浏览器打开 <code>music.163.com</code> 并<strong>登录</strong>你的账号",
                "按 <code>F12</code> 打开开发者工具（或右键页面 → 检查）",
                "切换到 <strong>Application</strong>（应用 / 应用程序）标签页",
                "左侧展开 <strong>Cookies</strong>，点选 <code>https://music.163.com</code>",
                "找到 <code>MUSIC_U</code> 那一行，双击 Value 列整行复制",
                "粘到下面的输入框后保存（只粘 MUSIC_U 的值也可以，程序会自动补上名字）",
            ],
            "tip": "在开发者工具的 Console 里执行 "
                   "<code>document.cookie.match(/MUSIC_U=([^;]+)/)[1]</code> 可以直接把值打印出来。",
            "warning": "MUSIC_U 等同于你的登录凭证，只保存在本机的 data/config.json，"
                       "请不要分享给别人；在网易云退出登录后它会立即失效。",
        },
    ]

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        host = _host_of(raw)
        return bool(host) and _known(host, _HOSTS + _SHORT_HOSTS)

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请输入网易云音乐的单曲链接")
        host = _host_of(raw)
        if not host or not _known(host, _HOSTS + _SHORT_HOSTS):
            raise ValueError("不是网易云音乐的链接：" + raw[:60])
        if _known(host, _SHORT_HOSTS):
            # 短链里没有歌曲 ID，交给 normalize 联网展开
            return Ref(raw=raw, site=self.id, kind="music", ident="",
                       extra={"short_url": raw})

        url = raw if "://" in raw else "https://" + raw.lstrip("/")
        parsed = urllib.parse.urlparse(url)
        # 前端路由把 /song?id= 写在 # 后面，所以路径判断要把 fragment 也算进来
        path = (parsed.path or "") + (parsed.fragment or "")
        ident = _song_id_from_url(url)
        if not ident:
            if any(k in path for k in _NON_SONG):
                raise ValueError("网易云只支持单曲链接：歌单 / 专辑 / 歌手 / 电台页请先打开具体一首歌")
            raise ValueError("这个网易云链接里没有歌曲 ID：" + raw[:60])
        return Ref(raw=raw, site=self.id, kind="music", ident=ident,
                   extra={"song_id": int(ident)})

    def normalize(self, ctx, ref: Ref) -> Ref:
        """163cn.tv 短链要跟一次跳转才拿得到歌曲 ID。"""
        if ref.ident or not ref.extra.get("short_url"):
            return ref
        url = ref.extra["short_url"]
        try:
            res = ctx.net.request(url, headers=_headers(ctx), referer=_REFERER,
                                  timeout=20, retries=3)
        except Exception as exc:
            raise SiteUnavailable(f"网易云短链展开失败：{exc}") from None
        ident = _song_id_from_url(res.url)
        if not ident:
            raise SiteUnavailable(f"网易云短链没有跳到歌曲页（{res.url[:90]}），可能已失效")
        ref.ident = ident
        ref.extra["song_id"] = int(ident)
        ref.extra["resolved"] = res.url
        return ref

    # ---------- 取内容 ----------
    def _detail(self, ctx, ident: str) -> dict:
        ids = urllib.parse.quote(json.dumps([int(ident)]))
        try:
            data = ctx.net.get_json(f"{_DETAIL_API}?ids={ids}", headers=_headers(ctx),
                                    referer=_REFERER, timeout=20, retries=3)
        except Exception as exc:
            raise SiteUnavailable(f"网易云歌曲详情接口请求失败：{exc}") from None
        songs = data.get("songs") or []
        if not songs:
            raise SiteUnavailable(f"网易云没有这首歌（id={ident}），可能已下架或有版权限制")
        return songs[0]

    def _track(self, ctx, ref: Ref) -> dict:
        ident = (ref.ident or "").strip()
        if not ident.isdigit():
            raise SiteUnavailable("网易云歌曲 ID 无效：" + ident)
        return self._detail(ctx, ident)

    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        song = self._track(ctx, ref)
        album = song.get("album") or {}
        artists = [a.get("name") for a in (song.get("artists") or []) if a.get("name")]
        duration = int((song.get("duration") or 0) // 1000)
        alias = "、".join(song.get("alias") or [])
        title = song.get("name") or ref.ident
        if alias:
            title = f"{title}（{alias}）"
        info = make_info(
            ref,
            title=title,
            cover=album.get("picUrl") or "",
            duration=duration,
            album=album.get("name") or "",
            owner=Owner(name=" / ".join(artists)),
            desc=f"专辑：{album.get('name') or '-'}　歌手：{' / '.join(artists) or '-'}",
            media="music",
            kind="music",
        )
        info.engine = self.id
        info.pubdate = int((song.get("publishTime") or 0) // 1000)
        info.pages = [Page(page=1, part="", duration=duration)]
        info.extra = {
            "song_id": song.get("id"),
            "fee": song.get("fee"),
            "album_id": album.get("id"),
            "levels": [qn for qn in sorted(_TIERS) if song.get(_TIERS[qn][3])],
        }
        return info

    # 单档探测：返回 (qn, url, size, 扩展名) 或 None
    def _probe(self, ctx, ident: str, tier: int, cookie: str):
        level, _, _, _ = _TIERS[tier]
        query = urllib.parse.urlencode({
            "encodeType": "flac" if tier >= 3 else "mp3",
            "level": level,
            "id": ident,
            "ids": json.dumps([int(ident)]),
        })
        headers = _headers(ctx)
        if cookie:
            headers["Cookie"] = cookie
        try:
            data = ctx.net.get_json(f"{_PLAY_API}?{query}", headers=headers,
                                    referer=_REFERER, timeout=20, retries=3)
        except Exception as exc:
            raise SiteUnavailable(f"网易云取流接口请求失败：{exc}") from None
        item = (data.get("data") or [{}])[0]
        url = item.get("url") or ""
        if item.get("code") != 200 or not url:
            return None
        # 服务端会按账号权限降级，档位只认它自己报的 level
        got = {v[0]: k for k, v in _TIERS.items()}.get(item.get("level") or "")
        if not got:
            return None
        ext = "flac" if (item.get("type") or "").lower() == "flac" else "mp3"
        return got, url.replace("http://", "https://", 1), int(item.get("size") or 0), ext

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        song = self._track(ctx, ref)
        ident = str(song.get("id") or ref.ident)
        duration = int((song.get("duration") or 0) // 1000)
        cookie = _cookie(ctx)
        want = qn if qn in _TIERS else 0

        if want in _LOGIN_TIERS and not cookie:
            raise SiteUnavailable(
                f"网易云的「{_TIERS[want][1]}」需要登录后才会下发：请在「我的」→「网易云音乐」里"
                f"填 MUSIC_U Cookie。免登录实测最高只能拿到「{_TIERS[2][1]}」。")

        # 候选档位：匿名只有前两档，带 Cookie 才去试无损 / Hi-Res
        candidates = [1, 2] + ([3, 4] if cookie else [])
        got = {}
        for tier in sorted(candidates, reverse=True):
            item = self._probe(ctx, ident, tier, cookie)
            if item:
                got[item[0]] = item
        if not got:
            who = "当前 MUSIC_U 账号" if cookie else "匿名身份"
            fee = song.get("fee")
            if fee == 1:
                raise SiteUnavailable(
                    f"这首歌是网易云会员歌曲（fee=1），{who}拿不到播放地址"
                    "（接口回 code=-110、url 为空）。"
                    + ("请换一个有会员的 MUSIC_U Cookie。"
                       if cookie else "请在「我的」→「网易云音乐」填 MUSIC_U Cookie 后重试。"))
            raise SiteUnavailable(
                f"网易云没有给{who}下发任何可用的播放地址（接口 code 非 200 或 url 为空）。"
                "多半是版权下架或地区限制；有 MUSIC_U Cookie 的话填上再试。")

        if want and want not in got:
            available = "、".join(_TIERS[q][1] for q in sorted(got))
            raise SiteUnavailable(
                f"网易云没有下发「{_TIERS[want][1]}」，当前身份实际只能拿到：{available}。"
                + ("无损 / Hi-Res 需要账号有对应会员。" if want in _LOGIN_TIERS else ""))

        chosen = want or max(got)
        _, url, size, ext = got[chosen]
        play = single_file_play(self, url, size, duration,
                                quality=_TIERS[chosen][1], ext=ext, referer=_REFERER)
        play.media = "music"
        play.ext = ext
        play.accept_quality = sorted(got)
        play.accept_desc = [_TIERS[q][1] for q in sorted(got)]

        # 这首歌"有、但当前身份没拿到"的档位才算虚标；本来就没有的版本不列进去
        missing = [q for q in sorted(_TIERS) if q not in got]
        absent = [q for q in missing if not song.get(_TIERS[q][3])]
        phantom = [q for q in missing if song.get(_TIERS[q][3])]
        play.phantom_quality = phantom

        if cookie:
            notes = ["已带 MUSIC_U Cookie 请求，档位以服务端实际下发的 level 字段为准"]
        else:
            notes = ["免登录实测最高 320K mp3（level=exhigh），无损 / Hi-Res 请求会被服务端降级"]
        if absent:
            notes.append("这首歌本身没有 " + "、".join(_TIERS[q][1] for q in absent) + " 版本")
        if phantom:
            if cookie:
                notes.append("已带 MUSIC_U 但服务端没下发 " + "、".join(_TIERS[q][1] for q in phantom)
                             + "（多半是该账号没有对应会员）")
            else:
                notes.append("填 MUSIC_U Cookie 后由服务端按会员等级下发 "
                             + "、".join(_TIERS[q][1] for q in phantom))
        notes.append("直链签名约 20 分钟过期，过期重新取流即可")
        play.note = "；".join(notes)
        play.raw = {"level": _TIERS[chosen][0], "song_id": song.get("id"), "fee": song.get("fee")}
        return play
