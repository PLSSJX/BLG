"""哔哩哔哩适配器：把现有的十条抓取通道包成统一的站点接口。"""
from __future__ import annotations

from ..engines import get_engine
from ..models import PlayInfo, VideoInfo
from ..refs import Ref
from ..refs import parse_ref as parse_bilibili_ref
from ..refs import resolve_short
from .base import SiteAdapter, SiteUnavailable

# 依次尝试的通道顺序（用户可在设置里改，这里只作为兜底）
DEFAULT_CHAIN = ["web_wbi", "web_durl", "app_api", "html_scrape",
                 "html5_durl", "web_legacy", "pgc_bangumi"]


class BilibiliSite(SiteAdapter):
    id = "bilibili"
    name = "哔哩哔哩"
    home = "https://www.bilibili.com"
    color = "#00AEEC"
    examples = [
        "BV1GJ411x7h7",
        "https://www.bilibili.com/video/BV1GJ411x7h7/?p=2",
        "b23.tv 短链",
        "番剧 ep267851 / ss26801",
    ]
    note = "十条独立抓取通道，普通投稿开箱可用 6 条"

    def match(self, text: str) -> bool:
        try:
            parse_bilibili_ref(text)
            return True
        except Exception:
            return False

    def parse(self, text: str) -> Ref:
        return parse_bilibili_ref(text)

    def normalize(self, ctx, ref: Ref) -> Ref:
        return resolve_short(ctx, ref)

    def _chain(self, ctx) -> list[str]:
        try:
            order = ctx.settings.ordered_engines()
        except Exception:
            order = []
        return order or list(DEFAULT_CHAIN)

    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        errors = []
        for engine_id in self._chain(ctx):
            engine = get_engine(engine_id)
            if engine_id == "pgc_bangumi" and ref.kind != "bangumi":
                continue
            ok, reason = engine.available(ctx)
            if not ok:
                continue
            try:
                return engine.fetch_info(ctx, ref)
            except Exception as exc:
                errors.append(f"{engine.name}：{str(exc)[:60]}")
        raise SiteUnavailable("所有通道都没能取到视频信息 —— " + "；".join(errors[:4]))

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        errors = []
        for engine_id in self._chain(ctx):
            engine = get_engine(engine_id)
            if engine_id == "pgc_bangumi" and ref.kind != "bangumi":
                continue
            ok, reason = engine.available(ctx)
            if not ok:
                continue
            try:
                play = engine.fetch_play(ctx, ref, page, qn)
            except Exception as exc:
                errors.append(f"{engine.name}：{str(exc)[:60]}")
                continue
            if play.videos or play.durl:
                return play
        raise SiteUnavailable("所有通道都没能取到播放地址 —— " + "；".join(errors[:4]))
