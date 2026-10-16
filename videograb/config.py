"""配置持久化与全局上下文。"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


# 打包成 exe 之后，程序被拆成两个位置，这一点必须分清楚：
#   RESOURCE_DIR —— PyInstaller 解包出来的只读资源（web/ 界面文件在这里）
#   BASE_DIR     —— exe 自己所在的目录
# data/ 与 downloads/ 一定要放在 BASE_DIR：放进解包目录的话，
# 单文件 exe 每次退出都会把用户配置和下载记录一起删掉。
if is_frozen():
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = Path(__file__).resolve().parent.parent
    BASE_DIR = RESOURCE_DIR

ROOT = BASE_DIR
DATA_DIR = BASE_DIR / "data"
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "downloads"


def _resolve_web_dir() -> Path:
    """界面文件目录，优先用 exe 同级的 web/ 覆盖层。

    打包后 web/ 被封在只读的解包里，改不了 —— 而"自检修复"需要能替换
    损坏的界面文件。所以只要 exe 旁边存在一份完整的 web/，就用它，
    修复程序把文件下到这里即可生效。
    """
    overlay = BASE_DIR / "web"
    if (overlay / "index.html").exists():
        return overlay
    return RESOURCE_DIR / "web"


WEB_DIR = _resolve_web_dir()
CONFIG_PATH = DATA_DIR / "config.json"
BIN_DIR = BASE_DIR / "bin"          # 随包发布的 ffmpeg 等可执行文件
DEPS_DIR = BASE_DIR / ".deps"       # 一键安装的 yt-dlp 等 Python 依赖

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Kazumi 自带的九种主题色，一一对应
THEME_PRESETS = [
    {"label": "默认", "color": "#4CAF50"},
    {"label": "青色", "color": "#009688"},
    {"label": "蓝色", "color": "#2196F3"},
    {"label": "靛蓝色", "color": "#3F51B5"},
    {"label": "紫罗兰色", "color": "#6750A4"},
    {"label": "粉红色", "color": "#E91E63"},
    {"label": "黄色", "color": "#FFEB3B"},
    {"label": "橙色", "color": "#FF9800"},
    {"label": "深橙色", "color": "#FF5722"},
]

# 顺序即默认优先级：先官方接口，再网页抓取，最后是当前失效但保留待恢复的通道
ALL_ENGINES = [
    "web_wbi",          # 1  Web WBI 签名接口
    "web_durl",         # 2  单文件直取（fnval=1，未登录下画质最高）
    "app_api",          # 3  移动端 APP 签名接口
    "html_scrape",      # 4  网页源码内嵌数据 + defaultWbiKey
    "html5_durl",       # 5  HTML5 移动端参数组合
    "web_legacy",       # 6  Web 经典免签名兜底
    "pgc_bangumi",      # 7  番剧 / 影视 PGC 接口
    "thirdparty_jx",    # 8  第三方解析聚合（实测已失效，且会把视频地址发给第三方，默认关闭）
    "tv_api",           # 9  云视听 TV 端（实测已失效）
    "external_tool",    # 10 外部下载器（需另行安装）
]

# 默认不启用的通道。
#
# thirdparty_jx 会把用户正在解析的 B 站地址发给 7 个互不相干的第三方解析服务
# （api.injahow.cn / api.xingzhige.com / api.52vmy.cn / api.pearktrue.cn /
#   jx.xmflv.com / tenapi.cn / api.cenguigui.cn）。而这些服务实测**已经全部失效** ——
# 也就是说它一分收益都没有，却会把"你在看什么"泄露出去。默认关掉，
# 想要的人可以在设置里自己打开（那里也写明了这一点）。
DEFAULT_DISABLED = {"thirdparty_jx"}
DEFAULT_ENABLED = [e for e in ALL_ENGINES if e not in DEFAULT_DISABLED]


@dataclass
class Settings:
    download_dir: str = ""          # 留空 = exe 同级的 downloads 目录
    # 登录态：填了才能解锁 1080P 及以上
    sessdata: str = ""
    bili_jct: str = ""
    # 音乐平台的登录凭据。填了就解锁更高音质，不填也能下标准档。
    ncm_cookie: str = ""       # 网易云音乐
    qqmusic_cookie: str = ""   # QQ音乐
    kugou_cookie: str = ""     # 酷狗音乐
    kuwo_cookie: str = ""      # 酷我音乐
    dede_user_id: str = ""
    buvid3: str = ""
    proxy: str = ""
    # 下载
    threads_per_task: int = 8
    max_parallel_tasks: int = 2
    prefer_quality: int = 0          # 0 表示"最高可用"
    prefer_codec: str = "auto"       # auto | h264 | hevc | av1
    auto_merge: bool = True
    keep_streams: bool = False       # 合并后保留原始音视频分片
    filename_template: str = "{title}"
    # 附加产物
    write_danmaku: bool = True
    write_cover: bool = True
    write_subtitle: bool = False
    write_nfo: bool = False
    # 通道
    enabled_engines: list = field(default_factory=lambda: list(DEFAULT_ENABLED))
    # 一次性隐私迁移标记：见 _migrate_privacy()
    privacy_migrated: bool = False
    engine_order: list = field(default_factory=lambda: list(ALL_ENGINES))
    # 界面（默认值与 Kazumi 一致：动态取色 + 跟随系统 + 绿色种子）
    theme_seed: str = "#4CAF50"
    theme_mode: str = "dark"         # system | light | dark | oled
    dynamic_color: bool = True
    # 网络
    user_agent: str = DEFAULT_UA
    request_timeout: int = 20
    referer: str = "https://www.bilibili.com/"

    def ordered_engines(self) -> list:
        order = [e for e in self.engine_order if e in ALL_ENGINES]
        for e in ALL_ENGINES:
            if e not in order:
                order.append(e)
        return [e for e in order if e in self.enabled_engines]

    def quality_choice(self) -> int:
        try:
            return int(self.prefer_quality)
        except (TypeError, ValueError):
            return 0


class Context:
    """贯穿整个进程的共享状态：配置 + HTTP 客户端 + 任务表。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self._lock = threading.RLock()
        self._net = None
        self.tasks = None       # 由 tasks.py 注入，避免循环导入
        self.on_quit = None     # 桌面窗口模式下用来关掉窗口
        # 必须在初始化时就存在：纯服务模式（--no-browser）根本不会走到开窗逻辑，
        # 少了这个默认值，后面读 ctx.window_mode 会直接抛 AttributeError。
        self.window_mode = ""
        # 本机 API 的写操作令牌。浏览器里任何网页都能向 127.0.0.1 发请求，
        # 没有这道校验的话，跨站页面就能改配置、偷走 SESSDATA。
        import secrets as _secrets
        self.api_token = _secrets.token_urlsafe(24)

    @property
    def net(self):
        if self._net is None:
            from .net import HttpClient
            self._net = HttpClient(self)
        return self._net

    def reset_net(self) -> None:
        with self._lock:
            self._net = None

    def save(self) -> None:
        with self._lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(asdict(self.settings), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, CONFIG_PATH)

    def update(self, patch: dict) -> Settings:
        """就地更新配置，忽略未知字段。"""
        valid = set(asdict(self.settings).keys())
        for key, value in (patch or {}).items():
            if key in valid:
                setattr(self.settings, key, value)
        _normalize(self.settings)
        if any(k in (patch or {}) for k in
               ("sessdata", "bili_jct", "proxy", "user_agent",
                "ncm_cookie", "qqmusic_cookie", "kugou_cookie", "kuwo_cookie")):
            self.reset_net()
        self.save()
        return self.settings

    def download_dir(self) -> Path:
        """解析下载目录。配置为空就用默认位置；旧配置指向的位置已经不存在
        （比如 exe 被挪到了别处）时也回落到默认位置，而不是凭空造一串目录出来。"""
        configured = (self.settings.download_dir or "").strip()
        if configured:
            candidate = Path(configured)
            if candidate.exists() or candidate.parent.exists():
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
        DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        return DEFAULT_DOWNLOAD_DIR


