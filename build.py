#!/usr/bin/env python3
"""把 Video-Grabing 连同 Python 运行时、界面资源和可选工具打成一个独立 exe。

用法：
    python build.py                    # 单文件便携版（推荐分发）
    python build.py --onedir           # 目录版，启动更快，便于替换 ffmpeg
    python build.py --all              # 两种都打
    python build.py --with-ffmpeg      # 额外内置 ffmpeg（约 +80MB，合并更稳）
    python build.py --with-ytdlp       # 额外内置 yt-dlp（解锁第 10 条通道）

不需要预装 PyInstaller —— 脚本会自己建一个构建用的虚拟环境。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "build-tools"
VENV = ROOT / ".build-venv"
DIST = ROOT / "dist"
DEPS = ROOT / ".deps"
BIN = ROOT / "bin"

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# 这些模块在打包版里用不到，排掉能省些体积。
# 注意别把 http.server 依赖的东西排掉 —— email 是解析 HTTP 头用的，
# 少了它程序连启动都做不到。
EXCLUDES = [
    # 注意不要排除 distutils：pythonnet / clr_loader 在分析阶段会 import 它，
    # 一旦被标记为 ExcludedModule，PyInstaller 会直接报错退出。
    "tkinter", "unittest", "pydoc_data", "test", "lib2to3",
    "curses", "numpy", "pandas", "PIL", "matplotlib", "setuptools", "pip",
    "doctest", "pdb", "bdb", "profile", "cProfile", "pstats", "turtle",
    "wave", "audioop", "sunau", "aifc", "chunk", "imghdr", "sndhdr",
]

# 只有真正带窗口的构建才需要 tkinter；安装程序自己那份构建会用到它
WINDOWED_EXCLUDES: list[str] = []


def log(message: str) -> None:
    print("  " + message, flush=True)


def step(title: str) -> None:
    print("\n== " + title + " ==", flush=True)


# ------------------------------------------------------------------ 图标
def make_icon() -> Path | None:
    """用 Pillow 画一个应用图标并导出多尺寸 .ico。

    只用基本图形（圆角矩形 + 播放三角），不依赖任何字体或 SVG 渲染器，
    因此在纯净环境里也能稳定复现。
    """
    target = TOOLS / "app.ico"
    if target.exists():
        return target
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log("没有 Pillow，跳过图标生成（exe 会用系统默认图标）")
        return None

    TOOLS.mkdir(parents=True, exist_ok=True)
    size = 512
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 竖向渐变底：取 Material 3 绿色种子的 primary / primaryContainer 色调
    top, bottom = (0x6A, 0xD8, 0x6E), (0x1B, 0x5E, 0x20)
    layer = Image.new("RGBA", (size, size))
    pixels = layer.load()
    for y in range(size):
        ratio = y / (size - 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        for x in range(size):
            pixels[x, y] = (*row, 255)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=112, fill=255)
    image.paste(layer, (0, 0), mask)

    # 白色播放三角
    cx, cy, r = size // 2, size // 2, 128
    draw.polygon(
        [(cx - r + 34, cy - r), (cx - r + 34, cy + r), (cx + r + 10, cy)],
        fill=(255, 255, 255, 255),
    )
    # 左下角一点"下载"暗示：一条白色短横线
    draw.rounded_rectangle([size // 2 - 120, size - 150, size // 2 + 120, size - 122],
                           radius=14, fill=(255, 255, 255, 235))

    image.save(target, format="ICO",
               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    log("图标已生成：" + str(target))
    return target


# ------------------------------------------------------------------ 构建环境
def ensure_venv() -> Path:
    python = VENV / "Scripts" / "python.exe"
    if not python.exists():
        log("创建构建用虚拟环境 .build-venv …")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    check = subprocess.run([str(python), "-c", "import PyInstaller"],
                           capture_output=True)
    if check.returncode != 0:
        log("安装 PyInstaller …")
        subprocess.run([str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-q", "pyinstaller"], check=True)
    return python


def ensure_ytdlp(python: Path) -> bool:
    """把 yt-dlp 装进 .deps，让 PyInstaller 一起收进 exe。"""
    step("准备 yt-dlp")
    DEPS.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(python), "-m", "pip", "install", "-q", "--upgrade",
                    "--target", str(DEPS), "yt-dlp"], check=True)
    ok = (DEPS / "yt_dlp").exists()
    log("yt-dlp " + ("已就绪：" + str(DEPS / "yt_dlp") if ok else "安装失败"))
    return ok


def ensure_ffmpeg() -> bool:
    """下载静态 ffmpeg 并只保留 ffmpeg.exe 放进 bin/。"""
    step("准备 ffmpeg")
    BIN.mkdir(parents=True, exist_ok=True)
    target = BIN / "ffmpeg.exe"
    if target.exists():
        log("已存在：" + str(target))
        return True

    archive = TOOLS / "ffmpeg.zip"
    TOOLS.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        log("下载静态 ffmpeg（约 110MB，只做一次）…")
        request = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=1800) as response, \
                open(archive, "wb") as handle:
            shutil.copyfileobj(response, handle, 1 << 20)
    log("下载完成，解包中 …")
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if name.endswith("/bin/ffmpeg.exe"):
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                break
    if target.exists():
        log("ffmpeg 已就绪：" + str(target) + "（%.0f MB）" % (target.stat().st_size / 1048576))
        return True
    log("压缩包里没找到 ffmpeg.exe")
    return False


# ------------------------------------------------------------------ 打包
def build(python: Path, mode: str, icon: Path | None, with_ytdlp: bool,
          windowed: bool = False) -> Path:
    step("打包 " + mode)
    args = [
        str(python), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "Video-Grabing",
        "--distpath", str(DIST),
        "--workpath", str(TOOLS / "pyinstaller"),
        "--specpath", str(TOOLS),
        # 注意：--add-data 的源路径是相对 spec 文件所在目录解析的，
        # 所以这里必须给绝对路径，否则会去 build-tools/ 里找 web 目录。
        "--add-data", str(ROOT / "web") + ";web",
        # 基线清单要打进包里：断网时也能拿它做自检
        "--add-data", str(ROOT / "update") + ";update",
        "--paths", str(ROOT),
        # 界面资源之外，凡是运行时可能用到的都显式声明
        "--hidden-import", "videograb.engines",
        "--collect-submodules", "videograb",
    ]
    if WEBVIEW2_BOOTSTRAP is not None and WEBVIEW2_BOOTSTRAP.exists():
        # 内置了 WebView2 安装器：放在 exe 同级，缺运行库时能一键安装
        args += ["--add-data", str(WEBVIEW2_BOOTSTRAP) + ";."]
    if mode == "onefile":
        args.append("--onefile")
    if windowed:
        # 无控制台构建：双击不再弹黑框。
        # launcher.py 会在从命令行调用时 AttachConsole 接回父控制台，
        # 所以 windowed 版同样可以用 Video-Grabing.exe probe BV... 这种命令行方式。
        args.append("--windowed")
    if icon:
        args += ["--icon", str(icon)]
    if with_ytdlp:
        args += ["--paths", str(DEPS), "--collect-all", "yt_dlp"]
    if windowed:
        # 独立窗口靠 pywebview + pythonnet 驱动 WebView2，这几包必须完整收进来
        args += [
            "--collect-all", "webview",
            "--collect-all", "clr_loader",
            "--collect-all", "pythonnet",
            "--hidden-import", "clr",
            "--hidden-import", "pythonnet",
            "--copy-metadata", "pywebview",
        ]
    for name in EXCLUDES:
        args += ["--exclude-module", name]
    args.append(str(ROOT / "launcher.py"))

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(DEPS), env.get("PYTHONPATH", "")])
    result = subprocess.run(args, env=env)
    if result.returncode != 0:
        raise SystemExit("打包失败，退出码 " + str(result.returncode))

    target = DIST / "Video-Grabing.exe" if mode == "onefile" else DIST / "Video-Grabing" / "Video-Grabing.exe"
    if not target.exists():
        raise SystemExit("打包结束但没有找到 " + str(target))
    # 目录版把 bin/ 放到 exe 旁边，方便用户自行替换 ffmpeg
    if mode == "onedir" and BIN.exists():
        shutil.copytree(BIN, target.parent / "bin", dirs_exist_ok=True)
    return target


# 内置了 WebView2 安装器时记在这里，build() 会用它加 --add-data
WEBVIEW2_BOOTSTRAP: Path | None = None


def _bootstrap_target(path: Path) -> None:
    global WEBVIEW2_BOOTSTRAP
    WEBVIEW2_BOOTSTRAP = path


def ensure_webview2(full: bool = False) -> Path | None:
    """下载 WebView2 运行时的安装器，放到 dist 里随程序一起分发。

    引导程序（约 1.9MB）会在安装时联网拉取运行时；
    完整离线包（约 204MB）不需要网络。两者都来自微软官方 fwlink。
    """
    import urllib.request

    url = ("https://go.microsoft.com/fwlink/p/?LinkId=2124701" if full
           else "https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    name = "MicrosoftEdgeWebview2Setup.exe"
    target = DIST / name
    if target.exists() and target.stat().st_size > 1_000_000:
        log("WebView2 安装器已存在：" + str(target))
        _bootstrap_target(target)
        return target
    step("下载 WebView2 安装器（%s）" % ("完整离线包" if full else "引导程序"))
    DIST.mkdir(parents=True, exist_ok=True)
    DEPS.mkdir(parents=True, exist_ok=True)
    target = DEPS / name          # 放 .deps 里，打包时用 --add-data 塞进程序目录
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response, open(target, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if total:
                    print("\r  %.1f / %.1f MB" % (done / 1048576, total / 1048576), end="")
        print()
        log("已下载：%s（%.1f MB）" % (target, target.stat().st_size / 1048576))
        _bootstrap_target(target)
        return target
    except Exception as exc:
        log("下载失败（不影响构建）：" + str(exc)[:120])
        return None


def build_installer(python: Path, icon: Path | None, with_ytdlp: bool = True) -> Path:
    """先打应用，再打卸载程序，最后把两者塞进安装程序。

    顺序很重要：payload.zip 里必须已经包含 uninstall.exe，
    否则装完之后「应用和功能」里的卸载按钮会指向一个不存在的文件。
    """
    step("准备安装包内容")
    app_target = build(python, "onedir", icon, with_ytdlp, windowed=True)
    app_dir = app_target.parent

    step("构建卸载程序")
    uninstall_args = [
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "uninstall",
        "--distpath", str(TOOLS / "uninstall-dist"),
        "--workpath", str(TOOLS / "pyinstaller-uninstall"),
        "--specpath", str(TOOLS),
    ]
    if icon:
        uninstall_args += ["--icon", str(icon)]
    uninstall_args.append(str(ROOT / "installer" / "uninstall.py"))
    if subprocess.run(uninstall_args).returncode != 0:
        raise SystemExit("卸载程序构建失败")
    shutil.copy2(TOOLS / "uninstall-dist" / "uninstall.exe", app_dir / "uninstall.exe")
    log("卸载程序已放入程序目录")

    step("打包 payload.zip")
    payload = TOOLS / "payload.zip"
    if payload.exists():
        payload.unlink()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(app_dir.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(app_dir).as_posix())
    log("payload.zip %.1f MB" % (payload.stat().st_size / 1048576))

    step("构建安装程序")
    version = _app_version()
    (TOOLS / "VERSION.txt").write_text(version, encoding="utf-8")
    installer_args = [
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "Video-Grabing-Setup-" + version,
        "--distpath", str(DIST),
        "--workpath", str(TOOLS / "pyinstaller-setup"),
        "--specpath", str(TOOLS),
        "--add-data", str(payload) + ";.",
        "--add-data", str(TOOLS / "VERSION.txt") + ";.",
        "--exclude-module", "numpy", "--exclude-module", "pandas",
        "--exclude-module", "PIL", "--exclude-module", "matplotlib",
    ]
    if icon:
        installer_args += ["--icon", str(icon), "--add-data", str(icon) + ";."]
    installer_args.append(str(ROOT / "installer" / "setup.py"))
    if subprocess.run(installer_args).returncode != 0:
        raise SystemExit("安装程序构建失败")

    target = DIST / ("Video-Grabing-Setup-" + version + ".exe")
    if not target.exists():
        raise SystemExit("没有找到生成的安装程序")

    # 安装程序建好之后重新生成一次清单：这份是要提交到仓库的，
    # 里面的安装包哈希必须指向刚刚产出的这个文件，否则自动更新会校验失败。
    subprocess.run([sys.executable, str(ROOT / "make_manifest.py")], check=True)
    log("清单已按新安装包重新生成")
    return target


def _app_version() -> str:
    sys.path.insert(0, str(ROOT))
    from videograb import __version__
    return __version__


def report(target: Path, mode: str) -> None:
    if mode == "onefile":
        size = target.stat().st_size / 1048576
        log("产物：%s（%.1f MB，单文件，双击即可运行）" % (target, size))
    else:
        total = sum(f.stat().st_size for f in target.parent.rglob("*") if f.is_file())
        log("产物：%s（整个目录 %.1f MB）" % (target, total / 1048576))


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Video-Grabing 独立 exe")
    parser.add_argument("--onefile", action="store_true", help="只打单文件版")
    parser.add_argument("--onedir", action="store_true", help="只打目录版")
    parser.add_argument("--all", action="store_true", help="两种都打")
    parser.add_argument("--with-ffmpeg", action="store_true", help="内置静态 ffmpeg")
    parser.add_argument("--with-ytdlp", action="store_true", help="内置 yt-dlp")
    parser.add_argument("--with-webview2", action="store_true",
                        help="内置 WebView2 引导程序（约 +1.9MB，缺运行库时可一键安装）")
    parser.add_argument("--with-webview2-full", action="store_true",
                        help="内置 WebView2 完整离线包（约 +204MB，无需联网）")
    parser.add_argument("--windowed", action="store_true",
                        help="目录版做成无控制台的桌面应用（双击不弹黑框）")
    parser.add_argument("--installer", action="store_true",
                        help="额外生成 .exe 安装程序（包含应用与卸载程序）")
    args = parser.parse_args()

    modes = []
    if args.all or not (args.onefile or args.onedir):
        modes = ["onefile", "onedir"] if args.all else ["onefile"]
    else:
        if args.onefile:
            modes.append("onefile")
        if args.onedir:
            modes.append("onedir")

    print("Video-Grabing 打包工具")
    print("  输出目录：" + str(DIST))
    print("  打包模式：" + "、".join(modes))

    if not (ROOT / "web" / "index.html").exists():
        raise SystemExit("找不到 web/index.html，请在项目根目录运行本脚本")

    python = ensure_venv()
    icon = make_icon()

    step("生成自检基线清单")
    subprocess.run([sys.executable, str(ROOT / "make_manifest.py")], check=True)

    if args.with_ffmpeg:
        ensure_ffmpeg()
    if args.with_ytdlp:
        ensure_ytdlp(python)
    # 必须排在 build() 之前：build() 靠 WEBVIEW2_BOOTSTRAP 决定要不要 --add-data，
    # 放到后面会导致安装包里有、但 onedir / 单文件版里没有。
    if args.with_webview2 or args.with_webview2_full:
        ensure_webview2(full=args.with_webview2_full)

    for mode in modes:
        # 目录版做成桌面应用（无控制台），单文件版保留控制台便于命令行使用
        windowed = args.windowed
        target = build(python, mode, icon, args.with_ytdlp, windowed=windowed)
        report(target, mode)

    if args.installer:
        # 安装程序内部自己会打一份目录版，这里就不重复打了
        target = build_installer(python, icon, args.with_ytdlp)
        log("安装程序：%s（%.1f MB）" % (target, target.stat().st_size / 1048576))

    print("\n完成。dist 目录里就是可以直接分发的成品。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
