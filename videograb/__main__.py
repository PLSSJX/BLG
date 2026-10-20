"""命令行入口：Web 服务、通道体检、直接下载。"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from .config import Context
from .engines import describe
from .muxer import find_ffmpeg
from .quality import label as qn_label
from .refs import normalize, parse_ref
from .server import build_state, run_probe, serve


def _print_table(rows: list[list[str]], header: list[str]) -> None:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    print(line)
    print("-" * _display_width(line))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def _display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _existing_instance(host: str, port: int) -> str:
    """端口上如果已经跑着一个 MakiseToolkit，返回它的地址，否则返回空串。"""
    url = f"http://{host}:{port}/api/ping"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("app") == "MakiseToolkit":
            return f"http://{host}:{port}/"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass
    return ""


_MUTEX_HANDLE = None


def _single_instance_mutex(name: str = "MakiseToolkit.SingleInstance") -> bool:
    """拿命名互斥量。返回 True 表示本进程是第一个，False 表示已经有实例在跑。

    为什么不用 HTTP ping 判断：对方可能刚起来还没开始监听，
    ping 失败会让第二个实例继续往下走、再开一个 WebView2 窗口，
    而 WebView2 的用户数据目录是独占的 —— 第二个窗口必定初始化失败，
    用户看到的就是一堆空白窗口（0x8007139F）。
    """
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
        if not handle:
            return True                      # 拿不到就放行，别把用户挡在门外
        _MUTEX_HANDLE = handle               # 保持引用，进程活着就一直占着
        return kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _activate_existing(base: str) -> bool:
    """请已经运行的实例把窗口拉到前台。"""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/window/activate",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except Exception:
        return False


def cmd_serve(args) -> int:
    # 必须在创建任何窗口之前声明 DPI 感知，否则 Windows 会把整个窗口位图拉伸，
    # 在非 100% 缩放的机器上界面就是糊的。
    from .dpi import enable as _enable_dpi
    dpi_mode = _enable_dpi()

    ctx = Context()
    if args.port:
        ctx.settings.__dict__["_port"] = args.port

    # 单实例：桌面版双击第二次时不该报"端口被占用"，
    # 直接把已经在跑的那个界面打开就好。
    if not args.allow_multi and not _single_instance_mutex():
        # 已经有实例在跑：等它开始监听（最多 10 秒），然后请它把窗口拿到前台
        for _ in range(20):
            running = _existing_instance(args.host, args.port)
            if running and _activate_existing(running):
                print("  已经有一个 MakiseToolkit 在运行，已切换到它的窗口。")
                return 0
            time.sleep(0.5)
        # 拿不到窗口就不要硬开第二个（WebView2 会初始化失败，只会多一个空白窗口）
        print("  已经有一个 MakiseToolkit 在运行。")
        return 0

    if not args.allow_multi:
        running = _existing_instance(args.host, args.port)
        if running:
            print(f"  程序已经在运行：{running}")
            # 直接让已经在跑的那个窗口跳到前台。
            # 以前这里会再开一个 WebView 窗口，但 WebView2 的用户数据目录是独占的，
            # 第二个窗口必定起不来，用户看到的就是一堆空白窗口。
            if _activate_existing(running):
                print("  已切换到正在运行的窗口。")
                return 0
            # 拉不到窗口就不要硬开第二个：WebView2 的用户数据目录是独占的，
            # 第二个窗口必定初始化失败，用户只会多一个空白窗口。
            print("  已经有一个 MakiseToolkit 在运行。")
            return 0

    httpd = serve(ctx, host=args.host, port=args.port, quiet=not args.verbose)
    url = f"http://{args.host}:{args.port}/"
    print(f"  {build_state(ctx)['app_cn']} 已启动")
    print(f"  界面地址：{url}")
    print(f"  下载目录：{ctx.download_dir()}")
    print(f"  合并方式：{'ffmpeg（' + (find_ffmpeg() or '') + '）' if find_ffmpeg() else '内置纯 Python fMP4 合并器'}")

    # HTTP 服务放到后台线程，主线程留给窗口（pywebview 必须在主线程跑）
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                     name="http-server")
    server_thread.start()

    mode = "server"
    if args.no_browser:
        print("  纯服务模式（未打开任何窗口），按 Ctrl+C 退出\n")
    elif args.browser:
        print("  使用系统浏览器打开，按 Ctrl+C 退出\n")
        webbrowser.open(url)
        mode = "browser"
    else:
        from .desktop import open_window
        print("  正在打开独立窗口…\n")
        mode = open_window(url, ctx)

    if not getattr(ctx, "window_mode", ""):
        ctx.window_mode = mode
    try:
        if mode in ("browser", "server"):
            # 这两种模式没有"窗口关闭"事件，只能等 Ctrl+C 或界面上的退出按钮
            while server_thread.is_alive():
                time.sleep(0.4)
        else:
            # 窗口已经关掉了，直接把服务也停掉
            pass
    except KeyboardInterrupt:
        print("\n  正在退出…")
    finally:
        httpd.shutdown()
    return 0


def cmd_engines(args) -> int:
    ctx = Context()
    rows = []
    for entry in describe(ctx):
        ok = "可用" if entry["available"] else "不可用"
        rows.append([entry["name"], entry["kind"], ok, entry["reason"] or entry["desc"][:40]])
    _print_table(rows, ["通道", "类型", "状态", "说明"])
    return 0


def cmd_probe(args) -> int:
    ctx = Context()
    ctx.tasks = None
    report = run_probe(ctx, args.input, args.qn)
    print(f"\n  目标：{report['identity']}   可用通道 {report['ok_count']}/{report['total']}\n")
    rows = []
    for item in report["results"]:
        rows.append([
            item["name"],
            "成功" if item.get("ok") else "失败",
            item.get("quality") or "-",
            f"{item.get('ms', 0)} ms",
            (item.get("note") or item.get("error") or "")[:48],
        ])
    _print_table(rows, ["通道", "结果", "最高画质", "耗时", "说明"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_download(args) -> int:
    ctx = Context()
    from .tasks import TaskManager
    ctx.tasks = TaskManager(ctx)
    task = ctx.tasks.submit(args.input, engine=args.engine, qn=args.qn, mode=args.mode)
    print(f"  已提交任务 {task.id}，模式 {args.mode}，通道 {args.engine}")
    last = ""
    while True:
        time.sleep(0.5)
        current = ctx.tasks.get(task.id)
        if not current:
            break
        progress = current.progress or {}
        line = (f"  [{current.status}] {current.title[:36]} "
                f"{progress.get('percent', 0):5.1f}%  {progress.get('speed_text', '-')}"
                f"  {progress.get('phase', '')}")
        if line != last:
            print(line)
            last = line
        if current.status in ("done", "error", "cancelled"):
            for entry in current.files:
                print(f"    · {entry['name']}")
            if current.error:
                print(f"    失败原因：{current.error}")
            return 0 if current.status == "done" else 1


def cmd_update(args) -> int:
    from .updater import UpdaterError, check_update, download_installer, repair, verify

    try:
        if args.repair:
            print("  正在比对本地文件与仓库基线 …")
            report = repair(progress=lambda i, n, path: print(f"    [{i}/{n}] {path}"))
            print(f"  更新源：{report['source']}")
            for path in report["repaired"]:
                print(f"    ✓ 已修复 {path}")
            for item in report["failed"]:
                print(f"    ✗ {item['path']}：{item['error']}")
            if not report["targets"]:
                print("  所有文件都与基线一致，无需修复。")
            elif report["need_restart"]:
                print("  修复完成，重启程序后生效。")
            return 0 if report["ok"] else 1

        if args.verify:
            report = verify()
            print(f"  更新源：{report['source']}")
            print(f"  共 {report['checked']} 个文件，正常 {report['ok_count']} 个")
            for item in report["bad"]:
                print(f"    ✗ {item['path']}（{item['detail']}）")
            if report["ok"]:
                print("  自检通过，本地文件与仓库基线一致。")
            return 0 if report["ok"] else 1

        info = check_update()
        print(f"  更新源：{info['source']}")
        print(f"  当前版本：{info['current']}")
        print(f"  最新版本：{info['latest']}")
        if info["has_update"]:
            print("  有新版本可用。")
            if info["notes"]:
                print("  更新说明：" + info["notes"])
            if args.download:
                result = download_installer(info, progress=lambda text: print("  " + text))
                print("  安装包已就绪：" + result["path"])
            else:
                print("  加 --download 可以直接下载安装包，或到发布页手动下载：")
                print("  " + info["release_page"])
        else:
            print("  已经是最新版本。")
        return 0
    except UpdaterError as exc:
        print("  失败：" + str(exc))
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="videograb",
        description="MakiseToolkit —— 八通道 B 站视频采集工具",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动图形界面（默认）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8931)
    p_serve.add_argument("--no-browser", action="store_true",
                         help="纯服务模式：不开窗口也不开浏览器")
    p_serve.add_argument("--browser", action="store_true",
                         help="用系统浏览器打开，而不是内置独立窗口")
    p_serve.add_argument("--verbose", action="store_true", help="打印每个 HTTP 请求")
    p_serve.add_argument("--allow-multi", action="store_true",
                         help="允许同时运行多个实例（默认会复用已运行的实例）")
    p_serve.set_defaults(func=cmd_serve)

    p_engines = sub.add_parser("engines", help="列出全部抓取通道及其可用性")
    p_engines.set_defaults(func=cmd_engines)

    p_probe = sub.add_parser("probe", help="用同一个视频体检所有通道")
    p_probe.add_argument("input", help="BV 号 / av 号 / 视频链接 / 番剧链接")
    p_probe.add_argument("--qn", type=int, default=0)
    p_probe.add_argument("--json", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_update = sub.add_parser("update", help="检查更新与自检修复")
    p_update.add_argument("--verify", action="store_true", help="只做自检，不改动任何文件")
    p_update.add_argument("--repair", action="store_true", help="自检并修复不一致的文件")
    p_update.add_argument("--download", action="store_true", help="有新版本时直接下载安装包")
    p_update.set_defaults(func=cmd_update)

    p_dl = sub.add_parser("download", help="直接下载（不开界面）")
    p_dl.add_argument("input")
    p_dl.add_argument("--engine", default="auto")
    p_dl.add_argument("--qn", type=int, default=0, help="清晰度 qn，0 表示最高可用")
    p_dl.add_argument("--mode", default="auto",
                      choices=["auto", "single", "dash", "audio", "meta"])
    p_dl.set_defaults(func=cmd_download)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
