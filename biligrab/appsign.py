"""移动端 / TV 端 APP 接口的 appkey + appsec 签名。"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

# 公开的官方客户端 appkey/appsec 对（社区长期在用的固定值）
APP_KEYS = {
    # 哔哩哔哩 Android 客户端
    "android": ("27eb53fc9058f8c3", "c2ed53a74eeefe3cf99fbd01d8c9c375"),
    # 哔哩哔哩 iPhone 客户端
    "iphone": ("27eb53fc9058f8c3", "c2ed53a74eeefe3cf99fbd01d8c9c375"),
    # 云视听小电视（TV 端）
    "tv": ("4409e2ce8ffd12b8", "59b43e04ad6965f34319062b478f83dd"),
    # 哔哩哔哩投稿工具
    "bili_tv": ("4409e2ce8ffd12b8", "59b43e04ad6965f34319062b478f83dd"),
}

# 各客户端常用的伪装参数
APP_EXTRA = {
    "android": {
        "mobi_app": "android",
        "platform": "android",
        "build": "8380200",
        "device": "phone",
        "c_locale": "zh_CN",
        "s_locale": "zh_CN",
    },
    "iphone": {
        "mobi_app": "iphone",
        "platform": "iphone",
        "build": "83800",
        "device": "phone",
        "c_locale": "zh_CN",
        "s_locale": "zh_CN",
    },
    "tv": {
        "mobi_app": "android_tv_yst",
        "platform": "android",
        "build": "102801",
        "device": "tv",
        "c_locale": "zh_CN",
        "s_locale": "zh_CN",
    },
}


def sign_params(params: dict, appkey: str, appsec: str, ts: int | None = None) -> dict:
    """按 key 排序拼接后取 md5(query + appsec) 作为 sign。"""
    params = dict(params)
    params["appkey"] = appkey
    params["ts"] = int(ts if ts is not None else time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    params["sign"] = hashlib.md5((query + appsec).encode("utf-8")).hexdigest()
    return params
