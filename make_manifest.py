#!/usr/bin/env python3
"""生成 update/manifest.json —— 自检修复用的基线清单。

清单里记录每个文件的 SHA-256，程序启动后或用户点「自检并修复」时会逐个比对，
对不上或缺失的就从更新源重新下载。

用法：
    python make_manifest.py                    # 生成清单
    python make_manifest.py --notes "修了xx"    # 同时写入更新说明
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from videograb import __version__                     # noqa: E402
from videograb.updater import RELEASE_PAGE, REPO      # noqa: E402

MANIFEST = ROOT / "update" / "manifest.json"

# 界面资源：exe 也能修复（会被写到程序目录下的 web/ 覆盖层）
RUNTIME_DIRS = ["web"]
RUNTIME_FILES: list[str] = []

# 源码文件：只有以源码方式运行才校验/修复
SOURCE_FILES = [
    "launcher.py", "start.py", "build.py", "make_manifest.py", "create_shortcut.py",
    "run.bat", "requirements.txt", "README.md",
]
SOURCE_DIRS = ["videograb"]

SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".tmp"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", ".build-venv", ".deps", "dist",
             "build-tools", "data", "downloads", "node_modules"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def collect(paths: list[str], dirs: list[str]) -> list[dict]:
    entries: list[dict] = []
    seen = set()

    def add(file: Path) -> None:
        relative = file.relative_to(ROOT).as_posix()
        if relative in seen:
            return
        if file.suffix.lower() in SKIP_SUFFIX:
            return
        seen.add(relative)
        entries.append({
            "path": relative,
            "sha256": sha256(file),
            "size": file.stat().st_size,
        })

    for name in paths:
        file = ROOT / name
        if file.is_file():
            add(file)
    for name in dirs:
        base = ROOT / name
        if not base.is_dir():
            continue
        for file in sorted(base.rglob("*")):
            if not file.is_file():
                continue
            if any(part in SKIP_DIRS for part in file.relative_to(ROOT).parts):
                continue
            add(file)
    return entries


def find_installer() -> dict:
    """如果 dist 里已经有安装包，就把它的信息一起写进清单。

    发布时要先跑安装包构建、再跑本脚本，这样清单里的哈希才是对的。
    """
    dist = ROOT / "dist"
    if not dist.is_dir():
        return {}
    candidates = sorted(dist.glob("Video-Grabing-Setup-*.exe"))
    if not candidates:
        return {}
    setup = candidates[-1]
    return {
        "name": setup.name,
        "size": setup.stat().st_size,
        "sha256": sha256(setup),
        # 发布资源走 GitHub Release；仓库文件走 jsDelivr（更新源里已处理）
        "url": f"https://github.com/{REPO}/releases/download/v{__version__}/{setup.name}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成自检基线清单")
    parser.add_argument("--notes", default="", help="更新说明，会显示在界面上")
    parser.add_argument("--print", action="store_true", help="只打印不写文件")
    args = parser.parse_args()

    runtime = collect(RUNTIME_FILES, RUNTIME_DIRS)
    source = collect(SOURCE_FILES, SOURCE_DIRS)

    manifest = {
        "version": __version__,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "notes": args.notes,
        "release_page": RELEASE_PAGE,
        "files": runtime,
        "source_files": source,
    }
    installer = find_installer()
    if installer:
        manifest["installer"] = installer

    text = json.dumps(manifest, ensure_ascii=False, indent=1)
    if args.print:
        print(text)
        return 0

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(text + "\n", encoding="utf-8")
    print(f"  清单已生成：{MANIFEST}")
    print(f"  版本 {manifest['version']}　界面资源 {len(runtime)} 个　源码文件 {len(source)} 个")
    if installer:
        print(f"  安装包：{installer['name']}（{installer['size'] / 1048576:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