# 数值型配置的合法区间。写坏一个值（手改 config.json、接口传字符串/列表）
# 以前会让调度线程每次都抛异常并被静默吞掉，表现是"队列只涨不跑、没有任何报错"。
_NUMERIC_FIELDS = {
    "threads_per_task": (1, 32, 8),
    "max_parallel_tasks": (1, 8, 2),
    "request_timeout": (5, 300, 30),
    "chunk_size_mb": (1, 64, 4),
    "retry_times": (0, 10, 3),
}


def _migrate_privacy(settings: Settings) -> Settings:
    """一次性隐私迁移。

    早期版本的 enabled_engines 默认是"全部通道"，其中包含会向第三方外发地址的
    thirdparty_jx。这里把它从已有配置里摘掉一次；用户之后仍可在设置里重新打开
    （设置页对该通道有明确的风险说明）。
    """
    if getattr(settings, "privacy_migrated", False):
        return settings
    enabled = list(getattr(settings, "enabled_engines", None) or [])
    if enabled:
        settings.enabled_engines = [e for e in enabled if e not in DEFAULT_DISABLED]
    settings.privacy_migrated = True
    return settings


def _normalize(settings: Settings) -> Settings:
    """把数值字段夹到合法区间；转不动的就回落到默认值。"""
    for name, (low, high, default) in _NUMERIC_FIELDS.items():
        if not hasattr(settings, name):
            continue
        raw = getattr(settings, name)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        setattr(settings, name, max(low, min(high, value)))
    return _migrate_privacy(settings)


def load_settings() -> Settings:
    if not CONFIG_PATH.exists():
        return Settings()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    valid = set(Settings().__dict__.keys())
    clean = {k: v for k, v in raw.items() if k in valid}
    try:
        return _normalize(Settings(**clean))
    except TypeError:
        return Settings()
