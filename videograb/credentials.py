# -*- coding: utf-8 -*-
"""各平台凭据校验：把用户粘贴的值拿去真的问一次平台接口。

为什么必须有这个：用户贴进来的东西经常不是 Cookie（比如一串没等号的哈希），
光看长度根本分不出来。只有真的拿它问一次登录态接口，才知道能不能用。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


class CheckError(Exception):
    pass


def _get(url: str, cookie: str, referer: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise CheckError("平台返回 HTTP " + str(exc.code)) from None
    except Exception as exc:
        raise CheckError("连不上平台：" + str(exc)[:100]) from None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise CheckError("平台返回的不是 JSON，接口可能变了") from None


def _looks_like_cookie(value: str) -> bool:
    """有没有 key=value 结构。没有的话多半是贴错了（哈希、token、别的东西）。"""
    return "=" in value


def _as_cookie(value: str, key: str) -> str:
    """用户可能只贴了值，也可能贴了整段 Cookie，两种都接受。"""
    value = value.strip()
    if _looks_like_cookie(value):
        return value
    return key + "=" + value


def check(site: str, value: str | None) -> dict:
    """返回 {"ok": bool, "detail": str, "level": "ok"|"warn"|"bad"}。"""
    from .config import load_settings
    settings = load_settings()
    value = (value or "").strip()
    if not value:
        # 没传就校验已保存的那个
        field = {"netease": "ncm_cookie", "qqmusic": "qqmusic_cookie",
                 "kugou": "kugou_cookie", "kuwo": "kuwo_cookie",
                 "bilibili": "sessdata"}.get(site, "")
        value = str(getattr(settings, field, "") or "").strip()
    if not value:
        return {"ok": False, "level": "bad", "detail": "还没有配置，先粘贴再校验"}

    if site == "netease":
        cookie = _as_cookie(value, "MUSIC_U")
        try:
            data = _get("https://music.163.com/api/nuser/account/get",
                        cookie, "https://music.163.com/")
        except CheckError as exc:
            return {"ok": False, "level": "warn", "detail": str(exc)}
        profile = data.get("profile") or {}
        if data.get("code") == 200 and profile.get("nickname"):
            vip = "（黑胶 VIP）" if (profile.get("vipType") or 0) > 0 else ""
            return {"ok": True, "level": "ok",
                    "detail": "登录态有效：" + str(profile.get("nickname")) + vip}
        if not _looks_like_cookie(value):
            return {"ok": False, "level": "bad",
                    "detail": "平台不认这个值。它看起来不像 Cookie（没有 key=value 结构），"
                              "请到浏览器 F12 → Application → Cookies 复制 MUSIC_U 那一行"}
        return {"ok": False, "level": "bad", "detail": "Cookie 无效或已过期，重新登录网易云再取一次"}

    if site == "qqmusic":
        cookie = _as_cookie(value, "uin")
        try:
            data = _get("https://u.y.qq.com/cgi-bin/musicu.fcg?data=%7B%7D",
                        cookie, "https://y.qq.com/")
        except CheckError as exc:
            return {"ok": False, "level": "warn", "detail": str(exc)}
        if data.get("code") in (0, 200) and _looks_like_cookie(cookie):
            return {"ok": True, "level": "ok", "detail": "接口可达，Cookie 结构正常"}
        return {"ok": False, "level": "bad", "detail": "QQ 音乐不认这个 Cookie，检查是否含 uin 和 qm_keyst"}

    if site == "bilibili":
        cookie = _as_cookie(value, "SESSDATA")
        try:
            data = _get("https://api.bilibili.com/x/web-interface/nav",
                        cookie, "https://www.bilibili.com/")
        except CheckError as exc:
            return {"ok": False, "level": "warn", "detail": str(exc)}
        if data.get("code") == 0 and (data.get("data") or {}).get("isLogin"):
            name = (data.get("data") or {}).get("uname") or ""
            return {"ok": True, "level": "ok", "detail": "登录态有效：" + str(name)}
        return {"ok": False, "level": "bad", "detail": "SESSDATA 无效或已过期"}

    # 酷狗 / 酷我：没有稳定的登录态查询接口，只能查格式，如实说明
    if site in ("kugou", "kuwo"):
        if _looks_like_cookie(value):
            return {"ok": True, "level": "warn",
                    "detail": "格式正常（含 key=value）。这个平台没有可用的在线校验接口，"
                              "只能保证格式；能不能用要实际下一首才知道"}
        return {"ok": False, "level": "bad",
                "detail": "看起来不像 Cookie（没有 key=value 结构），大概率贴错了"}

    return {"ok": False, "level": "warn", "detail": "这个平台暂时没有校验方式"}
