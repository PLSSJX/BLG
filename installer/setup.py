"""Video-Grabing 安装程序。

用 PyInstaller 打包成单个 exe，内部以 payload.zip 的形式携带整个应用。
安装过程：解包 → 创建快捷方式 → 注册到「应用和功能」→ 写入卸载程序。

支持静默安装：Video-Grabing-Setup-x.y.z.exe /S /D=<安装目录>
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winutil as W  # noqa: E402

# 版本号从包里读，避免和 biligrab/videograb 的 __version__ 各写一份
def _version() -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for candidate in (base / "VERSION.txt", Path(__file__).resolve().parent.parent / "videograb" / "__init__.py"):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        import re as _re
        match = _re.search(r'__version__s*=s*"([^"]+)"', text)
        if match:
            return match.group(1)
        if text.strip():
            return text.strip()
    return "1.1.0"


APP_VERSION = _version()
APP_NAME = W.APP_NAME


def payload_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "payload.zip"


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


# ---------------------------------------------------------------- 静默模式
def parse_silent(argv: list[str]) -> tuple[bool, Path]:
    silent = any(a.lower() in ("/s", "--silent", "-s") for a in argv)
    target = W.default_install_dir()
    for index, arg in enumerate(argv):
        if arg.lower().startswith("/d="):
            target = Path(arg[3:].strip('"'))
        elif arg.upper() == "/D" and index + 1 < len(argv):
            target = Path(argv[index + 1].strip('"'))
    return silent, target


def install(target: Path, progress=None, make_shortcuts: bool = True,
            uninstaller: Path | None = None) -> dict:
    """把 payload 解包到 target 并完成收尾工作。"""
    payload = payload_path()
    if not payload.exists():
        raise RuntimeError("安装包内部缺少 payload.zip，请重新下载安装程序")

    target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(payload) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        total = len(members)
        for index, member in enumerate(members, start=1):
            archive.extract(member, target)
            if progress and (index % 20 == 0 or index == total):
                progress(index, total, member.filename)

    if uninstaller and uninstaller.exists():
        shutil.copy2(uninstaller, target / W.UNINSTALL_NAME)

    exe = target / W.EXE_NAME
    created = []
    if make_shortcuts:
        desktop = W.desktop_dir() / (APP_NAME + ".lnk")
        start = W.programs_dir() / (APP_NAME + ".lnk")
        if W.create_shortcut(desktop, exe, "Video-Grabing —— 多平台视频采集器"):
            created.append(str(desktop))
        if W.create_shortcut(start, exe, "Video-Grabing —— 多平台视频采集器"):
            created.append(str(start))

    W.register_uninstall(target, APP_VERSION, W.directory_size_kb(target))
    return {"install_dir": str(target), "shortcuts": created, "exe": str(exe)}


def uninstall_existing(target: Path) -> None:
    """覆盖安装前先清掉旧版本的程序文件，但保留 data/ 与 downloads/。"""
    if not target.exists():
        return
    keep = {"data", "downloads", "bin", ".deps", "web"}   # web/ 是自检修复的覆盖层
    for item in target.iterdir():
        if item.name in keep:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except OSError:
            continue


# ---------------------------------------------------------------- 图形界面
def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME + " 安装程序")
    root.geometry("560x400")
    root.resizable(False, False)
    try:
        icon = resource_dir() / "app.ico"
        if icon.exists():
            root.iconbitmap(default=str(icon))
    except Exception:
        pass

    target_var = tk.StringVar(value=str(W.default_install_dir()))
    shortcut_var = tk.BooleanVar(value=True)
    launch_var = tk.BooleanVar(value=True)
    status_var = tk.StringVar(value="准备就绪。点「开始安装」继续。")

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text=APP_NAME, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frame, text="版本 " + APP_VERSION + "　·　十条抓取通道 · 自选清晰度 · 多线程下载",
              foreground="#666").pack(anchor="w", pady=(2, 16))

    ttk.Label(frame, text="安装位置").pack(anchor="w")
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(4, 12))
    entry = ttk.Entry(row, textvariable=target_var)
    entry.pack(side="left", fill="x", expand=True)

    def browse() -> None:
        chosen = filedialog.askdirectory(initialdir=target_var.get(), title="选择安装位置")
        if chosen:
            target_var.set(chosen)

    ttk.Button(row, text="浏览…", command=browse).pack(side="left", padx=(8, 0))

    ttk.Checkbutton(frame, text="创建桌面和开始菜单快捷方式",
                    variable=shortcut_var).pack(anchor="w")
    ttk.Checkbutton(frame, text="安装完成后立即启动",
                    variable=launch_var).pack(anchor="w", pady=(2, 12))

    bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
    bar.pack(fill="x", pady=(6, 6))
    ttk.Label(frame, textvariable=status_var, foreground="#444",
              wraplength=500).pack(anchor="w")

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", side="bottom", pady=(16, 0))
    install_btn = ttk.Button(buttons, text="开始安装")
    install_btn.pack(side="right")
    ttk.Button(buttons, text="退出",
               command=root.destroy).pack(side="right", padx=(0, 8))

    def worker() -> None:
        target = Path(target_var.get()).expanduser()
        try:
            if W.app_is_running():
                root.after(0, lambda: status_var.set("检测到程序正在运行，正在关闭…"))
                W.kill_running_app()
            root.after(0, lambda: status_var.set("正在清理旧版本文件…"))
            uninstall_existing(target)

            def on_progress(index: int, total: int, name: str) -> None:
                percent = index * 100 // max(1, total)
                root.after(0, lambda: (
                    bar.configure(value=percent),
                    status_var.set(f"正在安装 {index}/{total} …"),
                ))

            result = install(target, on_progress, shortcut_var.get(),
                             resource_dir() / "uninstall.exe")
        except Exception as exc:  # 安装失败要让用户看见
            root.after(0, lambda: (
                status_var.set("安装失败：" + str(exc)),
                messagebox.showerror("安装失败", str(exc)),
                install_btn.configure(state="normal"),
            ))
            return

        root.after(0, lambda: (
            bar.configure(value=100),
            status_var.set("安装完成。"),
            install_btn.configure(state="normal"),
        ))
        root.after(0, lambda: messagebox.showinfo(
            "安装完成",
            APP_NAME + " 已安装到：\n" + result["install_dir"] +
            ("\n\n已创建 " + str(len(result["shortcuts"])) + " 个快捷方式。" if result["shortcuts"] else "")))
        if launch_var.get():
            W.launch(Path(result["exe"]))
        root.after(300, root.destroy)

    def start() -> None:
        install_btn.configure(state="disabled")
        bar.configure(value=0)
        status_var.set("正在准备…")
        threading.Thread(target=worker, daemon=True).start()

    install_btn.configure(command=start)
    root.mainloop()
    return 0


def main() -> int:
    silent, target = parse_silent(sys.argv[1:])
    if silent:
        try:
            if W.app_is_running():
                W.kill_running_app()
            uninstall_existing(target)
            result = install(target, None, True, resource_dir() / "uninstall.exe")
            print("installed to " + result["install_dir"])
            return 0
        except Exception as exc:
            print("install failed: " + str(exc), file=sys.stderr)
            return 1
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
