"""B 站清晰度（qn）码表与画质判定工具。"""
from __future__ import annotations

# qn -> (中文名, 短标签, 排序权重)
QUALITY_TABLE: dict[int, tuple[str, str, int]] = {
    127: ("8K 超高清", "8K", 130),
    126: ("杜比视界", "Dolby", 128),
    125: ("HDR 真彩", "HDR", 126),
    120: ("4K 超清", "4K", 124),
    116: ("1080P60 高帧率", "1080P60", 118),
    112: ("1080P+ 高码率", "1080P+", 114),
    100: ("智能修复", "AI", 110),
    80: ("1080P 高清", "1080P", 100),
    74: ("720P60 高帧率", "720P60", 90),
    64: ("720P 高清", "720P", 80),
    32: ("480P 清晰", "480P", 60),
    16: ("360P 流畅", "360P", 40),
    6: ("240P 极速", "240P", 20),
}

# qn -> 大致画面高度，用于 durl（接口不给分辨率）时估算画质
_QN_HEIGHT = {
    127: 4320, 126: 2160, 125: 2160, 120: 2160,
    116: 1080, 112: 1080, 100: 1080, 80: 1080,
    74: 720, 64: 720, 32: 480, 16: 360, 6: 240,
}

# 登录 / 大会员才可能解锁的画质，用于界面上的"锁"标记
VIP_QUALITIES = {127, 126, 125, 120, 116, 112, 100, 74}
LOGIN_QUALITIES = {80, 74, 64}


def label(qn: int) -> str:
    return QUALITY_TABLE.get(qn, (f"未知({qn})", str(qn), 0))[0]


def short(qn: int) -> str:
    return QUALITY_TABLE.get(qn, (f"qn{qn}", str(qn), 0))[1]


def weight(qn: int) -> int:
    return QUALITY_TABLE.get(qn, ("", "", 0))[2]


def estimate_height(qn: int) -> int:
    return _QN_HEIGHT.get(int(qn or 0), 0)


def needs_login(qn: int) -> bool:
    return qn in VIP_QUALITIES or qn in LOGIN_QUALITIES


def needs_vip(qn: int) -> bool:
    return qn in VIP_QUALITIES


def best(qns) -> int:
    """从一组 qn 中挑出最高画质。"""
    qns = [int(q) for q in qns if q]
    if not qns:
        return 0
    weights = [weight(q) for q in qns]
    if not any(weights):
        # 音乐平台的档位是 1..4（1=标准 … 4=Hi-Res），weight() 只认 B 站的 qn 码，
        # 全为 0 时 max() 会退化成"取第一个"，也就是永远选最低档。
        return max(qns)
    return max(qns, key=weight)


def rank_from_resolution(width: int, height: int, fps: float = 0.0) -> int:
    """当接口只给了分辨率时，反推一个近似 qn，用于跨通道比较。"""
    if height >= 4320:
        return 127
    if height >= 2160:
        return 120
    if height >= 1440:
        return 112
    if height >= 1080:
        return 116 if fps and fps >= 50 else 112
    if height >= 720:
        return 74 if fps and fps >= 50 else 64
    if height >= 480:
        return 32
    if height >= 360:
        return 16
    return 6


def resolution_text(stream: dict) -> str:
    w = int(stream.get("width") or 0)
    h = int(stream.get("height") or 0)
    if not w or not h:
        return "-"
    return f"{w}x{h}"


def human_size(num: float | int | None) -> str:
    if not num:
        return "-"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024
    return "-"


def codec_text(codecs: str | None) -> str:
    """把 avc1.640028 / hev1.1.6.L120.90 这类 codec 串翻译成人话。"""
    if not codecs:
        return "未知"
    c = codecs.lower()
    if c.startswith("avc"):
        return "H.264 / AVC"
    if c.startswith("hev") or c.startswith("hvc"):
        return "H.265 / HEVC"
    if c.startswith("av01"):
        return "AV1"
    if c.startswith("mp4a"):
        return "AAC"
    if c.startswith("ec-3"):
        return "E-AC-3 杜比"
    if c.startswith("flac"):
        return "FLAC 无损"
    return codecs


# 编码偏好 -> 匹配函数。B 站的 DASH 里同一档画质会同时给 HEVC / AVC / AV1 三个版本。
CODEC_PREFERENCES = [
    {"id": "auto", "name": "自动（优先 H.264，兼容性最好）"},
    {"id": "h264", "name": "只要 H.264 / AVC"},
    {"id": "hevc", "name": "优先 H.265 / HEVC（体积更小）"},
    {"id": "av1", "name": "优先 AV1（体积最小，解码要求高）"},
]

_CODEC_PRIORITY = {
    "auto": ("avc", "hev", "hvc", "av01"),
    "h264": ("avc", "hev", "hvc", "av01"),
    "hevc": ("hev", "hvc", "avc", "av01"),
    "av1": ("av01", "hev", "hvc", "avc"),
}


def codec_rank(codecs: str | None, preference: str = "auto") -> int:
    """越小越优先。"""
    order = _CODEC_PRIORITY.get(preference or "auto", _CODEC_PRIORITY["auto"])
    text = (codecs or "").lower()
    for index, prefix in enumerate(order):
        if text.startswith(prefix):
            return index
    return len(order)
