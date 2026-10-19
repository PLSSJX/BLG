#!/usr/bin/env python3
r"""把 dist 里的成品装到本机，并**重建快捷方式**。

用法：
    python install.py                 # 装到 %LOCALAPPDATA%\Programs\MakiseToolkit
    python install.py --dir 某路径     # 装到指定目录
    python install.py --no-shortcut   # 只复制，不建快捷方式

每次更新完都跑这个。复制用 robocopy /MIR（会同步删掉旧文件），
但 data/ 和 downloads/ 被 /XD 排除，用户配置和下载内容不会被卷掉。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "MakiseToolkit"


def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def find_built() -> Path | None:
    onedir = ROOT / "dist" / APP_NAME
    if (onedir / (APP_NAME + ".exe")).exists():
        return onedir
    onefile = ROOT / "dist" / (APP_NAME + ".exe")
    if onefile.exists():
        return ROOT / "dist"
    return None


def copy_tree(src: Path, dst: Path) -> int:
    """robocopy /MIR，排除 data 和 downloads。退出码 0-7 都算成功。"""
    dst.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["robocopy", str(src), str(dst), "/MIR", "/XD", "data", "downloads",
         "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 MakiseToolkit 并重建快捷方式")
    parser.add_argument("--dir", help="安装目录")
    parser.add_argument("--no-shortcut", action="store_true", help="不重建快捷方式")
    args = parser.parse_args()

    built = find_built()
    if not built:
        print("找不到打包产物。先运行：python build.py --onedir --windowed --with-ytdlp --with-webview2")
        return 1
    target_dir = Path(args.dir).expanduser() if args.dir else default_dir()

    print("源目录  ：" + str(built))
    print("安装目录：" + str(target_dir))
    code = copy_tree(built, target_dir)
    if code > 7:
        print("复制失败（robocopy 退出码 %d）" % code)
        return 1
    exe = target_dir / (APP_NAME + ".exe")
    if not exe.exists():
        print("复制完了但没找到 " + exe.name)
        return 1
    print("已复制。")

    if args.no_shortcut:
        return 0
    # 每次更新都重建：旧快捷方式可能指向别处，或者已经被删了
    result = subprocess.run([sys.executable, str(ROOT / "create_shortcut.py"),
                             "--target", str(exe), "--start-menu"])
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
