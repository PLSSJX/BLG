"""平台注册表：识别输入属于哪个平台，并把请求转给对应的适配器。"""
from __future__ import annotations

from ..refs import Ref
from .base import SiteAdapter, SiteUnavailable, make_info, single_file_play

_SITES: list[SiteAdapter] | None = None


def _build() -> list[SiteAdapter]:
    from .bilibili import BilibiliSite
    from .douyin import DouyinSite
    from .iqiyi import IqiyiSite
    from .kugou import KugouSite
    from .kuwo import KuwoSite
    from .mgtv import MgtvSite
    from .qqmusic import QQMusicSite
    from .vqq import VqqSite
    from .wangyi import NeteaseSite
    from .xiaohongshu import XiaohongshuSite
    from .youku import YoukuSite

    # 顺序即探测优先级。B 站放第一个，因为 BV 号这类纯 ID 最容易被别的规则误判；
    # 音乐平台放在最后，它们的输入多以域名区分，不会和视频平台抢。
    return [
        BilibiliSite(),
        DouyinSite(),
        XiaohongshuSite(),
        MgtvSite(),
        VqqSite(),
        YoukuSite(),
        IqiyiSite(),
        NeteaseSite(),
        QQMusicSite(),
        KugouSite(),
        KuwoSite(),
    ]


def all_sites() -> list[SiteAdapter]:
    global _SITES
    if _SITES is None:
        _SITES = _build()
    return _SITES


def get_site(site_id: str) -> SiteAdapter:
    for site in all_sites():
        if site.id == site_id:
            return site
    raise KeyError("未知平台：" + str(site_id))


def detect(text: str) -> SiteAdapter | None:
    """按注册顺序询问每个平台能不能处理这段输入。"""
    for site in all_sites():
        try:
            if site.match(text):
                return site
        except Exception:
            continue
    return None


def parse_any(text: str) -> Ref:
    """把用户输入解析成带 site 字段的 Ref。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("请输入视频地址或分享链接")
    site = detect(text)
    if site is None:
        raise ValueError(
            "无法识别的链接。目前支持：" +
            "、".join(s.name for s in all_sites())
        )
    ref = site.parse(text)
    ref.site = site.id
    return ref


def describe() -> list[dict]:
    return [site.to_dict() for site in all_sites()]
