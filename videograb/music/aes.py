# -*- coding: utf-8 -*-
"""AES-128 加解密（纯标准库实现）。

来历：做网易云 eapi 签名时自己写的，并用 FIPS-197 标准向量验证过：
    00112233... -> 69c4e0d86a7b0430d8cdb78070b4c55a  ✓

**目前没有接入主流程。** eapi 签名验证是通的，但那台机器到网易云音频 CDN
（m*.music.126.net）的链路被中间人代理阻断，拿到的地址一律 403，
所以接进下载流程没有任何收益。留着这个实现，环境允许时可以直接用：

    body = "params=" + ecb(sign_text.encode(), b'e82ckenh8dichen8').hex().upper()

和 music/decrypt.py 里的解密实现是配套的：那边有 _aes_ecb_decrypt，
这边补上加密方向，密钥扩展和 S 盒复用那边的，避免两份常量。
"""
from __future__ import annotations

from .decrypt import _SBOX

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _mul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _expand(key: bytes) -> list:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return w


def _block(block: bytes, w) -> bytes:
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(rnd):
        for c in range(4):
            for r in range(4):
                s[r][c] ^= w[rnd * 4 + c][r]

    add_round_key(0)
    for rnd in range(1, 11):
        for r in range(4):
            for c in range(4):
                s[r][c] = _SBOX[s[r][c]]
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
        if rnd != 10:
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
                s[1][c] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
                s[2][c] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
                s[3][c] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
        add_round_key(rnd)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def ecb_encrypt(data: bytes, key: bytes, pad: bool = True) -> bytes:
    """AES-128-ECB 加密。pad=False 时要求数据长度是 16 的整数倍。"""
    if pad:
        n = 16 - len(data) % 16
        data = data + bytes([n]) * n
    elif len(data) % 16:
        raise ValueError("AES 数据长度不是 16 的整数倍")
    w = _expand(key)
    return b"".join(_block(data[i:i + 16], w) for i in range(0, len(data), 16))
