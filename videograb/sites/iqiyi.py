"""爱奇艺适配器。

实测可用路径（2026-10 逐一验证）：
  1. 短 ID -> tvid：页面 URL 里的 v_xxxxxxxxxxx 是 base36 短号，播放器 JS 里带一套
     异或换位算法（见 _patch_to_id）。已用 4 组真实数据（推荐接口同时给出
     playUrl 与 tvId）验证，4/4 完全一致，所以这一步不需要联网。
  2. 元数据：https://pcw-api.iqiyi.com/video/video/playervideoinfo?tvid={tvid}
     返回标题 vn / 封面 vpic / 简介 info / 时长 plg / 频道 cid / 上映日期 issueTime /
     付费标记 payMark / vipType，一次请求全齐。这是目前唯一实测可用的元数据接口。
  3. 播放地址按顺序试两条通路：
     a) https://cache.video.iqiyi.com/dash（PCW，主路径）—— 要 vf 签名。签名算法是从
        播放器 bundle（pcweb.wonder.*.online.js）里读出来的：
        vf = md5(path+query 再拼 32 个由固定公式生成的字符)，见 _vf()。
     b) https://cache.video.iqiyi.com/jp/dash（H5/JP 变体）—— 同一套签名与参数，
        响应外面套一层 var tvInfoJs={...}（_dash_once 会剥掉），实测两次都可用。
     带上签名后 code=A00000，program.video 里按 bid 给出 200/300/500/600 等档位。
     注意：只有「本次请求的那个 bid」会带 fs 分片表，所以要按目标档位再请求一次。
  4. 分片落地：fs[].l 是路径，拿 https://data.video.iqiyi.com/videos + 路径 换回真实
     CDN 直链（*.71edge.com）。实测该直链支持 Range，206 + Content-Range，
     字节数与 fs[].b 完全对得上。

实测走不通的接口（不要再试）：
  * cache.video.iqiyi.com/vps —— 同一套 vf 签名被回 "This sign is not correct."
    （该端点用的是另一套盐）。
  * cache.m.iqiyi.com/jp/tmts/...（移动端 tmts）—— 无论带不带签名、带不带 modeCode，
    一律 code=A00001 "Rule Block"。
  * cache.m.iqiyi.com/dash、http://cache.video.qiyi.com/dash —— 404 / 证书不匹配。
  * pcw-api 的 baseinfo / getvideoinfo / videoinfo / album videolist 等路径 —— 一律 404。
  * web 播放页与 m.iqiyi.com 移动播放页内嵌数据里没有播放地址（页面里没有 playUrl/m3u8）。

限制：未登录时接口对会员片只下发试看片段（boss.previewTime，实测 6 分钟，
duration 却有 106 分钟），下载到的是试看而不是全片；免费内容则是完整分片表。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.parse

from ..models import Durl, Owner, Page, PlayInfo, VideoInfo
from ..quality import estimate_height, rank_from_resolution
from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://www.iqiyi.com/"
# H5/JP 变体只认移动端 UA
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
DASH_URL = "https://cache.video.iqiyi.com/dash"
# 取流通路表：(名称, 接口地址, ori 参数, UA 档位)
#   1 是实测主路径（PCW 接口，返回裸 JSON，ori=pcw）；
#   2 是同一个签名算法下的 H5/JP 变体，响应外面套一层 var tvInfoJs={...}，
#     实测两次都拿到 A00000 + bid500 + 2 段分片，签名和参数与主路径一致。
_DASH_ROUTES = (
    ("cache.video.iqiyi.com/dash（PCW）", DASH_URL, "pcw", "desktop"),
    ("cache.video.iqiyi.com/jp/dash（H5）",
     "https://cache.video.iqiyi.com/jp/dash", "h5", "mobile"),
)
DD_URL = "http://pcw-data.video.iqiyi.com/videos"
PVI_URL = "https://pcw-api.iqiyi.com/video/video/playervideoinfo"
# 播放器下发的固定 src（PCW 端口标识），签名对这个值不敏感但保持一致更稳
SRC = "01010031010000000000"

_SHORT_IN_URL = re.compile(r"/[vwp]_([0-9A-Za-z]+)\.html")
_TVID_IN_URL = re.compile(r"[?&]tvid=(\d+)")
_BARE_SHORT = re.compile(r"[0-9a-z]{8,16}")

# bid -> 中文画质名（爱奇艺自己的档位编号）
BID_LABELS = {
    100: "极速 240P",
    200: "流畅 360P",
    300: "标清 480P",
    500: "高清 720P",
    600: "超清 1080P",
    610: "超清 720P60",
    700: "蓝光 720P",
    800: "蓝光 1080P",
    1020: "4K",
    1030: "4K 60帧",
    4000: "杜比视界",
}

# 频道 id -> 中文分类，用于 tname
CHANNELS = {
    1: "电影", 2: "电视剧", 3: "纪录片", 4: "动漫", 5: "综艺",
    6: "综艺", 7: "娱乐", 8: "体育", 9: "军事", 10: "生活",
    11: "音乐", 12: "游戏", 13: "搞笑", 14: "原创", 15: "儿童",
    16: "科技", 17: "旅游", 18: "时尚", 19: "教育", 20: "片花",
}


def _suffix32() -> str:
    """播放器里那段固定生成的 32 个字符，签名的盐。"""
    out = []
    for t in range(4):
        for i in range(2):
            for n in range(4):
                value = (70 * t + 677 * i + 21 * n + 87 * t * i * n + 59) % 30
                out.append(chr(value + (48 if value < 9 else 88)))
    return "".join(out)


_VF_SALT = _suffix32()


def _vf(full_url: str) -> str:
    """vf 签名：md5(path + query + 固定盐)，路径里不含协议与域名。"""
    parts = urllib.parse.urlsplit(full_url)
    target = parts.path + ("?" + parts.query if parts.query else "")
    return hashlib.md5((target + _VF_SALT).encode("utf-8")).hexdigest()


def _patch_to_id(short: str) -> int:
    """base36 短号 -> tvid。算法逐行照搬播放器 patchToID，已用真实数据验证。"""
    try:
        value = int(short, 36)
    except ValueError:
        raise SiteUnavailable(f"爱奇艺短号不合法：{short!r}") from None
    key = bin(0x75706971676C)[2:][::-1]
    bits = bin(value)[2:][::-1]
    merged = []
    for index in range(max(len(bits), len(key))):
        left = int(bits[index]) if index < len(bits) else 0
        right = int(key[index]) if index < len(key) else 0
        merged.append(left ^ right)
    result = int("".join(str(x) for x in merged)[::-1], 2)
    if result < 900000:
        result = 100 * (result + 900000)
    return result


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 接口偶尔不给 scrsz，用 bid 反推一个近似高度，仅用于挑档位
BID_HEIGHTS = {
    100: 240, 200: 360, 300: 480, 500: 720, 600: 1080, 610: 720,
    700: 720, 800: 1080, 1020: 2160, 1030: 2160, 4000: 2160,
}


def _short_side(entry: dict, bid: int) -> int:
    """档位的短边像素。竖屏视频也用短边比较，避免 720x1280 被当成 1280P。"""
    width, height = _resolution(entry.get("scrsz") or "")
    sides = [x for x in (width, height) if x]
    if sides:
        return min(sides)
    return BID_HEIGHTS.get(bid, 0)


def _resolution(text: str) -> tuple[int, int]:
    """scrsz 形如 "1280x536"。"""
    found = re.match(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*$", text or "")
    if not found:
        return 0, 0
    return int(found.group(1)), int(found.group(2))


class IqiyiSite(SiteAdapter):
    id = "iqiyi"
    name = "爱奇艺"
    home = "https://www.iqiyi.com"
    color = "#00CC36"
    examples = [
        "https://www.iqiyi.com/v_1lg5qzzbjq8.html",
        "https://m.iqiyi.com/v_1lg5qzzbjq8.html",
    ]
    note = ("播放走两条 dash 通路依次重试（PCW → H5/JP 变体），签名算法一致；"
            "未登录时会员片只下发试看片段")
    needs_login = False

    def __init__(self) -> None:
        # 上次成功的通路（优先复用）与本条数据实际走的通路名（写进报告）
        self._preferred = ""
        self._route = ""

    # ---------------- 识别与解析 ----------------
    def match(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        return "iqiyi.com" in raw.lower()

    def parse(self, text: str) -> Ref:
        raw = (text or "").strip()
        if not raw:
            raise SiteUnavailable("请输入爱奇艺视频地址")
        plain = urllib.parse.unquote(raw)
        short = ""
        found = _SHORT_IN_URL.search(plain)
        if found:
            short = found.group(1)
        else:
            tvid_found = _TVID_IN_URL.search(plain)
            if tvid_found:
                tvid = int(tvid_found.group(1))
                ref = Ref(raw=raw, site=self.id, ident=str(tvid), kind="video")
                ref.extra = {"tvid": tvid, "vid": ""}
                return ref
            if _BARE_SHORT.fullmatch(raw):
                short = raw
        if not short:
            if "/a_" in plain:
                raise SiteUnavailable(
                    "这是爱奇艺的专辑页（a_ 开头），不是单集地址。"
                    "请点进具体一集，复制 v_ 开头的播放页地址。"
                )
            raise SiteUnavailable(
                "没能在链接里找到爱奇艺的短号（形如 /v_1lg5qzzbjq8.html）"
            )
        ref = Ref(raw=raw, site=self.id, ident=short, kind="video")
        ref.extra = {"vid": short, "tvid": _patch_to_id(short)}
        return ref

    def normalize(self, ctx, ref: Ref) -> Ref:
        """tvid 是纯计算出来的，这里只做一次补全，不发请求。"""
        extra = dict(ref.extra or {})
        if not extra.get("tvid"):
            short = extra.get("vid") or ref.ident
            if short and not str(short).isdigit():
                extra["tvid"] = _patch_to_id(short)
            elif str(short).isdigit():
                extra["tvid"] = int(short)
        ref.extra = extra
        return ref

    # ---------------- 内部：签名请求 ----------------
    @staticmethod
    def _tvid(ref: Ref) -> int:
        extra = ref.extra or {}
        tvid = _int(extra.get("tvid"))
        if not tvid:
            short = extra.get("vid") or ref.ident
            if short and not str(short).isdigit():
                tvid = _patch_to_id(str(short))
            else:
                tvid = _int(short)
        if not tvid:
            raise SiteUnavailable("Ref 里没有爱奇艺 tvid，无法继续")
        return tvid

    def _ordered_routes(self) -> list:
        """上次成功的通路优先，减少故障切换时的等待。"""
        if self._preferred:
            return ([r for r in _DASH_ROUTES if r[1] == self._preferred]
                    + [r for r in _DASH_ROUTES if r[1] != self._preferred])
        return list(_DASH_ROUTES)

    def _dash_once(self, ctx, base: str, ori: str, ua: str, tvid: int, vid: str,
                   bid: int) -> dict:
        """向指定的通路要一次播放信息，返回 data 段。"""
        tm = str(int(time.time() * 1000))
        kuid = _md5(f"{random.random()}_{random.random()}")
        # 播放器里是 md5(md5(up) + tm + tvid)，匿名时 up 为空串
        auth = _md5(_md5("") + tm + str(tvid))
        params = [
            ("tvid", str(tvid)), ("bid", str(bid)), ("vid", vid), ("src", SRC),
            ("vt", "0"), ("rs", "1"), ("uid", ""), ("ori", ori), ("ps", "1"),
            ("k_uid", kuid), ("pt", "0"), ("d", "0"), ("s", ""), ("lid", "0"),
            ("cf", "0"), ("ct", "0"), ("authKey", auth), ("k_tag", "1"), ("dfp", ""),
            ("locale", "zh_cn"), ("up", ""), ("qd_v", "a1"), ("tm", tm), ("sr", "1"),
        ]
        url = base + "?" + urllib.parse.urlencode(params)
        url = url + "&vf=" + _vf(url)
        headers = {
            "User-Agent": MOBILE_UA if ua == "mobile" else DESKTOP_UA,
            "Accept": "application/json, */*",
            "Origin": "https://www.iqiyi.com",
        }
        try:
            raw = ctx.net.get_text(url, headers=headers,
                                   referer="https://m.iqiyi.com/" if ua == "mobile" else REFERER,
                                   retries=2, timeout=20)
        except Exception as exc:
            raise SiteUnavailable(f"请求失败（{str(exc)[:90]}）") from None
        # H5 变体外面套一层 var tvInfoJs={...}，主路径是裸 JSON，两种都认
        found = re.search(r"tvInfoJs\s*=\s*(\{.*\})\s*;?\s*$", raw.strip(), re.S)
        body = found.group(1) if found else raw.strip()
        try:
            data = json.loads(body)
        except ValueError:
            raise SiteUnavailable("返回的不是 JSON，可能被风控了") from None
        if not isinstance(data, dict):
            raise SiteUnavailable("返回的不是 JSON 对象，可能被风控了")
        code = data.get("code")
        if code != "A00000":
            msg = data.get("msg") or ""
            if "sign" in str(msg).lower():
                raise SiteUnavailable("接口拒绝了签名（vf 算法可能又被改了）")
            raise SiteUnavailable(f"接口返回 {code}：{msg or '未知错误'}")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise SiteUnavailable("接口没有返回 data 段")
        return payload

    def _dash(self, ctx, tvid: int, vid: str, bid: int) -> dict:
        reasons: list[str] = []
        for name, base, ori, ua in self._ordered_routes():
            try:
                payload = self._dash_once(ctx, base, ori, ua, tvid, vid, bid)
            except SiteUnavailable as exc:
                reasons.append(f"{name}：{exc}")
                continue
            self._preferred = base
            self._route = name
            return payload
        raise SiteUnavailable(
            f"爱奇艺所有取流通路都失败（tvid={tvid}，bid={bid}）：" + "；".join(reasons)
        )
    @staticmethod
    def _bid_entries(payload: dict) -> dict:
        """按 bid 归并档位，同一 bid 有多条音轨时留体积最大的那条。"""
        result: dict[int, dict] = {}
        program = payload.get("program") or {}
        for item in program.get("video") or []:
            if not isinstance(item, dict):
                continue
            bid = _int(item.get("bid"))
            if bid <= 0:
                continue
            current = result.get(bid)
            if current is None or _int(item.get("vsize")) > _int(current.get("vsize")):
                result[bid] = item
        if not result:
            raise SiteUnavailable(
                "爱奇艺播放接口里没有 program.video 档位：可能是地区限制、"
                "版权下架或需要登录"
            )
        return result

    def _resolve(self, ctx, base: str, path: str) -> str:
        """fs[].l 只是路径，向 dd 主机换一次真实 CDN 直链。"""
        url = (base or DD_URL).rstrip("/") + path
        try:
            data = ctx.net.get_json(url, headers={
                "User-Agent": DESKTOP_UA,
                "Accept": "application/json, */*",
            }, referer=REFERER, retries=2, timeout=20)
        except Exception as exc:
            raise SiteUnavailable(f"爱奇艺分片地址解析失败：{str(exc)[:120]}") from None
        real = (data or {}).get("l") if isinstance(data, dict) else ""
        if not real:
            raise SiteUnavailable("爱奇艺没有为这个分片返回可用地址（可能是版权或地区限制）")
        return real

    # ---------------- 元数据 ----------------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        tvid = self._tvid(ref)
        url = f"{PVI_URL}?tvid={tvid}"
        try:
            raw = ctx.net.get_json(url, headers={
                "User-Agent": DESKTOP_UA,
                "Accept": "application/json, */*",
            }, referer=REFERER, retries=2, timeout=20)
        except Exception as exc:
            raise SiteUnavailable(f"爱奇艺元数据接口请求失败：{str(exc)[:120]}") from None
        if not isinstance(raw, dict) or raw.get("code") != "A00000":
            code = (raw or {}).get("code") if isinstance(raw, dict) else "?"
            raise SiteUnavailable(f"爱奇艺元数据接口返回 {code}，tvid={tvid}")
        data = raw.get("data")
        if not isinstance(data, dict) or not data.get("vn"):
            raise SiteUnavailable(f"爱奇艺没有返回 tvid={tvid} 的信息，可能已下架")

        title = (data.get("vn") or "").strip()
        duration = _int(data.get("plg"))
        channel = _int(data.get("cid"))
        published = str(data.get("issueTime") or data.get("ty") or "")
        pubdate = 0
        if len(published) == 8 and published.isdigit():
            try:
                pubdate = int(time.mktime(time.strptime(published, "%Y%m%d")))
            except ValueError:
                pubdate = 0

        info = VideoInfo(
            title=title,
            cover=data.get("vpic") or data.get("apic") or "",
            desc=data.get("info") or "",
            duration=duration,
            pubdate=pubdate,
            tname=CHANNELS.get(channel, f"频道{channel}" if channel else ""),
            owner=Owner(),
            pages=[Page(cid=tvid, page=1, part=title, duration=duration)],
            stat={},
            kind="video",
            extra={
                "tvid": tvid,
                "vid": (ref.extra or {}).get("vid") or "",
                "detail_route": "pcw-api.iqiyi.com/video/video/playervideoinfo",
                "albumId": _int(data.get("albumId") or data.get("aid")),
                "channelId": channel,
                "payMark": _int(data.get("payMark")),
                "vipType": data.get("vipType") or [],
                "bossStatus": _int(data.get("bossStatus")),
                "url": data.get("vu") or "",
            },
        )
        info.site = self.id
        return info

    # ---------------- 播放地址 ----------------
    def _dash_selected(self, ctx, tvid: int, vid: str, bid: int):
        """请求某一档，返回 (完整 payload, 档位表, 实际下发的那一档)。

        关键：接口会把你请求的 bid 悄悄截断到匿名身份允许的上限，
        并把真正下发的那条标成 _selected，只有它带 fs 分片表。
        所以永远以 _selected 为准，不能自己按 bid 猜。
        """
        payload = self._dash(ctx, tvid, vid, bid)
        entries = self._bid_entries(payload)
        selected = None
        for item in (payload.get("program") or {}).get("video") or []:
            if isinstance(item, dict) and item.get("_selected"):
                selected = item
                break
        if selected is None or not selected.get("fs"):
            # 没有 _selected 标记时，退回到「带分片表且体积最大」的那一条
            with_fs = [v for v in ((payload.get("program") or {}).get("video") or [])
                       if isinstance(v, dict) and v.get("fs")]
            if with_fs:
                selected = max(with_fs, key=lambda v: _int(v.get("vsize")))
        if selected is None or not selected.get("fs"):
            raise SiteUnavailable(
                "爱奇艺没有下发分片表（fs 为空）：会员专享、DRM 加密、"
                "版权下架或地区限制的内容都是这种表现"
            )
        return payload, entries, selected

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        tvid = self._tvid(ref)
        vid = (ref.extra or {}).get("vid") or ""

        # 先按 600（超清 1080P）探一次：接口会截断到匿名可用的上限，
        # 同时这一次响应就把全部档位表带回来了
        requested = 600
        if qn:
            target = estimate_height(qn)
            if target:
                requested = min(BID_HEIGHTS, key=lambda b: abs(BID_HEIGHTS[b] - target))
        payload, entries, selected = self._dash_selected(ctx, tvid, vid, requested)
        granted = _int(selected.get("bid"))

        # 探到的上限如果低于片源最高档（比如片源有蓝光 800），再按最高档要一次
        if not qn:
            best = max(entries)
            if granted < best:
                payload, entries, selected = self._dash_selected(ctx, tvid, vid, best)
                granted = _int(selected.get("bid"))

        base = payload.get("dd") or DD_URL
        segments = selected.get("fs") or []
        short_side = _short_side(selected, granted)
        label = BID_LABELS.get(granted) or f"bid{granted}"
        seconds = _int(selected.get("duration"))
        total = _int(selected.get("vsize")) or sum(_int(s.get("b")) for s in segments)

        play = PlayInfo(engine=self.id, engine_name=self.name, duration=seconds)
        for index, segment in enumerate(segments):
            path = segment.get("l")
            if not path:
                continue
            play.durl.append(Durl(
                order=index + 1,
                url=self._resolve(ctx, base, path),
                size=_int(segment.get("b")),
                length=int(_int(segment.get("d")) / 1000),
            ))
        if not play.durl:
            raise SiteUnavailable("爱奇艺返回的分片表里没有一条可用地址")

        # qn 用平台自己的档位编号换算，保证和 accept_desc 说的是同一档；
        # 档位未知时才退回用实测像素反推
        nominal = BID_HEIGHTS.get(granted) or short_side
        play.accept_quality = [rank_from_resolution(nominal, nominal)] if nominal else [granted]
        play.accept_desc = [label]
        play.is_dash = False
        play.raw = {
            "tvid": tvid, "bid": granted, "scrsz": selected.get("scrsz") or "",
            "vsize": total, "segments": len(play.durl), "route": self._route,
            "available_bids": sorted(entries),
        }

        boss = payload.get("boss") or {}
        preview = _int(boss.get("previewTime"))
        boss_data = boss.get("data") if isinstance(boss.get("data"), dict) else {}
        notes = [f"爱奇艺 dash 接口（通路：{self._route or '未知'}；bid={granted} / {label}），"
                 f"音视频封装在同一分片里，共 {len(play.durl)} 段，按顺序拼接即可"]
        if granted < requested:
            notes.append(f"请求的是 {BID_LABELS.get(requested, requested)}，"
                         f"未登录身份只下发到 {label}")
        if preview > 0 or boss_data.get("prv") == "1":
            notes.append(f"该片是会员内容，未登录只下发前 {preview or '若干'} 分钟试看片段")
        if page and page > 1:
            notes.append(f"爱奇艺单集视频没有分 P，已忽略 page={page}")
        play.note = "；".join(notes)
        return play
