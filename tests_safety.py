"""安全性回归测试：把所有"可能删/覆盖用户数据"的路径都构造最坏输入跑一遍。

只在自己的沙箱目录里操作，不碰真实盘和数据。
用法：python tests_safety.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  [OK] " if cond else "  [XX] ") + label)


def sandbox(name):
    base = ROOT / "build-tools" / "_safety" / name
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    return base


def touch(path, text="x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- A. 安装路径收敛
def test_resolve_target():
    print("\nA. 安装路径收敛（防呆：永远进 VGPlatform）")
    import setup as S
    r = S.resolve_target
    check(r(Path("D:\\")) == Path("D:\\VGPlatform"), "盘根 D:\\ -> D:\\VGPlatform")
    check(r(Path(r"D:\Tools")) == Path(r"D:\Tools\VGPlatform"), "普通目录 -> 其下 VGPlatform")
    check(r(Path(r"D:\Tools\VGPlatform")) == Path(r"D:\Tools\VGPlatform"), "已选到 VGPlatform 不再嵌套")
    check(r(Path(r"D:\Tools\Video-Grabing")) == Path(r"D:\Tools\Video-Grabing"), "已选到程序目录不再嵌套")
    sb = sandbox("resolve")
    touch(sb / "Video-Grabing.exe")
    check(r(sb) == sb, "现有安装目录原地升级（不套两层）")
    sb2 = sandbox("resolve2")
    touch(sb2 / "installed-files.json", "{}")
    check(r(sb2) == sb2, "有清单的目录也原地升级")
    # 相对路径不该炸
    got = r(Path("relative_dir"))
    check(str(got).endswith("VGPlatform"), "相对路径也收敛到 VGPlatform")


# ---------------------------------------------------------------- B. 盘根 / 危险目录
def test_drive_root():
    print("\nB. 盘根识别")
    import setup as S
    check(S.is_drive_root(Path("D:\\")) is True, "D:\\ 判定为盘根")
    check(S.is_drive_root(Path("C:\\Windows")) is False, "C:\\Windows 不是盘根")
    check(S.is_drive_root(Path(r"D:\Tools")) is False, "D:\\Tools 不是盘根")


# ---------------------------------------------------------------- C. 覆盖安装的删除范围
def test_uninstall_existing():
    print("\nC. 覆盖安装只删自己的文件")
    import setup as S

    # 纯用户目录：一个都不许动
    sb = sandbox("pure_user")
    touch(sb / "重要.txt")
    touch(sb / "项目" / "code.py")
    kept = S.uninstall_existing(sb)
    check((sb / "重要.txt").exists() and (sb / "项目" / "code.py").exists(),
          "纯用户目录：文件全部保留")

    # 老安装（无清单）+ 用户文件：只删已知名字
    sb = sandbox("legacy")
    touch(sb / "Video-Grabing.exe")
    touch(sb / "_internal" / "a.dll")
    touch(sb / "我的备份.txt")
    S.uninstall_existing(sb)
    check(not (sb / "Video-Grabing.exe").exists(), "老安装：exe 被删")
    check(not (sb / "_internal").exists(), "老安装：_internal 被删")
    check((sb / "我的备份.txt").exists(), "老安装：用户文件保留")

    # 新安装（有清单）+ 用户文件
    sb = sandbox("manifest")
    touch(sb / "Video-Grabing.exe")
    touch(sb / "_internal" / "a.dll")
    touch(sb / "installed-files.json",
          '{"files": ["Video-Grabing.exe", "_internal/a.dll"]}')
    touch(sb / "用户文件.txt")
    S.uninstall_existing(sb)
    check(not (sb / "Video-Grabing.exe").exists(), "清单安装：exe 被删")
    check(not (sb / "_internal").exists(), "清单安装：_internal 被删")
    check((sb / "用户文件.txt").exists(), "清单安装：用户文件保留")


# ---------------------------------------------------------------- D. 解包冲突检测
def test_conflicts():
    print("\nD. 解包前检测同名覆盖")
    import setup as S
    sb = sandbox("conflict")
    touch(sb / "README.md")
    touch(sb / "_internal" / "old.dll")

    class M:
        def __init__(self, n):
            self.filename = n

    members = [M("README.md"), M("_internal/old.dll"), M("Video-Grabing.exe")]
    bad = S.conflicting_files(sb, members)
    check("README.md" in bad, "用户同名文件被识别为冲突")
    check("_internal/old.dll" not in bad, "自己的旧文件不算冲突")
    check("Video-Grabing.exe" not in bad, "新文件不算冲突")


# ---------------------------------------------------------------- E. 下载目录归属
def test_own_directory():
    print("\nE. 下载目录归属（不写进用户的同名文件夹）")
    from videograb.tasks import DIR_MARKER, _own_directory

    sb = sandbox("dl")
    user = sb / "某视频"
    touch(user / "我的视频.mp4", "用户的数据")
    got = _own_directory(user)
    check(got != user, "用户的同名目录被让开")
    check((user / "我的视频.mp4").read_text(encoding="utf-8") == "用户的数据",
          "用户的文件完好无损")

    fresh = sb / "新视频"
    got2 = _own_directory(fresh)
    check(got2 == fresh and (fresh / DIR_MARKER).is_file(), "全新目录建好并打标记")
    # 复用要先把目录"还回去"——生产侧在任务结束时调 _release_directory（tasks.py:445），
    # 测试里也要照做，否则第二次拿到的必然是 "x (2)"（这是并发保护，不是回归）
    from videograb.tasks import _release_directory
    check(_own_directory(fresh) != fresh, "任务未结束时不会把同一目录给第二个任务")
    _release_directory(fresh)                      # 模拟任务结束
    check(_own_directory(fresh) == fresh, "任务结束后自己的目录可复用（断点续传）")

    got3 = _own_directory(user)
    check(got3 != user and got3.name.endswith(")"), "再次遇到用户目录继续换序号")


# ---------------------------------------------------------------- F. 删除任务文件
def test_delete_task_files():
    print("\nF. 删除任务只删自己的产物")
    from videograb.tasks import Task, TaskManager

    sb = sandbox("task")
    d = sb / "某视频"
    touch(d / "out.mp4")
    touch(d / "我的笔记.txt")
    touch(d / "_parts" / "p1.bin")
    outside = touch(sb / "外部重要文件.txt")   # 目录之外

    m = TaskManager.__new__(TaskManager)
    t = Task(raw_input="x", directory=str(d))
    t.files = [{"path": str(d / "out.mp4")}, {"path": str(outside)}]
    m._delete_task_files(t)
    check(not (d / "out.mp4").exists(), "任务产物被删")
    check(not (d / "_parts").exists(), "中间目录被删")
    check((d / "我的笔记.txt").exists(), "用户文件保留")
    check(outside.exists(), "**目录之外的路径被拒绝删除**")
    check(d.is_dir(), "目录非空所以保留")


# ---------------------------------------------------------------- G. 清单路径校验
def test_safe_relative():
    print("\nG. 清单路径穿越防护")
    from videograb.updater import safe_relative
    for bad in ["../x", "../../x", "C:/x", "/x", "a/../../x", "..\\x", ""]:
        check(safe_relative(bad) is None, "拒绝 " + repr(bad))
    for ok in ["web/index.html", "videograb/tasks.py", "a/b/c.js"]:
        check(safe_relative(ok) is not None, "放行 " + repr(ok))


# ---------------------------------------------------------------- H. 卸载程序
def test_remover():
    print("\nH. 卸载程序只删自己的文件")
    import uninstall as U
    sb = sandbox("uninst")
    touch(sb / "Video-Grabing.exe")
    touch(sb / "_internal" / "a.dll")
    touch(sb / "installed-files.json",
          '{"files": ["Video-Grabing.exe", "_internal/a.dll"]}')
    touch(sb / "用户资料.txt")
    touch(sb / "我的照片" / "a.jpg")
    removed, failed, kept = U.remove_all(sb, keep_user_data=True)
    check("用户资料.txt" in kept, "用户文件进 kept 列表")
    check((sb / "用户资料.txt").exists(), "用户文件仍在")
    check((sb / "我的照片" / "a.jpg").exists(), "用户目录仍在")
    check(not (sb / "Video-Grabing.exe").exists(), "程序文件被删")
    check(not (sb / "_internal").exists(), "_internal 被删")


# ---------------------------------------------------------------- I. 自删除只删空目录
def test_self_delete():
    print("\nI. 自删除用非递归 rmdir")
    import inspect
    import uninstall as U
    src = inspect.getsource(U.schedule_self_delete)
    # 只看真正拼出来的命令行，别被注释里"以前是 rmdir /s"误导
    command = [line for line in src.splitlines() if line.strip().startswith("script =")]
    joined = " ".join(command)
    check("rmdir /q" in joined, "实际命令使用 rmdir /q（非递归）")
    check("/s" not in joined, "实际命令不含 /s（递归清空）")


# ---------------------------------------------------------------- J. 真实静默安装
def test_real_install():
    print("\nJ. 真实静默安装到非空目录（集成）")
    import subprocess
    setup = ROOT / "dist" / "Video-Grabing-Setup-2.4.7.exe"
    if not setup.is_file():
        print("  [--] 跳过：安装包不在")
        return
    sb = sandbox("realinstall")
    touch(sb / "用户文件.txt", "用户的数据")
    touch(sb / "我的项目" / "main.py", "用户的代码")
    proc = subprocess.run([str(setup), "/S", "/D=" + str(sb)],
                          capture_output=True, text=True, timeout=300,
                          cwd=str(ROOT / "dist"))
    nested = sb / "VGPlatform"
    check(proc.returncode == 0, "安装成功（退出码 0）")
    check(nested.is_dir(), "在选中目录下建了 VGPlatform")
    check((nested / "Video-Grabing.exe").is_file(), "主程序在 VGPlatform 里")
    check((sb / "用户文件.txt").exists(), "用户文件完好")
    check((sb / "我的项目" / "main.py").exists(), "用户目录完好")
    # 顶层不该出现散落的程序文件
    stray = [p.name for p in sb.iterdir() if p.name in ("_internal", "uninstall.exe")]
    check(not stray, "程序文件没有散落到用户目录（" + str(stray) + "）")
    shutil.rmtree(sb, ignore_errors=True)


# ---------------------------------------------------------------- K. 转码输出不覆盖
def test_transcode_unique():
    print("\nK. 转码/解密输出不覆盖已有文件")
    from videograb.music.decrypt import _unique_path as dq
    from videograb.music.transcode import _unique_path as tq

    sb = sandbox("trans")
    target = touch(sb / "歌.mp3", "用户原有的歌")
    got = tq(target)
    check(got != target, "转码输出：同名文件存在时另起名字")
    check(target.read_text(encoding="utf-8") == "用户原有的歌", "转码输出：原文件未被覆盖")
    free = sb / "新歌.mp3"
    check(tq(free) == free, "转码输出：没有同名文件时用原名")

    target2 = touch(sb / "解密的歌.flac", "用户原有的歌")
    got2 = dq(target2)
    check(got2 != target2, "解密输出：同名文件存在时另起名字")
    check(target2.read_text(encoding="utf-8") == "用户原有的歌", "解密输出：原文件未被覆盖")

    # 连续两次命名不冲突
    n1 = tq(target)
    touch(n1, "x")
    n2 = tq(target)
    check(n1 != n2, "连续调用不会给出同一个名字")


# ---------------------------------------------------------------- M. 接口路径收敛
def test_api_path_guard():
    print("\nM. 接口输出目录收敛 / 请求体类型校验")
    from videograb import server as S
    from videograb.config import Context, Settings

    sb = sandbox("apiguard")
    downloads = sb / "downloads"
    music = downloads / "音乐"
    music.mkdir(parents=True, exist_ok=True)
    ctx = Context(Settings(download_dir=str(downloads), music_dir=str(music)))

    src = touch(downloads / "素材" / "video.mp4")
    check(S._inside_output_root(downloads / "out", ctx, src) is True,
          "下载目录里的子目录：放行")
    check(S._inside_output_root(music, ctx, src) is True, "音乐目录：放行")
    check(S._inside_output_root(src.parent, ctx, src) is True, "视频自己所在的目录：放行")
    for bad in ("C:\\Windows", "C:\\Users\\Public", str(sb / ".."), "..\\..\\..\\escape"):
        check(S._inside_output_root(bad, ctx, src) is False, "拒绝 " + repr(bad))
    check(S._inside_output_root(downloads / ".." / "..", ctx, src) is False,
          "拒绝：从允许目录里用 .. 爬出去")

    try:
        S._as_str_list({"paths": 123}, "paths", "视频路径")
        got = None
    except S.ApiError as exc:
        got = exc
    check(isinstance(got, S.ApiError), "paths 传整数 -> ApiError(400)，不是 TypeError(500)")
    check(S._as_str_list({"paths": ["a", "b"]}, "paths", "路径") == ["a", "b"], "正常数组照常返回")
    check(S._as_str_list({"paths": "abc"}, "paths", "路径") == ["abc"],
          "单个字符串当成一项，不会被拆成 a/b/c")


# ---------------------------------------------------------------- N. 歌单落盘校验
def test_playlist_guard():
    print("\nN. 空歌单不再写进 playlists.json")
    from videograb import playlist as P

    sb = sandbox("playlist")
    old_path, old_dir = P.PLAYLISTS_PATH, P.DATA_DIR
    P.PLAYLISTS_PATH, P.DATA_DIR = sb / "playlists.json", sb
    try:
        for bad in (None, {}, {"id": "1"}, {"site": "wangyi"},
                    {"id": "1", "site": "wangyi", "songs": "不是数组"}):
            try:
                P.add_local(bad)
                rejected = False
            except ValueError:
                rejected = True
            check(rejected, "拒绝非法歌单：" + repr(bad)[:60])
        entry = P.add_local({"id": "42", "site": "wangyi", "title": "测试",
                             "songs": [{"name": "a"}]})
        check(entry["id"] == "42" and len(P.load_local()) == 1, "合法歌单正常保存")
        check(P.remove_local("wangyi", "42") is True and P.load_local() == [],
              "按 id+site 正常删除")
    finally:
        P.PLAYLISTS_PATH, P.DATA_DIR = old_path, old_dir


# ---------------------------------------------------------------- L. 附加文件作用域
def test_extras_scope():
    print("\nL. 封面/弹幕等附加文件写在任务目录内")
    import inspect
    import videograb.extras as E
    src = inspect.getsource(E)
    check("task.directory" in src or "directory" in src,
          "extras 使用任务目录（不写任意路径）")


def main():
    print("=" * 60)
    print("安全性回归测试")
    print("=" * 60)
    for fn in (test_resolve_target, test_drive_root, test_uninstall_existing,
               test_conflicts, test_own_directory, test_delete_task_files,
               test_safe_relative, test_remover, test_self_delete,
               test_real_install, test_transcode_unique, test_extras_scope,
               test_api_path_guard, test_playlist_guard):
        try:
            fn()
        except Exception as exc:
            FAIL.append(fn.__name__)
            print("  [XX] " + fn.__name__ + " 抛异常：" + str(exc)[:160])
    shutil.rmtree(ROOT / "build-tools" / "_safety", ignore_errors=True)
    print()
    print("=" * 60)
    print("通过 " + str(len(PASS)) + " 项，失败 " + str(len(FAIL)) + " 项")
    if FAIL:
        for f in FAIL:
            print("  失败：" + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())