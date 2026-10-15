"""Video-Grabing 卸载程序。

被安装到程序目录下，和主程序放在一起。运行时以自身所在目录为安装目录，
删掉程序文件、快捷方式和注册表项；用户可以选择是否一并删除下载的视频和配置。

最后一步是自删除：进程没法删掉正在运行中的自己，所以交给一个延迟执行的
cmd 去把剩下的目录清掉。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winutil as W  # noqa: E402

APP_NAME = W.APP_NAME

# 用户数据：默认保留，除非用户明确要一起删
USER_DATA = {"data", "downloads", ".deps"}


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent / "dist" / "Video-Grabing"


def remove_all(target: Path, keep_user_data: bool) -> list[str]:
    removed, failed = [], []
    if not target.exists():
        return removed, failed

    if not keep_user_data:
        for name in USER_DATA:
            shutil.rmtree(target / name, ignore_errors=True)

    for item in sorted(target.iterdir()):
        if item.name == W.UNINSTALL_NAME:
            continue                      # 自己最后再删
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed.append(item.name)
        except OSError as exc:
            failed.append(item.name + "（" + str(exc)[:50] + "）")
    return removed, failed


def remove_shortcuts() -> None:
    for path in (W.desktop_dir() / (APP_NAME + ".lnk"),
                 W.programs_dir() / (APP_NAME + ".lnk")):
        W.remove_file(path)


def schedule_self_delete(target: Path) -> None:
    """延迟两秒后把剩下的一切删干净，包括正在运行的自己。"""
    script = 'ping 127.0.0.1 -n 3 >nul & rmdir /s /q "' + str(target) + '"'
    try:
        subprocess.Popen(
            ["cmd", "/c", script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except OSError:
        pass


def run_silent(keep_user_data: bool = True) -> int:
    target = install_dir()
    if W.app_is_running():
        W.kill_running_app()
    remove_shortcuts()
    W.unregister_uninstall()
    removed, failed = remove_all(target, keep_user_data)
    print("removed " + str(len(removed)) + " items, " + str(len(failed)) + " failed")
    schedule_self_delete(target)
    return 0


def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    target = install_dir()
    root = tk.Tk()
    root.title(APP_NAME + " 卸载")
    root.geometry("520x300")
    root.resizable(False, False)

    keep_var = tk.BooleanVar(value=True)   # 默认保留用户数据，避免误删下载好的视频
    status_var = tk.StringVar(value="安装位置：" + str(target))

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="卸载 " + APP_NAME, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
    ttk.Label(frame, textvariable=status_var, foreground="#666",
              wraplength=470).pack(anchor="w", pady=(4, 16))
    ttk.Checkbutton(frame, text="保留下载的视频和配置（data/ 与 downloads/）",
                    variable=keep_var).pack(anchor="w")
    ttk.Label(frame, text="取消勾选会把这些文件一并删除，且无法恢复。",
              foreground="#a00", wraplength=470).pack(anchor="w", pady=(2, 12))

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", side="bottom")

    def do_remove() -> None:
        if not messagebox.askyesno("确认卸载", "确定要卸载 " + APP_NAME + " 吗？"):
            return
        if W.app_is_running():
            W.kill_running_app()
        remove_shortcuts()
        W.unregister_uninstall()
        removed, failed = remove_all(target, keep_var.get())
        messagebox.showinfo(
            "卸载完成",
            "已删除 " + str(len(removed)) + " 项。" +
            ("\n\n有 " + str(len(failed)) + " 项删不掉，可能仍被占用。" if failed else "") +
            ("\n\n下载的视频和配置已保留。" if keep_var.get() else ""))
        schedule_self_delete(target)
        root.destroy()

    ttk.Button(buttons, text="确认卸载", command=do_remove).pack(side="right")
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="right", padx=(0, 8))
    root.mainloop()
    return 0


def main() -> int:
    if any(a.lower() in ("/s", "--silent", "-s") for a in sys.argv[1:]):
        return run_silent()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
