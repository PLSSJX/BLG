# 清掉测试遗留的项目登记与目录。
# 用法：python scripts/prune_test_projects.py [--dry]
"""删除测试产生的项目登记。

判定规则（任一命中即删）：
  · 名字是测试用例常见的那些（按钮巡检 / 界面端到端 / 场景1 …）
  · 路径落在临时目录，或以 _ / mt- 开头（e2e 套件用 mkdtemp 建的）
  · 登记的目录已经不在了（悬空条目）
「演示项目」和用户自己建的项目不会被碰。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TEST_NAME = re.compile(
    r"^(按钮巡检|界面端到端|端到端|联动测试|安全专项|附件测试|场景\d|中文空格|并发保存|占用测试|"
    r"大文件项目|挂错文件|坏版本号|空项目|标题边界|报告\.docx|p-|a/b|📚|长{20,}|"
    r"导出 git 仓库|内容块清理|分支切换与删除|同一行冲突|不同位置合并|分支合并场景)")
TEST_PATH = re.compile(r"[\\/](_e2e|_btn|_link|_sc_|_safety|_bk|_dl|mt-)|[\\/]Temp[\\/]", re.I)


def registry_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        profile = os.environ.get("USERPROFILE") or str(Path(r"C:\\Users").glob("*").__next__())
        local = os.path.join(profile, "AppData", "Local")
    return Path(local) / "Programs" / "MakiseToolkit" / "data" / "projects.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只看会删什么，不动手")
    args = ap.parse_args()

    path = registry_path()
    if not path.exists():
        print("没有找到项目登记：" + str(path))
        return 0
    items = json.loads(path.read_text(encoding="utf-8"))
    keep, drop = [], []
    tmp_root = str(Path(tempfile.gettempdir()))
    for it in items:
        target = str(it.get("path") or "")
        stale = not Path(target).exists()
        named = bool(TEST_NAME.match(str(it.get("name") or "")))
        located = bool(TEST_PATH.search(target)) or target.startswith(tmp_root)
        if named or located or stale:
            drop.append(it)
        else:
            keep.append(it)

    print(f"登记 {len(items)} 条，将删除 {len(drop)} 条，保留 {len(keep)} 条")
    for it in drop[:8]:
        print("  - " + str(it.get("name")) + "  " + str(it.get("path")))
    if len(drop) > 8:
        print("  …还有 " + str(len(drop) - 8) + " 条")

    if args.dry:
        return 0

    removed_dirs = 0
    for it in drop:
        target = Path(str(it.get("path") or ""))
        if not target.exists():
            continue
        # 只删测试目录，避免误删用户自己的东西
        if not (TEST_PATH.search(str(target)) or str(target).startswith(tmp_root)):
            print("  跳过非测试路径：" + str(target))
            continue
        try:
            shutil.rmtree(target, ignore_errors=True)
            removed_dirs += 1
        except OSError:
            pass

    path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已清理目录 {removed_dirs} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
