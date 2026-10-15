"""跨通道统一的元数据与播放流模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .quality import codec_text, human_size, short as q_short


@dataclass
class Page:
    cid: int = 0
    page: int = 1
    part: str = ""
    duration: int = 0
    from_ep: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Owner:
    mid: int = 0
    name: str = ""
    face: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VideoInfo:
    bvid: str = ""
    aid: int = 0
    title: str = ""
    cover: str = ""
    desc: str = ""
    duration: int = 0
    pubdate: int = 0
    tname: str = ""
    owner: Owner = field(default_factory=Owner)
    pages: list = field(default_factory=list)
    stat: dict = field(default_factory=dict)
    engine: str = ""
    kind: str = "video"
    extra: dict = field(default_factory=dict)

    def page_by_index(self, index: int) -> Page | None:
        for page in self.pages:
            if page.page == index:
                return page
        return None

    @property
    def cid(self) -> int:
        first = self.pages[0] if self.pages else None
        return first.cid if first else 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["owner"] = self.owner.to_dict()
        data["pages"] = [p.to_dict() for p in self.pages]
        data["cid"] = self.cid
        data["duration_text"] = format_duration(self.duration)
        return data


@dataclass
class Stream:
    id: int = 0
    qn: int = 0
    label: str = ""
    width: int = 0
    height: int = 0
    codecs: str = ""
    bandwidth: int = 0
    size: int = 0          # 接口给出的真实字节数，没有就是 0
    size_hint: int = 0     # 用 码率 × 时长 估出来的大小，只用于界面显示
    url: str = ""
    backups: list = field(default_factory=list)
    mime: str = ""
    fps: float = 0.0
    audio_quality: int = 0
    kind: str = "video"          # video | audio
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "qn": self.qn, "label": self.label or q_short(self.qn),
            "width": self.width, "height": self.height,
            "resolution": f"{self.width}x{self.height}" if self.width else "-",
            "codecs": self.codecs, "codec_text": codec_text(self.codecs),
            "bandwidth": self.bandwidth, "size": self.size,
            "size_hint": self.size_hint,
            "size_estimated": not self.size and bool(self.size_hint),
            "size_text": ("约 " if not self.size and self.size_hint else "")
                         + human_size(self.size or self.size_hint),
            "mime": self.mime,
            "fps": self.fps, "kind": self.kind, "note": self.note,
            "url": self.url, "has_backup": bool(self.backups),
        }


@dataclass
class Durl:
    order: int = 1
    url: str = ""
    size: int = 0
    length: int = 0
    backups: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "order": self.order, "url": self.url, "size": self.size,
            "size_text": human_size(self.size), "length": self.length,
            "has_backup": bool(self.backups),
        }


@dataclass
class PlayInfo:
    engine: str = ""
    engine_name: str = ""
    is_dash: bool = False
    videos: list = field(default_factory=list)
    audios: list = field(default_factory=list)
    durl: list = field(default_factory=list)
    accept_quality: list = field(default_factory=list)   # 实际下发到的画质
    accept_desc: list = field(default_factory=list)
    declared_quality: list = field(default_factory=list)  # 接口声称支持的画质
    phantom_quality: list = field(default_factory=list)   # 声称支持但实际没下发
    support_formats: list = field(default_factory=list)
    duration: int = 0
    note: str = ""
    raw: dict = field(default_factory=dict)

    def best_height(self) -> int:
        """这个结果实际能拿到的最高画面高度，用于跨通道比画质。"""
        heights = [v.height for v in self.videos if v.height]
        if heights:
            return max(heights)
        if self.durl:
            from .quality import estimate_height
            qns = list(self.accept_quality) or [0]
            return max(estimate_height(q) for q in qns)
        return 0

    def best_video(self) -> Stream | None:
        return max(self.videos, key=lambda s: (s.height, s.bandwidth)) if self.videos else None

    def best_audio(self) -> Stream | None:
        return max(self.audios, key=lambda s: s.bandwidth) if self.audios else None

    def video_by_qn(self, qn: int) -> Stream | None:
        same = [v for v in self.videos if v.qn == qn]
        pool = same or self.videos
        return max(pool, key=lambda s: (s.height, s.bandwidth)) if pool else None

    def to_dict(self, include_urls: bool = True) -> dict:
        def s(item):
            d = item.to_dict()
            if not include_urls:
                d.pop("url", None)
            return d
        return {
            "engine": self.engine,
            "engine_name": self.engine_name,
            "is_dash": self.is_dash,
            "videos": [s(v) for v in self.videos],
            "audios": [s(a) for a in self.audios],
            "durl": [d.to_dict() for d in self.durl],
            "accept_quality": self.accept_quality,
            "accept_desc": self.accept_desc,
            "declared_quality": self.declared_quality,
            "phantom_quality": self.phantom_quality,
            "support_formats": self.support_formats,
            "duration": self.duration,
            "note": self.note,
        }


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "00:00"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
