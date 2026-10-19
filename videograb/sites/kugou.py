"""酷狗音乐适配器。

实测（2026-10，无 Cookie）：
  * 歌曲详情 + 128K mp3 直链走移动端接口
    https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={hash}
    一次请求给出 songName / singerName / timeLength / imgUrl / url / fileSize，
    以及 extra 里这首歌全部版本的 hash（128hash / 320hash / sqhash / highhash）与字节数。
  * 320K mp3 与无损 FLAC 走酷狗自己的资源接口
    https://trackercdn.kugou.com/i/v2/?key={md5(hash+"kgcloudv2")}&hash={hash}&br=320&...
    免登录就能下载（返回自带 extName / bitRate / fileSize，档位一律以它为准）；
    付费歌曲它直接回 status=2、不给地址，这时如实报错，不做任何绕过。
  * 网页版 https://wwwapi.kugou.com/yy/index.php?r=play/getdata 现在对匿名请求一律回
    err_code=30020（SSA 风控），所以没有采用它。
  * 分享地址 www.kugou.com/song/#hash=XXXX 的 hash 写在 # 后面，纯字符串即可解析；
    /mixsong/{数字}.html 拿不到 hash（该页面已不再服务端直出），会明确报错。
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse

from ..models import Owner, Page, PlayInfo, VideoInfo
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info, single_file_play

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HOME = "https://www.kugou.com"
_REFERER = _HOME + "/"
_GETSONG_API = "https://m.kugou.com/app/i/getSongInfo.php"
_TRACKER_API = "https://trackercdn.kugou.com/i/v2/"
_SEARCH_API = "https://songsearch.kugou.com/song_search_v2"
# trackercdn v2 的固定 key 盐（酷狗客户端内置的公开常量，不是登录态）
_TRACKER_SALT = "kgcloudv2"

# qn -> (界面文案, 落盘扩展名)
_TIERS = {
    1: ("标准 128K mp3", "mp3"),
    2: ("高品 320K mp3", "mp3"),
    3: ("无损 FLAC", "flac"),
    4: ("Hi-Res 母带 FLAC", "flac"),
}

_HEX32 = re.compile(r"^[0-9A-Fa-f]{32}$")
_HASH_ANY = re.compile(r"[?&#/]hash=([0-9A-Fa-f]{32})", re.I)
_HASH_LOOSE = re.compile(r"\b([0-9A-Fa-f]{32})\b")
_MIXSONG_RE = re.compile(r"/mixsong/(\d+)", re.I)
_SHORT_RE = re.compile(r"(?:https?://)?t\d?\.kugou\.com/[A-Za-z0-9_\-]+", re.I)

_MIXSONG_HINT = ("酷狗新版 /mixsong/{id}.html 详情页已不再服务端直出，拿不到歌曲 hash。"
                 "请在酷狗网页版打开这首歌，用「复制链接」得到 "
                 "https://www.kugou.com/song/#hash=XXXX 这种带 hash 的地址。")


def _ua(ctx) -> str:
    return (getattr(ctx.settings, "user_agent", "") or "").strip() or _DESKTOP_UA


def _headers(ctx, cookie: str = "") -> dict:
    headers = {
        "User-Agent": _ua(ctx),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": _REFERER,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _cookie(ctx) -> str:
    """读 kugou_cookie。用户可能整串粘 Cookie，也可能只粘一个 token 值。"""
    raw = (getattr(ctx.settings, "kugou_cookie", "") or "").strip()
    if not raw:
        return ""
    if "=" in raw:
        return raw
    return "token=" + raw


def _host_of(text: str) -> str:
    raw = (text or "").strip()
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        return (urllib.parse.urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""


def _is_kugou(host: str) -> bool:
    return bool(host) and (host == "kugou.com" or host.endswith(".kugou.com"))


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _img(url: str) -> str:
    """酷狗的图片地址带 {size} 占位符，顺手升级到 https。"""
    url = (url or "").strip()
    if not url:
        return ""
    url = url.replace("{size}", "500")
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


class KugouSite(SiteAdapter):
    id = "kugou"
    name = "酷狗音乐"
    media = "music"
    home = _HOME
    color = "#2CA2F9"
    examples = [
        "https://www.kugou.com/song/#hash=18AB3A8EB949AD1B17842B485C9A5357",
        "https://www.kugou.com/song/?hash=18AB3A8EB949AD1B17842B485C9A5357",
        "18AB3A8EB949AD1B17842B485C9A5357（32 位歌曲 hash）",
    ]
    note = ("免登录实测可拿到标准 128K mp3、高品 320K mp3 与无损 FLAC（以歌曲本身拥有的版本为准）；"
            "付费 / 会员歌曲接口直接拒绝，填 Cookie 也不保证能拿到。")
    needs_login = False
    credentials = [
        {
            "key": "cookie",
            "field": "kugou_cookie",
            "label": "酷狗 Cookie",
            "secret": True,
            "purpose": "带上登录（最好是会员）账号的 Cookie 去请求酷狗接口，部分 VIP 歌曲才会下发 320K / 无损资源。",
            "howto": [
                "用浏览器打开 <code>https://www.kugou.com/</code> 并<strong>登录</strong>账号",
                "按 <code>F12</code> 打开开发者工具（或右键页面 → 检查）",
                "切换到 <strong>Network</strong>（网络）标签页，刷新一次页面",
                "点开任意一条 <code>kugou.com</code> 请求，找到 <strong>Request Headers</strong> 里的 <code>Cookie</code>",
                "把 <code>Cookie:</code> 后面<strong>整串</strong>复制下来（至少要含 <code>token=</code>、<code>userid=</code>、<code>kg_mid=</code>）",
                "粘到下面的输入框后保存",
            ],
            "tip": "不填 Cookie 也能下载大部分免费歌曲的 128K / 320K / 无损，先不填试一次更快。",
            "warning": "Cookie 等同于你的登录凭证，只保存在本机的 data/config.json，请不要分享给别人；"
                       "在酷狗退出登录后它会立即失效。",
        },
    ]

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if _HEX32.fullmatch(raw):
            return True
        return "kugou.com" in raw.lower()

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请输入酷狗歌曲链接或 32 位歌曲 hash")
        ref = Ref(raw=raw, site=self.id, kind="music")

        if _HEX32.fullmatch(raw):
            ref.ident = raw.upper()
            return ref

        found = _HASH_ANY.search(raw)
        if found:
            ref.ident = found.group(1).upper()
            return ref

        host = _host_of(raw)
        if not _is_kugou(host):
            raise ValueError("无法从输入中识别酷狗歌曲：" + raw[:80])

        found = _MIXSONG_RE.search(raw)
        if found:
            # 详情页拿不到 hash，先记下来，normalize 时给一句人话解释
            ref.extra["mixsong_id"] = found.group(1)
            return ref
        found = _SHORT_RE.search(raw)
        if found:
            url = found.group(0)
            if not url.lower().startswith("http"):
                url = "https://" + url
            ref.extra["short_url"] = url
            return ref
        found = _HASH_LOOSE.search(raw)
        if found:
            ref.ident = found.group(1).upper()
            return ref
        raise ValueError("这个酷狗地址里没有歌曲 hash：" + raw[:80])

    def normalize(self, ctx, ref: Ref) -> Ref:
        """短链展开；mixsong 详情页明确报错（新版页面拿不到 hash）。"""
        if ref.ident:
            return ref
        url = ref.extra.get("short_url")
        if url:
            try:
                res = ctx.net.request(url, headers=_headers(ctx), referer=_REFERER,
                                      timeout=20, retries=3)
            except Exception as exc:
                raise SiteUnavailable(f"酷狗短链展开失败：{exc}") from None
            final = res.url or ""
            found = _HASH_ANY.search(final) or _HASH_LOOSE.search(final)
            if found:
                ref.ident = found.group(1).upper()
                ref.extra["resolved"] = final
                return ref
            if _MIXSONG_RE.search(final):
                ref.extra["mixsong_id"] = _MIXSONG_RE.search(final).group(1)
                raise SiteUnavailable(_MIXSONG_HINT)
            raise SiteUnavailable(f"酷狗短链没有跳到歌曲页（{final[:90]}），可能已失效")
        if ref.extra.get("mixsong_id"):
            raise SiteUnavailable(_MIXSONG_HINT)
        raise SiteUnavailable("缺少酷狗歌曲 hash，请重新复制分享链接")

    # ---------- 接口 ----------
    def _song_info(self, ctx, ref: Ref) -> dict:
        """getSongInfo：详情 + 128K 直链 + 各档 hash，一次请求全拿到。"""
        cached = ref.extra.get("_info")
        if isinstance(cached, dict):
            return cached
        ident = (ref.ident or "").strip().upper()
        if not _HEX32.fullmatch(ident):
            raise SiteUnavailable("酷狗歌曲 hash 无效：" + (ref.ident or "(空)"))
        url = f"{_GETSONG_API}?cmd=playInfo&hash={ident}"
        try:
            data = ctx.net.get_json(url, headers=_headers(ctx, _cookie(ctx)),
                                    referer=_REFERER, timeout=20, retries=3)
        except Exception as exc:
            raise SiteUnavailable(f"酷狗歌曲详情接口请求失败：{exc}") from None
        if not isinstance(data, dict) or not (data.get("songName") or data.get("hash")):
            msg = (data or {}).get("error") or "接口没有返回歌曲信息"
            raise SiteUnavailable(f"酷狗取不到这首歌（{msg}），可能是 hash 不对或歌曲已下架")
        ref.extra["_info"] = data
        return data

    def _hashes(self, ref: Ref, info: dict) -> dict:
        """这首歌各档位对应的 hash；没有的档位不写进去。"""
        extra = info.get("extra") or {}

        def clean(value) -> str:
            value = str(value or "").strip().upper()
            return value if _HEX32.fullmatch(value) else ""

        base = clean(info.get("hash")) or (ref.ident or "").upper()
        out = {1: clean(extra.get("128hash")) or base}
        for tier, key in ((2, "320hash"), (3, "sqhash")):
            value = clean(extra.get(key))
            if value:
                out[tier] = value
        high = clean(extra.get("highhash"))
        if high and _int(extra.get("highbitrate")) >= 1000:
            out[4] = high
        return out

    def _duration(self, info: dict) -> int:
        extra = info.get("extra") or {}
        seconds = _int(info.get("timeLength"))
        if seconds > 0:
            return seconds
        for key in ("320timelength", "128timelength", "sqtimelength", "hightimelength"):
            ms = _int(extra.get(key))
            if ms > 0:
                return int(ms // 1000)
        return 0

    def _tracker(self, ctx, hash_: str, br: str):
        """按 hash 向酷狗 CDN 要资源；只有 status=1 且带 url 才算拿到了。"""
        cookie = _cookie(ctx)
        for cand in dict.fromkeys([hash_.upper(), hash_.lower()]):
            key = hashlib.md5((cand + _TRACKER_SALT).encode()).hexdigest()
            url = (f"{_TRACKER_API}?key={key}&hash={cand}&br={br}"
                   f"&appid=1005&pid=2&cmd=25&behavior=play")
            try:
                data = ctx.net.get_json(url, headers=_headers(ctx, cookie),
                                        referer=_REFERER, timeout=20, retries=3)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("status") == 1 and (data.get("url") or []):
                return data
        return None

    @staticmethod
    def _classify(ext: str, bitrate: int) -> int:
        """按返回里的真实扩展名与码率判档，不按"我们请求了什么档"判。"""
        ext = (ext or "").lower()
        if bitrate and bitrate < 10000:      # getSongInfo 给的是 kbps
            bitrate *= 1000
        if ext.startswith("flac"):
            return 4 if bitrate >= 1000000 else 3
        return 2 if bitrate >= 256000 else 1

    def _resolve(self, ctx, ref: Ref, info: dict, tier: int, hashes: dict):
        """取某一档的直链；拿不到返回 None。"""
        hash_ = hashes.get(tier)
        if not hash_:
            return None
        data = None
        if tier == 1:
            url = (info.get("url") or "").strip()
            if url:
                bitrate = _int(info.get("bitRate"))
                return {"url": url, "size": _int(info.get("fileSize")), "ext": "mp3",
                        "qn": self._classify("mp3", bitrate),
                        "route": "getSongInfo", "bitrate": bitrate}
            data = self._tracker(ctx, hash_, "128")
        else:
            data = self._tracker(ctx, hash_, {2: "320", 3: "sq", 4: "flac"}[tier])
        if not data:
            return None
        url = (data.get("url") or [""])[0]
        if not url:
            return None
        size, br = _int(data.get("fileSize")), _int(data.get("bitRate"))
        ext = (data.get("extName") or "mp3").lower()
        return {"url": url, "size": size,
                "ext": "flac" if ext.startswith("flac") else "mp3",
                "qn": self._classify(ext, br), "route": "trackercdn", "bitrate": br}

    def _album(self, ctx, ref: Ref, info: dict, hashes: dict) -> dict:
        """用公开搜索接口按 hash 反查专辑名与封面，失败就当没有。"""
        name = (info.get("songName") or "").strip()
        singer = (info.get("singerName") or info.get("author_name") or "").strip()
        if not name:
            return {}
        query = urllib.parse.urlencode({
            "keyword": f"{name} {singer}".strip(), "page": 1, "pagesize": 20,
            "platform": "WebFilter", "userid": -1, "clientver": 2000,
            "iscorrection": 1, "privilege_filter": 0, "filter": 10,
            "token": "", "appid": 1014,
        })
        try:
            data = ctx.net.get_json(f"{_SEARCH_API}?{query}", headers=_headers(ctx, _cookie(ctx)),
                                    referer=_REFERER, timeout=20, retries=2)
        except Exception:
            return {}
        mine = {value for value in hashes.values() if value}
        for item in ((data.get("data") or {}).get("lists") or []):
            found = {str(item.get(key) or "").upper() for key in
                     ("FileHash", "HQFileHash", "SQFileHash", "ResFileHash", "SuperFileHash")}
            if mine & found:
                return {"album": item.get("AlbumName") or "",
                        "cover": _img(item.get("AlbumImage") or item.get("Image") or "")}
        return {}

    # ---------- 取内容 ----------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        ref = self.normalize(ctx, ref)
        info = self._song_info(ctx, ref)
        hashes = self._hashes(ref, info)
        duration = self._duration(info)
        title = (info.get("songName") or ref.ident or "").strip()
        extra = info.get("extra") or {}
        trans = info.get("trans_param") or {}
        singer = (info.get("singerName") or info.get("author_name") or "").strip()
        authors = info.get("authors") or []
        if not singer and authors:
            singer = (authors[0] or {}).get("author_name") or ""
        album = self._album(ctx, ref, info, hashes)
        cover = album.get("cover") or _img(trans.get("union_cover")) or _img(info.get("imgUrl"))
        vinfo = make_info(
            ref,
            title=title or ("酷狗 " + (ref.ident or "")),
            cover=cover,
            duration=duration,
            album=album.get("album") or "",
            owner=Owner(name=singer, face=_img(info.get("imgUrl"))),
            desc=f"歌手：{singer or '-'}　专辑：{album.get('album') or '-'}　时长：{duration} 秒",
            media="music",
            kind="music",
        )
        vinfo.engine = self.id
        vinfo.pages = [Page(page=1, part=title, duration=duration)]
        vinfo.extra = {
            "hash": (ref.ident or "").upper(),
            "audio_id": info.get("audio_id"),
            "album_id": info.get("albumid") or info.get("req_albumid"),
            "hashes": hashes,
            "levels": sorted(tier for tier, value in hashes.items() if value),
            "free_bytes": extra.get("128filesize") or 0,
        }
        return vinfo

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        ref = self.normalize(ctx, ref)
        info = self._song_info(ctx, ref)
        hashes = self._hashes(ref, info)
        duration = self._duration(info)
        want = qn if qn in _TIERS else 0
        cookie = _cookie(ctx)

        got: dict = {}
        for tier in sorted(hashes, reverse=True):
            item = self._resolve(ctx, ref, info, tier, hashes)
            if item:
                got.setdefault(item["qn"], item)

        if not got:
            hint = ("这首歌在酷狗是付费 / 会员资源，接口直接拒绝下发任何档位"
                    f"（fail_process={info.get('fail_process')}）。")
            if not cookie:
                hint += "可以在「设置 → 平台登录」→「酷狗音乐」填上登录（最好是会员）账号的 Cookie 再试。"
            else:
                hint += "已带 Cookie 仍被拒绝，说明当前账号没有这首歌的下载权限。"
            raise SiteUnavailable(hint)

        if want and want not in got:
            have = "、".join(_TIERS[q][0] for q in sorted(got))
            raise SiteUnavailable(
                f"酷狗没有下发「{_TIERS[want][0]}」，当前身份实际只能拿到：{have}。"
                + ("这一档需要歌曲本身有无损 / 母带版本。" if want >= 3 else ""))

        chosen = want or max(got)
        item = got[chosen]
        play = single_file_play(self, item["url"], item["size"], duration,
                                quality=_TIERS[chosen][0], ext=item["ext"], referer=_REFERER)
        play.media = "music"
        play.ext = item["ext"]
        play.duration = duration
        play.accept_quality = sorted(got, reverse=True)
        play.accept_desc = [_TIERS[q][0] for q in sorted(got)]

        # 这首歌"有这一版、但当前身份没拿到"的档位才算虚标；本来就没有的版本不列
        phantom = sorted(q for q in hashes if q not in got)
        play.phantom_quality = phantom

        size_text = f"{item['size'] / 1048576:.2f} MB" if item.get("size") else "接口未给大小"
        notes = []
        if item.get("route") == "getSongInfo":
            notes.append("128K mp3 直链由酷狗移动接口 getSongInfo 下发")
        else:
            notes.append(f"{_TIERS[chosen][0]} 直链由酷狗 CDN trackercdn 下发，"
                         f"接口自报 extName={item['ext']} / bitRate={item.get('bitrate', 0)}")
        notes.append("实测 " + size_text)
        if phantom:
            notes.append("这首歌存在 " + "、".join(_TIERS[q][0] for q in phantom)
                         + " 版本，但当前身份没拿到")
        if not cookie:
            notes.append("以上是免登录结果；填 kugou_cookie 可再试 VIP 歌曲")
        notes.append("直链带时间戳，过一段时间会失效，重新取流即可")
        play.note = "；".join(notes)
        play.raw = {"hash": ref.ident, "route": item.get("route"),
                    "size": item.get("size"), "bitrate": item.get("bitrate")}
        return play
