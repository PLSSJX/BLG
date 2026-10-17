"""Video-Grabing 安装程序。

用 PyInstaller 打包成单个 exe，内部以 payload.zip 的形式携带整个应用。
安装过程：解包 → 创建快捷方式 → 注册到「应用和功能」→ 写入卸载程序。

支持静默安装：Video-Grabing-Setup-x.y.z.exe /S /D=<安装目录>
"""
from __future__ import annotations

import json
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

    # 盘符根目录直接拒绝。装到 D:\ 意味着程序文件会和整个盘的内容混在一起，
    # 卸载/覆盖安装时的任何一次误判都可能造成大面积数据丢失 ——
    # 这种路径没有任何正当理由，不如直接拦住。
    # 经过 resolve_target() 之后这里基本不会命中；留着是最后一道保险。
    if is_drive_root(target):
        raise RuntimeError(
            "不能把程序安装到盘符根目录（" + str(target) + "）。\n"
            "请选一个单独的文件夹，程序会在里面自动建一个 " + NEST_NAME + " 子目录。")

    target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(payload) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        # 解包前先检查会不会盖掉用户自己的文件
        clash = conflicting_files(target, members)
        if clash:
            preview = "、".join(clash[:5]) + ("…" if len(clash) > 5 else "")
            raise RuntimeError(
                "安装目录里有 " + str(len(clash)) + " 个同名文件会被覆盖，已中止安装：\n"
                + preview + "\n\n请换一个空文件夹，或用默认安装位置。")
        total = len(members)
        for index, member in enumerate(members, start=1):
            archive.extract(member, target)
            if progress and (index % 20 == 0 or index == total):
                progress(index, total, member.filename)

    if uninstaller and uninstaller.exists():
        shutil.copy2(uninstaller, target / W.UNINSTALL_NAME)

    # 记下"这次到底装了哪些文件"。卸载程序只删清单里的东西 ——
    # 用户如果把这个程序装进了自己的目录（比如 D:\Tools），
    # 卸载时绝不能把目录里原有的文件一起带走。
    try:
        listed = sorted({m.filename.replace("\\", "/") for m in members})
        listed.append(W.UNINSTALL_NAME)
        (target / W.MANIFEST_NAME).write_text(
            json.dumps({"files": listed, "version": APP_VERSION}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        pass

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


# 安装目录里"可能属于本程序"的名字。uninstall_existing 只允许动这些，
# 其余一律不碰 —— 用户如果把安装路径选成了 D:\ 这种目录，
# 以前会把里面的东西全部递归删掉。
APP_OWNED_NAMES = {W.EXE_NAME, W.UNINSTALL_NAME, W.MANIFEST_NAME,
                   "_internal", "web", "update", "bin", ".deps"}
# 这两个是用户数据，覆盖安装时保留
USER_KEEP_NAMES = {"data", "downloads"}


NEST_NAME = "VGPlatform"          # 程序永远装在这个子文件夹里
APP_FOLDER_NAMES = {"videograb", "video-grabing", "vgplatform"}


def resolve_target(selected: Path) -> Path:
    """把用户选的目录收敛成真正的安装目录。

    防呆设计：**永远装到 <选中目录>/VGPlatform 里面**。这样程序始终有一块
    属于自己的文件夹，不可能和用户的文件混在一起 —— 删除、覆盖、卸载
    这些操作的波及范围从结构上就被限死了。

    三种例外（原样使用，不再套一层）：
      1. 选中的目录本身就叫 VGPlatform / Video-Grabing（用户已经选到这一层）
      2. 那里已经是一份现有安装（升级、自动更新传 /D=<当前目录>）
    """
    selected = Path(selected).expanduser()
    name = selected.name.strip().lower()
    if name in APP_FOLDER_NAMES or name == NEST_NAME.lower():
        return selected
    if looks_like_install(selected):
        return selected
    return selected / NEST_NAME


def is_drive_root(path: Path) -> bool:
    r"""是不是盘符根目录（D: 这种）。装到盘根既不合理又极其危险。"""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved.parent == resolved


def looks_like_install(target: Path) -> bool:
    """目录里有没有本程序的痕迹。没有就说明这块地方不是我们的。"""
    for name in (W.EXE_NAME, W.UNINSTALL_NAME, W.MANIFEST_NAME):
        if (target / name).exists():
            return True
    return (target / "_internal").is_dir()


def _installed_names(target: Path) -> set | None:
    """读安装时写下的文件清单。拿不到返回 None（老版本装的）。"""
    path = target / W.MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = data.get("files")
        return set(names) if isinstance(names, list) else None
    except (OSError, ValueError):
        return None


def conflicting_files(target: Path, members) -> list:
    r"""找出解包时会被**覆盖**的、不属于本程序的文件。

    装到一个已有内容的文件夹里时（比如把路径选成 D:\Tools），
    payload 里的文件会直接盖掉同名的用户文件。这里先把冲突列出来，
    让安装中止而不是悄悄覆盖。
    """
    names = _installed_names(target) or set()
    bad: list = []
    for member in members:
        rel = str(member.filename).replace("\\", "/")
        if rel.endswith("/"):
            continue
        dest = target / rel
        if not dest.is_file():
            continue
        top = rel.split("/")[0]
        if rel in names or top in APP_OWNED_NAMES:
            continue          # 是我们自己的文件（覆盖安装 / 升级），正常
        bad.append(rel)
    return bad


def preview_removal(target: Path) -> list:
    """列出"覆盖安装"会删掉哪些文件（不真的删）。给安装界面做确认用。"""
    if not target.exists() or not target.is_dir():
        return []
    if is_drive_root(target) or not looks_like_install(target):
        return []
    names = _installed_names(target)
    doomed: list = []
    for item in target.iterdir():
        if item.name in USER_KEEP_NAMES:
            continue
        if names is not None:
            if item.is_dir():
                prefix = item.name + "/"
                allowed = any(n.startswith(prefix) for n in names)
            else:
                allowed = item.name in names
        else:
            allowed = item.name in APP_OWNED_NAMES
        if allowed:
            doomed.append(str(item))
    return doomed


def uninstall_existing(target: Path) -> list:
    """覆盖安装前清掉**旧版本的程序文件**，返回被保留的名字。

    安全约束（这是本文件里最容易出事的地方）：
      1. 有 installed-files.json 就只删清单里列过的文件；
      2. 没有清单时只认 APP_OWNED_NAMES 里的固定名字；
      3. 除此之外的任何东西 —— 不管看起来像不像我们的 —— 一律保留。
    删之前还会再确认一次目录"像是本程序的安装位置"。
    """
    kept: list = []
    if not target.exists() or not target.is_dir():
        return kept
    # 盘根一律不碰。install() 里也会拒绝，但本函数在它之前就被调用了，
    # 所以这道拦截必须放在这里 —— 否则"拒绝安装"之前东西已经被删掉了。
    if is_drive_root(target):
        return [p.name for p in target.iterdir()]
    if not looks_like_install(target):
        # 这儿不是我们的地盘，什么都别动
        return [p.name for p in target.iterdir()]

    names = _installed_names(target)
    for item in target.iterdir():
        if item.name in USER_KEEP_NAMES:
            continue
        # 目录要按前缀匹配：清单里记的是 "_internal/x.dll" 这样的具体文件，
        # 而这里遍历到的是 "_internal" 这个目录名本身。
        if names is not None:
            if item.is_dir():
                prefix = item.name + "/"
                allowed = any(n.startswith(prefix) for n in names)
            else:
                allowed = item.name in names
        else:
            allowed = item.name in APP_OWNED_NAMES
        if not allowed:
            kept.append(item.name)
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except OSError:
            continue
    return kept


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
    confirmed = [False]   # 用户是否已经确认过"清理旧版本文件"

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

    # 防呆：选中的目录下面会自动建一个 VGPlatform 子目录，程序装在里面。
    # 这里实时把它显示出来，避免用户以为程序会散落到自己选的目录里。
    actual_var = tk.StringVar()
    ttk.Label(frame, textvariable=actual_var, foreground="#0a7", wraplength=500).pack(anchor="w")

    def refresh_actual(*_args) -> None:
        try:
            resolved = resolve_target(Path(target_var.get() or "."))
            actual_var.set("将安装到：" + str(resolved) +
                           ("" if resolved.name.lower() == NEST_NAME.lower()
                            else "（已选到程序目录，不再套一层）"))
        except Exception as exc:
            actual_var.set("路径无效：" + str(exc)[:60])

    target_var.trace_add("write", refresh_actual)
    refresh_actual()

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
        target = resolve_target(Path(target_var.get()).expanduser())
        try:
            if W.app_is_running():
                root.after(0, lambda: status_var.set("检测到程序正在运行，正在关闭…"))
                W.kill_running_app()
            # 删除是不可撤销的：先把具体要删的路径列出来，用户点了"继续"才动手
            doomed = preview_removal(target)
            if doomed and not confirmed[0]:
                preview = "\n".join("· " + p for p in doomed[:12])
                if len(doomed) > 12:
                    preview += "\n…（共 " + str(len(doomed)) + " 项）"
                if not messagebox.askokcancel(
                        "确认清理旧版本",
                        "安装前需要先删掉下面 " + str(len(doomed)) + " 项旧版本文件：\n\n"
                        + preview
                        + "\n\n" + str(NEST_NAME) + " 目录以外的文件不会被删除。"):
                    status_var.set("已取消")
                    return
                confirmed[0] = True

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
            target = resolve_target(target)
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
