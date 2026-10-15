"""B 站 Web 端 WBI 签名（2023 年起 playurl 等接口的强制风控参数）。"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

# 官方前端使用的重排表，顺序不可改
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# urlencode 时不能被转义的字符集，与官方 wbi 实现保持一致
_FILTER = "!'()*"


def get_mixin_key(orig: str) -> str:
    """把 img_key + sub_key 按重排表混洗，取前 32 位。"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def enc_wbi(params: dict, img_key: str, sub_key: str, now: float | None = None) -> dict:
    """给参数字典补上 wts 与 w_rid。"""
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(now if now is not None else time.time())

    items = sorted(params.items())
    # 官方实现会先把 !'()* 去掉再编码，签名与请求必须用同一套结果
    query = urllib.parse.urlencode(
        [(k, "".join(ch for ch in str(v) if ch not in _FILTER)) for k, v in items]
    )
    params["w_rid"] = _md5(query + mixin_key)
    return params


def keys_from_urls(img_url: str, sub_url: str) -> tuple[str, str]:
    """从 nav 接口返回的两个图片地址里抽出 img_key / sub_key。"""
    def stem(url: str) -> str:
        return url.rsplit("/", 1)[-1].split(".")[0]

    return stem(img_url), stem(sub_url)
