"""十条抓取通道的注册表。

顺序即默认优先级，也是对同一个视频依次尝试的顺序：
先走最稳的官方接口，再走网页抓取，最后才是已失效但保留待恢复的通道。
"""
from __future__ import annotations

from .base import Engine, EngineUnavailable
from .e1_web_wbi import WebWbiEngine
from .e2_web_legacy import WebLegacyEngine
from .e3_app_api import AppApiEngine
from .e4_tv_api import TvApiEngine
from .e5_html_scrape import HtmlScrapeEngine
from .e6_pgc_bangumi import PgcBangumiEngine
from .e7_thirdparty_jx import ThirdPartyJxEngine
from .e8_external_tool import ExternalToolEngine
from .e9_html5_durl import Html5DurlEngine
from .e10_web_durl import WebDurlEngine

# 实例化顺序 = 默认优先级
ENGINE_CLASSES = [
    WebWbiEngine,        # 1  WBI 签名接口
    WebDurlEngine,       # 2  单文件直取（未登录下画质最高的免费路线）
    AppApiEngine,        # 3  移动端 APP 签名
    HtmlScrapeEngine,    # 4  网页内嵌数据 + defaultWbiKey
    Html5DurlEngine,     # 5  HTML5 移动端参数组合
    WebLegacyEngine,     # 6  Web 经典免签名兜底
    PgcBangumiEngine,    # 7  番剧 / 影视 PGC
    ThirdPartyJxEngine,  # 8  第三方解析聚合（当前失效）
    TvApiEngine,         # 9  云视听 TV（当前失效）
    ExternalToolEngine,  # 10 外部下载器（需另行安装）
]

_BY_ID: dict[str, Engine] = {}


def all_engines() -> list[Engine]:
    if not _BY_ID:
        for cls in ENGINE_CLASSES:
            inst = cls()
            _BY_ID[inst.id] = inst
    return list(_BY_ID.values())


def get_engine(engine_id: str) -> Engine:
    engines = {e.id: e for e in all_engines()}
    if engine_id not in engines:
        raise KeyError(f"未知的抓取通道：{engine_id}")
    return engines[engine_id]


def describe(ctx) -> list[dict]:
    """给界面用的通道清单，附带实时可用性判定。"""
    from ..config import ALL_ENGINES

    order = ctx.settings.ordered_engines()
    result = []
    for index, engine in enumerate(all_engines()):
        ok, reason = engine.available(ctx)
        result.append({
            "id": engine.id,
            "name": engine.name,
            "desc": engine.desc,
            "kind": engine.kind,
            "badge": engine.badge,
            "needs": list(engine.needs),
            "enabled": engine.id in ctx.settings.enabled_engines,
            "available": ok,
            "reason": reason,
            "index": index,
            "order": order.index(engine.id) if engine.id in order else len(ALL_ENGINES),
        })
    result.sort(key=lambda item: (item["order"], item["index"]))
    return result
