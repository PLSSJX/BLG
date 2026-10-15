"""把用户输入的 BV 号 / av 号 / 链接 / 番剧链接统一解析成 Ref。"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_EP_RE = re.compile(r"ep(\d+)", re.IGNORECASE)
_SS_RE = re.compile(r"ss(\d+)", re.IGNORECASE)
_AID_RE = re.compile(r"/video/av(\d+)", re.IGNORECASE)


@dataclass
class Ref:
    raw: str = ""
    site: str = "bilibili"       # 平台 id，见 videograb/sites/
    ident: str = ""              # 非 B 平台的主标识（aweme_id / note_id / vid / tvid …）
    extra: dict = field(default_factory=dict)   # 平台自己的附加字段
    kind: str = "video"          # video | bangumi
    bvid: str = ""
    aid: int = 0
    ep_id: int = 0
    season_id: int = 0
    p: int = 1
    short_url: str = ""
    note: str = ""

    def key(self) -> str:
        """这一集的唯一键，用于任务去重。"""
        if self.site != "bilibili":
            return f"{self.site}:{self.ident}_p{self.p}"
        if self.kind == "bangumi":
            return f"ep{self.ep_id}"
        ident = self.bvid or f"av{self.aid}"
        return f"{ident}_p{self.p}"

    def display(self) -> str:
        if self.site != "bilibili":
            return self.ident or self.raw
        if self.kind == "bangumi":
            return f"ep{self.ep_id}"
        return self.bvid or (f"av{self.aid}" if self.aid else self.raw)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw, "site": self.site, "ident": self.ident,
            "kind": self.kind, "bvid": self.bvid, "aid": self.aid,
            "ep_id": self.ep_id, "season_id": self.season_id, "p": self.p,
            "short_url": self.short_url, "note": self.note,
        }


def _page_from_query(query: dict) -> int:
    for key in ("p", "page", "index"):
        vals = query.get(key)
        if vals:
            try:
                return max(1, int(vals[0]))
            except (TypeError, ValueError):
                continue
    return 1


def parse_ref(text: str) -> Ref:
    """纯字符串解析，不发网络请求。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请输入视频 BV 号、av 号、番剧链接或完整网址")

    ref = Ref(raw=raw, p=1)

    # 纯号码
    if _BV_RE.fullmatch(raw):
        ref.bvid = raw
        return ref

    m = _AV_RE.fullmatch(raw)
    if m:
        ref.aid = int(m.group(1))
        return ref

    m = _EP_RE.fullmatch(raw)
    if m:
        ref.kind, ref.ep_id = "bangumi", int(m.group(1))
        return ref

    m = _SS_RE.fullmatch(raw)
    if m:
        ref.kind, ref.season_id = "bangumi", int(m.group(1))
        return ref

    # 网址
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    query = urllib.parse.parse_qs(parsed.query)
    ref.p = _page_from_query(query)

    if host.endswith("b23.tv") or host.endswith("bili2233.cn"):
        ref.short_url = raw
        return ref

    path = parsed.path or ""

    m = _BV_RE.search(path)
    if m:
        ref.bvid = m.group(0)
        return ref

    m = _AID_RE.search(path) or _AV_RE.search(path)
    if m:
        ref.aid = int(m.group(1))
        return ref

    if "/bangumi/play/" in path:
        ref.kind = "bangumi"
        m = _EP_RE.search(path)
        if m:
            ref.ep_id = int(m.group(1))
            return ref
        m = _SS_RE.search(path)
        if m:
            ref.season_id = int(m.group(1))
            return ref

    # 番剧页有时用 media/md 号
    m = re.search(r"/bangumi/media/md(\d+)", path)
    if m:
        ref.kind, ref.season_id = "bangumi", int(m.group(1))
        return ref

    raise ValueError(f"无法从输入中识别视频：{text[:80]}")


def resolve_short(ctx, ref: Ref) -> Ref:
    """跟随 b23.tv 短链跳转，把真实地址再解析一次。"""
    if not ref.short_url:
        return ref
    try:
        res = ctx.net.request(ref.short_url, method="GET", retries=2)
        final = res.url
    except Exception as exc:  # 短链失败不必让整个流程挂掉
        ref.note = f"短链解析失败：{exc}"
        return ref
    try:
        resolved = parse_ref(final)
    except ValueError:
        ref.note = f"短链跳转到无法识别的位置：{final[:120]}"
        return ref
    resolved.raw = ref.raw
    resolved.short_url = ref.short_url
    return resolved


def normalize(ctx, ref: Ref) -> Ref:
    """做一次短链展开 + av/BV 互转补全。"""
    ref = resolve_short(ctx, ref)
    if ref.kind == "video" and ref.aid and not ref.bvid:
        try:
            data = ctx.net.api(
                f"https://api.bilibili.com/x/web-interface/view?aid={ref.aid}"
            )
            ref.bvid = data.get("bvid") or ""
            ref.aid = int(data.get("aid") or ref.aid)
            if not ref.p:
                ref.p = 1
        except Exception:
            pass
    return ref
