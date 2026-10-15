"""平台适配层。

每个平台一个适配器，对外只暴露四件事：识别输入、解析成 Ref、取元数据、取播放地址。
上层（任务调度、界面）因此不需要知道任何平台细节。

B 站那边因为历史原因拆成了 10 条抓取通道，这里用一个薄适配器把它们包起来，
让七个平台在接口上完全平等。
"""
from __future__ import annotations

from ..models import PlayInfo, VideoInfo
from ..refs import Ref


class SiteUnavailable(Exception):
    """平台当前取不到内容：风控、需要登录、接口变更、地区限制等。"""


class SiteAdapter:
    id = "base"
    name = "未知平台"
    home = ""
    color = "#888888"
    examples: list = []
    # 该平台是否必须登录/会员才能拿到完整内容
    needs_login = False
    note = ""
    # 该平台需要的登录凭据。每一项会在设置页单独渲染成一个区块：
    #   key      凭据标识
    #   field    存在 Settings 里的字段名
    #   label    界面上显示的名字
    #   purpose  这个值能换来什么
    #   howto    分步获取说明（列表）
    #   tip      可选的小技巧
    #   warning  可选的风险提示
    #   secret   是否按密码处理（不回显原文）
    # 不需要任何凭据的平台留空列表，设置页会把它们列进"无需登录"。
    credentials: list = []

    # ---------- 识别与解析 ----------
    def match(self, text: str) -> bool:
        """这一段输入是不是本平台的。"""
        raise NotImplementedError

    def parse(self, text: str) -> Ref:
        """纯字符串解析（不发网络请求）。"""
        raise NotImplementedError

    def normalize(self, ctx, ref: Ref) -> Ref:
        """需要联网的补全：短链展开、ID 互转等。默认什么都不做。"""
        return ref

    # ---------- 取内容 ----------
    def fetch_info(self, ctx, ref: Ref) -> VideoInfo:
        raise NotImplementedError

    def fetch_play(self, ctx, ref: Ref, page: int = 1, qn: int = 0) -> PlayInfo:
        raise NotImplementedError

    # ---------- 给界面用 ----------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "home": self.home,
            "color": self.color,
            "examples": list(self.examples),
            "needs_login": self.needs_login,
            "note": self.note,
            "credentials": list(self.credentials),
        }


def make_info(ref: Ref, **fields) -> VideoInfo:
    """统一填好 site / engine 字段，避免每个适配器各写一遍。"""
    info = VideoInfo(**fields)
    info.site = ref.site
    return info


def single_file_play(adapter: SiteAdapter, url: str, size: int = 0,
                     duration: int = 0, quality: str = "原始画质",
                     note: str = "") -> PlayInfo:
    """很多平台的成品就是一个直链 MP4，用它快速构造 PlayInfo。"""
    from ..models import Durl

    play = PlayInfo(engine=adapter.id, engine_name=adapter.name, duration=duration)
    play.durl = [Durl(order=1, url=url, size=size, length=duration)]
    play.accept_quality = [0]
    play.accept_desc = [quality]
    play.note = note or "单文件直链，免合并"
    return play
