"""PyInstaller 打包入口。

和 start.py 的区别：这里不做 sys.path 操作，直接按普通包导入 ——
PyInstaller 的静态分析最喜欢这种写法，能自动把 biligrab 整个包收集进去。

另外这里还要处理"没有控制台"这件事：桌面版是 windowed（无控制台）构建，
PyInstaller 会把 sys.stdout 直接设成 None，任何 print 都会炸。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

LOG_MAX_BYTES = 1024 * 1024


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def attach_parent_console() -> bool:
    """windowed 版从命令行启动时，把输出接回父进程的控制台。

    双击启动时父进程是 explorer.exe，没有控制台，AttachConsole 会直接失败，
    这正是我们想要的 —— 双击时不弹黑框，命令行调用时照样能看到输出。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        return True
    except Exception:
        return False


def redirect_io_to_log() -> None:
    """没有控制台时把 print 导向日志文件，顺便给用户留一份可排查的记录。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = base_dir() / "data" / "BiliGrab.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > LOG_MAX_BYTES:
            log_path.replace(log_path.with_suffix(".log.old"))
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        stream.write("\n" + "=" * 60 + "\n")
    except OSError:
        import io
        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def show_error(message: str) -> None:
    """桌面版出错时至少要让用户看见，而不是默默什么都不发生。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "哔哩采集器 BiliGrab", 0x10)
    except Exception:
        pass


def main() -> int:
    from biligrab.__main__ import main as cli_main

    argv = sys.argv[1:]
    # 双击 exe 时没有参数，默认启动界面；带参数则原样交给命令行
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv
    return cli_main(argv)


if __name__ == "__main__":
    attach_parent_console()
    redirect_io_to_log()
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        last = [line for line in detail.strip().splitlines() if line.strip()][-1]
        show_error("程序启动失败：\n\n" + last +
                   "\n\n完整日志：\n" + str(base_dir() / "data" / "BiliGrab.log"))
        sys.exit(1)
