# -*- coding: utf-8 -*-
"""把错误写进 data/error.log。

为什么要它：这轮排查的两个坑（PowerShell 中文乱码、拖拽接口 500）都是被
静默吞掉的 —— 用户看到的是"没反应"，而真正的原因谁也不知道。
所有被抓但没地方报的异常都应该走这里。
"""
from __future__ import annotations

import threading
import time
import traceback

from .config import DATA_DIR

_LOCK = threading.Lock()
MAX_BYTES = 2 * 1024 * 1024        # 超过 2MB 就轮转一次，避免无限增长


def _rotate_if_needed(path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass


def log_error(where: str, exc: BaseException | str, detail: str = "") -> None:
    """记一条错误。永远不要因为它自己抛异常而影响主流程。"""
    try:
        path = DATA_DIR / "error.log"
        _rotate_if_needed(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(exc, BaseException):
            body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            body = str(exc)
        line = "%s [%s] %s%s\n" % (stamp, where, detail + " " if detail else "", body.rstrip())
        with _LOCK:
            with open(path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line)
    except Exception:
        pass


def log_note(where: str, message: str) -> None:
    log_error(where, message)
