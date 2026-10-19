#!/usr/bin/env python3
"""MakiseToolkit 一键启动脚本。

用法：
    python start.py              # 启动界面并自动打开浏览器
    python start.py --port 9000  # 换端口
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if sys.version_info < (3, 9):
    print("需要 Python 3.9 或更高版本，当前为 %s" % sys.version.split()[0])
    sys.exit(1)

os.chdir(ROOT)

from videograb.__main__ import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv
    sys.exit(main(argv))
