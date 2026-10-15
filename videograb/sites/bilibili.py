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
    needs_login = False   # 不填也能用，只是画质受限
    credentials = [
        {
            "key": "sessdata",
            "field": "sessdata",
            "label": "SESSDATA",
            "secret": True,
            "purpose": "解锁 1080P 及以上画质。不填时 DASH 最高 480P、单文件通道最高 720P。",
            "howto": [
                "用浏览器打开 bilibili.com 并<strong>登录</strong>你的账号",
                "按 <code>F12</code> 打开开发者工具（或右键页面 → 检查）",
                "切换到 <strong>Application</strong> 标签页（中文版叫「应用」或「应用程序」）",
                "左侧展开 <strong>Cookies</strong>，点选 <code>https://www.bilibili.com</code>",
                "在右侧列表里找到 <code>SESSDATA</code> 那一行，双击 Value 列并整行复制",
                "粘贴到下面的输入框，点「保存」，再点「校验」确认登录态有效",
            ],
            "tip": "也可以在开发者工具的 Console 里执行 "
                   "<code>document.cookie.match(/SESSDATA=([^;]+)/)[1]</code> 直接把值打印出来。",
            "warning": "这个值等同于你的登录凭证，只保存在本机的 data/config.json，不会上传到任何地方。"
                       "请不要分享给别人；在 B 站退出登录后它会立即失效。",
        },
        {
            "key": "bili_jct",
            "field": "bili_jct",
            "label": "bili_jct",
            "secret": True,
            "purpose": "可选。这是 B 站的 CSRF token，本程序的下载功能并不依赖它，留着以备后续需要写操作的接口。",
            "howto": ["获取方式与 SESSDATA 完全相同，在同一个 Cookie 列表里找名为 bili_jct 的那一行"],
            "tip": "",
            "warning": "",
        },
    ]

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
