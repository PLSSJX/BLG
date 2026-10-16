"""Video-Grabing 卸载程序。

被安装到程序目录下，和主程序放在一起。运行时以自身所在目录为安装目录，
删掉程序文件、快捷方式和注册表项；用户可以选择是否一并删除下载的视频和配置。

最后一步是自删除：进程没法删掉正在运行中的自己，所以交给一个延迟执行的
cmd 去把剩下的目录清掉。
"""
from __future__ import annotations

import json
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


# 没有清单时（老版本装的）只认这些名字，其余一律不动
LEGACY_KNOWN = {W.EXE_NAME, W.UNINSTALL_NAME, "_internal", "data", "downloads",
                "bin", ".deps", "web", "update", "installed-files.json"}


def installed_names(target: Path) -> set | None:
    """安装时记下的文件清单。拿不到就返回 None，走保守路径。"""
    path = target / W.MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = data.get("files")
        return set(names) if isinstance(names, list) else None
    except (OSError, ValueError):
        return None


def _listed(item: Path, names: set) -> bool:
    if item.is_file():
        return item.name in names
    prefix = item.name + "/"
    return any(n.startswith(prefix) for n in names)


def remove_all(target: Path, keep_user_data: bool) -> tuple[list, list, list]:
    """删除程序自己装的东西。

    返回 (已删除, 失败, 已保留)。**关键安全约束**：只删安装时记录过的文件；
    没有清单时只认已知的目录名。用户自己的文件一律保留并报告出来 ——
    以前这里是"遍历目录、见什么删什么"，如果程序被装进了用户的目录
    （例如把安装路径选成 D:\Tools 而不是 D:\Tools\Video-Grabing），
    卸载会把那个目录里原有的文件全部删掉。
    """
    removed, failed, kept = [], [], []
    if not target.exists():
        return removed, failed, kept

    names = installed_names(target)

    if not keep_user_data:
        for name in USER_DATA:
            if names is not None and not any(n.startswith(name + "/") for n in names):
                continue          # 清单里没有这个目录，不动
            shutil.rmtree(target / name, ignore_errors=True)

    for item in sorted(target.iterdir()):
        if item.name == W.UNINSTALL_NAME:
            continue                      # 自己最后再删
        if item.name == W.MANIFEST_NAME:
            try:
                item.unlink()
                removed.append(item.name)
            except OSError:
                pass
            continue
        allowed = _listed(item, names) if names is not None else (item.name in LEGACY_KNOWN)
        if not allowed:
            kept.append(item.name)        # 不是我们装的，留着
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed.append(item.name)
        except OSError as exc:
            failed.append(item.name + "（" + str(exc)[:50] + "）")
    return removed, failed, kept


def remove_shortcuts() -> None:
    for path in (W.desktop_dir() / (APP_NAME + ".lnk"),
                 W.programs_dir() / (APP_NAME + ".lnk")):
        W.remove_file(path)


def schedule_self_delete(target: Path) -> None:
    """延迟两秒后把剩下的一切删干净，包括正在运行的自己。"""
    # 用不带 /s 的 rmdir：它**只能删空目录**，非空就会失败。
    # 以前是 rmdir /s /q，等于递归清空 —— 万一目录里还有用户的东西就一起没了。
    script = 'ping 127.0.0.1 -n 3 >nul & rmdir /q "' + str(target) + '"'
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
    removed, failed, kept = remove_all(target, keep_user_data)
    print("removed " + str(len(removed)) + " items, " + str(len(failed)) + " failed, "
          + str(len(kept)) + " kept")
    for name in kept:
        print("kept (not installed by us): " + name)
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
        removed, failed, kept = remove_all(target, keep_var.get())
        messagebox.showinfo(
            "卸载完成",
            "已删除 " + str(len(removed)) + " 项。" +
            ("\n\n有 " + str(len(failed)) + " 项删不掉，可能仍被占用。" if failed else "") +
            ("\n\n下载的视频和配置已保留。" if keep_var.get() else "") +
            # 只删自己装的东西；目录里原有的文件会留在这里并告诉用户
            ("\n\n以下 " + str(len(kept)) + " 项不是本程序安装的，已替你保留：\n"
             + "、".join(kept[:8]) + ("…" if len(kept) > 8 else "") if kept else ""))
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
