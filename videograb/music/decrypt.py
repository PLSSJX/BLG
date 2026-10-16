"""平台加密音频解密。

纯标准库实现（无第三方依赖），把音乐客户端下载到的私有格式还原成通用音频：

    .ncm                                                     网易云音乐
    .qmc0/.qmc3/.qmcflac/.qmcogg/.mflac/.mflac0/.mgg/.mgg1   QQ 音乐
    .kgm/.kgma/.vpr                                          酷狗音乐
    .kwm                                                     酷我音乐

对外接口：ENCRYPTED_EXTENSIONS / is_encrypted / sniff / decrypt。
音频数据一律流式分块处理，不会把整首歌读进内存；解不出来就抛 ValueError，
绝不写出一个内容损坏的文件冒充成功。
"""
from __future__ import annotations

import base64
import json
import os
import struct
from itertools import cycle
from pathlib import Path

__all__ = ["ENCRYPTED_EXTENSIONS", "is_encrypted", "sniff", "decrypt"]

# ---------------------------------------------------------------- 基本常量

ENCRYPTED_EXTENSIONS: set[str] = {
    ".ncm",
    ".qmc0", ".qmc2", ".qmc3", ".qmc4", ".qmc6", ".qmc8",
    ".qmcflac", ".qmcogg", ".qmcra", ".tkm",
    ".mflac", ".mflac0", ".mflach", ".mgg", ".mgg0", ".mgg1", ".mggl",
    ".kgm", ".kgma", ".kgg", ".vpr",
    ".kwm",
}

_CHUNK = 1 << 18  # 256 KiB：流式处理块大小
_FORMAT_EXT = {"flac": ".flac", "mp3": ".mp3", "ogg": ".ogg", "m4a": ".m4a", "wav": ".wav"}
_MP3_SR_OK = {0, 1, 2, 3}
_MP3_BR_OK = set(range(1, 15))


def _u32le(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _xor(data: bytes, key: int) -> bytes:
    return bytes(b ^ key for b in data)


# ---------------------------------------------------------------- 容器探测

def _probe_format(head: bytes) -> str:
    """按文件头判断真实音频容器：flac / mp3 / ogg / m4a / wav / ''（未知）。"""
    if len(head) < 4:
        return ""
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    if head[:3] == b"ID3":
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and len(head) >= 4:
        ver = (head[1] >> 3) & 0x03
        layer = (head[1] >> 1) & 0x03
        br = (head[2] >> 4) & 0x0F
        sr = (head[2] >> 2) & 0x03
        if ver != 1 and layer != 0 and br in _MP3_BR_OK and sr in _MP3_SR_OK:
            return "mp3"
    return ""


def _read_head(path: Path, size: int = 64) -> bytes:
    with path.open("rb") as fp:
        return fp.read(size)


def _read_tail(path: Path, size: int) -> bytes:
    with path.open("rb") as fp:
        fp.seek(0, os.SEEK_END)
        length = fp.tell()
        fp.seek(max(0, length - size))
        return fp.read()


# ---------------------------------------------------------------- AES-128 ECB（仅解密）

def _build_sbox() -> bytes:
    """按定义生成 AES S 盒（GF(2^8) 求逆 + 仿射变换），不手工抄表。"""
    sbox = bytearray(256)
    for a in range(256):
        inv = 0
        if a:
            for x in range(1, 256):
                p, y, xa = 0, 0, a
                for _ in range(8):
                    if (x >> y) & 1:
                        p ^= xa
                    y += 1
                    xa = ((xa << 1) ^ (0x1B if xa & 0x80 else 0)) & 0xFF
                if p == 1:
                    inv = x
                    break
        s = inv
        for _ in range(4):
            inv = ((inv << 1) | (inv >> 7)) & 0xFF
            s ^= inv
        sbox[a] = s ^ 0x63
    return bytes(sbox)


_SBOX = _build_sbox()
_INV_SBOX = bytes(_SBOX.index(i) for i in range(256))


def _mul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF
    return p


_MUL2 = bytes(_mul(i, 0x02) for i in range(256))
_MUL3 = bytes(_mul(i, 0x03) for i in range(256))
_MUL9 = bytes(_mul(i, 0x09) for i in range(256))
_MUL11 = bytes(_mul(i, 0x0B) for i in range(256))
_MUL13 = bytes(_mul(i, 0x0D) for i in range(256))
_MUL14 = bytes(_mul(i, 0x0E) for i in range(256))


def _expand_key(key: bytes) -> list:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    w = list(struct.unpack(">4I", key))
    rcon = 1
    for i in range(4, 44):
        t = w[i - 1]
        if i % 4 == 0:
            t = ((t << 8) & 0xFFFFFFFF) | (t >> 24)  # RotWord：整体左移一个字节
            t = (_SBOX[(t >> 24) & 0xFF] << 24 | _SBOX[(t >> 16) & 0xFF] << 16 |
                 _SBOX[(t >> 8) & 0xFF] << 8 | _SBOX[t & 0xFF])
            t ^= rcon << 24
            rcon = ((rcon << 1) ^ (0x1B if rcon & 0x80 else 0)) & 0xFF
        w.append(w[i - 4] ^ t)
    return [w[i:i + 4] for i in range(0, 44, 4)]


def _decrypt_block(block: bytes, rk: list) -> bytes:
    """单块 AES-128 解密。

    状态按列主序存放（state[r][c] 在 block[r + 4c]），这样行位移就是整段切片平移。
    """
    ks = b"".join(x.to_bytes(4, "big") for x in rk[10])
    s = [block[i] ^ ks[i] for i in range(16)]
    inv = _INV_SBOX
    m9, m11, m13, m14 = _MUL9, _MUL11, _MUL13, _MUL14
    for rnd in range(9, 0, -1):
        # InvShiftRows：第 r 行循环右移 r 个字节
        s = [s[0], s[13], s[10], s[7],
             s[4], s[1], s[14], s[11],
             s[8], s[5], s[2], s[15],
             s[12], s[9], s[6], s[3]]
        ks = b"".join(x.to_bytes(4, "big") for x in rk[rnd])
        s = [inv[c] ^ ks[i] for i, c in enumerate(s)]
        # InvMixColumns
        o = [0] * 16
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            o[c] = m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3]
            o[c + 1] = m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3]
            o[c + 2] = m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3]
            o[c + 3] = m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3]
        s = o
    s = [s[0], s[13], s[10], s[7],
         s[4], s[1], s[14], s[11],
         s[8], s[5], s[2], s[15],
         s[12], s[9], s[6], s[3]]
    ks = b"".join(x.to_bytes(4, "big") for x in rk[0])
    return bytes(inv[c] ^ ks[i] for i, c in enumerate(s))


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES 数据长度不是 16 的整数倍")
    rk = _expand_key(key)
    out = bytearray(len(data))
    for off in range(0, len(data), 16):
        out[off:off + 16] = _decrypt_block(bytes(data[off:off + 16]), rk)
    return bytes(out)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and len(data) >= n and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


# ---------------------------------------------------------------- NCM（网易云音乐）

_NCM_MAGIC = b"CTENFDAM"
_NCM_CORE_KEY = bytes((0x68, 0x7A, 0x48, 0x52, 0x41, 0x6D, 0x73, 0x6F,
                       0x35, 0x6B, 0x49, 0x6E, 0x62, 0x61, 0x78, 0x57))
_NCM_META_KEY = bytes((0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21,
                       0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28))
_NCM_KEY_PREFIX = b"neteasecloudmusic"
_NCM_META_PREFIX = b"163 key(Don't modify):"


def _ncm_build_keybox(key: bytes) -> bytearray:
    """网易的 RC4 变体：用音频密钥生成 256 字节 keybox。"""
    box = bytearray(range(256))
    klen = len(key)
    if klen == 0:
        raise ValueError("文件损坏：NCM 音频密钥为空")
    last = 0
    off = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= klen:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return box


class _NcmCipher:
    """NCM 音频流：按 (offset+i+1) 索引 keybox 的 RC4 变体。"""

    name = "网易云 NCM"

    def __init__(self, box: bytearray):
        self._box = box

    def decrypt(self, buf: bytearray, offset: int) -> None:
        box = self._box
        for i in range(len(buf)):
            j = (offset + i + 1) & 0xFF
            buf[i] ^= box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]


def _ncm_parse_meta(meta_raw: bytes) -> dict:
    try:
        blob = _xor(meta_raw, 0x63)
        if blob.startswith(_NCM_META_PREFIX):
            blob = blob[len(_NCM_META_PREFIX):]
        plain = _pkcs7_unpad(_aes_ecb_decrypt(base64.b64decode(blob), _NCM_META_KEY))
        if plain.startswith(b"music:"):
            plain = plain[6:]
        data = json.loads(plain.decode("utf-8", "replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _ncm_meta_pick(meta: dict, cover: bytes) -> dict:
    out: dict = {}
    if meta.get("musicName"):
        out["title"] = str(meta["musicName"])
    artists = meta.get("artist")
    if isinstance(artists, list):
        names = []
        for item in artists:
            if isinstance(item, (list, tuple)) and item:
                names.append(str(item[0]))
            elif isinstance(item, str):
                names.append(item)
        if names:
            out["artist"] = "/".join(names)
    if meta.get("album"):
        out["album"] = str(meta["album"])
    if meta.get("format"):
        out["format"] = str(meta["format"]).lower()
    for key in ("bitrate", "duration"):
        if meta.get(key):
            out[key] = meta[key]
    if cover:
        out["cover"] = cover
    return out


def _parse_ncm(path: Path):
    """解析 NCM 头部，返回 (keybox, meta, cover, audio_offset)。"""
    size = path.stat().st_size
    with path.open("rb") as fp:
        if fp.read(8) != _NCM_MAGIC:
            raise ValueError("不是有效的 NCM 文件：文件头不是 CTENFDAM")
        fp.seek(10)
        raw = fp.read(4)
        if len(raw) < 4:
            raise ValueError("文件损坏：NCM 密钥段长度读取失败")
        key_len = _u32le(raw, 0)
        if key_len <= 0 or key_len > (1 << 20):
            raise ValueError(f"文件损坏：密钥段长度不对（{key_len}）")
        key_data = fp.read(key_len)
        if len(key_data) != key_len:
            raise ValueError("文件损坏：密钥段数据不完整")
        try:
            key_blob = _pkcs7_unpad(_aes_ecb_decrypt(_xor(key_data, 0x64), _NCM_CORE_KEY))
        except ValueError as exc:
            raise ValueError(f"文件损坏：密钥段无法解密（{exc}）") from exc
        if not key_blob.startswith(_NCM_KEY_PREFIX):
            raise ValueError("文件损坏：密钥段解密结果缺少 neteasecloudmusic 前缀")
        stream_key = key_blob[len(_NCM_KEY_PREFIX):]
        if len(stream_key) < 16:
            raise ValueError("文件损坏：音频密钥不足 16 字节")
        box = _ncm_build_keybox(stream_key)

        # 元数据长度是 u32：真实文件紧跟密钥段，少数工具会在中间多插 2 字节
        import os as _os
        here = fp.tell()
        raw = fp.read(4)
        if len(raw) < 4:
            raise ValueError("文件损坏：元数据长度读取失败")
        meta_len = _u32le(raw, 0)
        if meta_len > (1 << 24):
            fp.seek(here)
            fp.read(2)
            raw = fp.read(4)
            if len(raw) < 4:
                raise ValueError("文件损坏：元数据长度读取失败")
            meta_len = _u32le(raw, 0)
        if meta_len > (1 << 24):
            raise ValueError(f"文件损坏：元数据长度不合理（{meta_len}）")
        meta: dict = {}
        if meta_len > 0:
            meta_raw = fp.read(meta_len)
            if len(meta_raw) != meta_len:
                raise ValueError("文件损坏：元数据段不完整")
            meta = _ncm_parse_meta(meta_raw)

        fp.read(4)   # CRC32
        fp.read(5)   # 版本与间隔
        raw = fp.read(4)
        if len(raw) < 4:
            raise ValueError("文件损坏：封面长度读取失败")
        cover_len = _u32le(raw, 0)
        cover = b""
        if 0 < cover_len <= (1 << 26):
            cover = fp.read(cover_len)
            if len(cover) != cover_len:
                raise ValueError("文件损坏：封面数据不完整")
        audio_offset = fp.tell()
        if audio_offset >= size:
            raise ValueError("文件损坏：没有音频数据")
    return box, meta, cover, audio_offset


# ---------------------------------------------------------------- QMC（QQ 音乐）

_QMC_STATIC_KEYS = bytes((
    0xC3, 0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52, 0xD8, 0xA1, 0x66, 0x62,
    0x9F, 0x5B, 0x09, 0x00, 0xC3, 0x5E, 0x95, 0x23, 0x9F, 0x13, 0x11, 0x7E,
    0xD8, 0x92, 0x3F, 0xBC, 0x90, 0xBB, 0x74, 0x0E, 0xC3, 0x47, 0x74, 0x3D,
    0x90, 0xAA, 0x3F, 0x51, 0xD8, 0xF4, 0x11, 0x84, 0x9F, 0xDE, 0x95, 0x1D,
    0xC3, 0xC6, 0x09, 0xD5, 0x9F, 0xFA, 0x66, 0xF9, 0xD8, 0xF0, 0xF7, 0xA0,
    0x90, 0xA1, 0xD6, 0xF3,
))

_QMC_MAGIC_Q = b"QTag"
_QMC_MAGIC_S = b"STag"
_QMC_MAGIC_M = b"musicex\x00"
_QMC_ENC_V2 = b"QQMusic EncV2,Key:"
_QMC_V2_KEY1 = bytes((0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
                      0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28))
_QMC_V2_KEY2 = bytes((0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
                      0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54))
_QMC_TEA_SEED = bytes((0x69, 0x56, 0x46, 0x38, 0x2B, 0x20, 0x15, 0x0B))

_QMC_FIRST_SEGMENT = 0x80
_QMC_OTHER_SEGMENT = 0x1400


def _qmc_make_mask() -> bytes:
    """把 64 字节静态表摊平成 128 字节掩码，解密时只做取模。"""
    mask = bytearray(128)
    for i in range(128):
        index = i & 0x7F
        if index > 0x3F:
            index = (0x80 - index) & 0x3F
        mask[i] = _QMC_STATIC_KEYS[index]
    return bytes(mask)


_QMC_MASK = _qmc_make_mask()


def _tea_decrypt_block(block: bytes, key: int, rounds: int = 32,
                       delta: int = 0x9E3779B9) -> bytes:
    v0, v1 = struct.unpack(">2I", block)
    k0 = (key >> 96) & 0xFFFFFFFF
    k1 = (key >> 64) & 0xFFFFFFFF
    k2 = (key >> 32) & 0xFFFFFFFF
    k3 = key & 0xFFFFFFFF
    total = (delta * (rounds // 2)) & 0xFFFFFFFF
    for _ in range(rounds // 2):
        v1 = (v1 - ((((v0 << 4) & 0xFFFFFFFF) + k2) ^ ((v0 + total) & 0xFFFFFFFF) ^
                    (((v0 >> 5) + k3) & 0xFFFFFFFF))) & 0xFFFFFFFF
        v0 = (v0 - ((((v1 << 4) & 0xFFFFFFFF) + k0) ^ ((v1 + total) & 0xFFFFFFFF) ^
                    (((v1 >> 5) + k1) & 0xFFFFFFFF))) & 0xFFFFFFFF
        total = (total - delta) & 0xFFFFFFFF
    return struct.pack(">2I", v0, v1)


def _tea_decrypt_ecb(data: bytes, key: bytes, rounds: int = 32) -> bytes:
    """TEA-ECB，按 8 字节一块解密（腾讯 tc_tea 的底层）。"""
    if len(key) != 16:
        raise ValueError("TEA 密钥长度必须是 16 字节")
    if len(data) % 8:
        raise ValueError("TEA 数据长度不是 8 的整数倍")
    k = int.from_bytes(key, "big")
    return b"".join(_tea_decrypt_block(data[i:i + 8], k, rounds)
                    for i in range(0, len(data), 8))


def _tc_tea_decrypt(data: bytes, key: bytes) -> bytes:
    """腾讯 TarsCpp tc_tea（CBC + salt + 尾部零校验）解密。"""
    if len(data) < 16 or len(data) % 8:
        raise ValueError("tc_tea 数据长度不合法")
    first = _tea_decrypt_ecb(data[:8], key)
    pad_len = first[0] & 0x07
    out_len = len(data) - pad_len - 2 - 7 - 1
    if out_len < 0:
        raise ValueError("tc_tea 解密失败：长度推算为负")
    out = bytearray(out_len)
    iv_prev = bytearray(8)
    iv_cur = bytearray(data[:8])
    dest = bytearray(first)
    pos = 8
    idx = 1 + pad_len

    def crypt_block() -> None:
        nonlocal pos
        iv_prev[:] = iv_cur
        iv_cur[:] = data[pos:pos + 8]
        dest[:] = _tea_decrypt_ecb(bytes(x ^ y for x, y in zip(dest[:8], iv_cur)), key)
        pos += 8

    skipped = 0
    while skipped < 2:
        if idx < 8:
            idx += 1
            skipped += 1
        else:
            crypt_block()
            idx = 0
    out_pos = 0
    while out_pos < out_len:
        if idx < 8:
            out[out_pos] = dest[idx] ^ iv_prev[idx]
            idx += 1
            out_pos += 1
        else:
            crypt_block()
            idx = 0
    for _ in range(7):
        if idx == 8:
            crypt_block()
            idx = 0
        if dest[idx] ^ iv_prev[idx]:
            raise ValueError("tc_tea 解密失败：尾部零校验不通过")
        idx += 1
    return bytes(out)


def _qmc_v2_decode_key(raw_key: bytes) -> bytes:
    """还原 QMCv2 的 ekey：base64 → EncV2 解包 → TEA 解包。"""
    ekey = bytes(raw_key).strip().rstrip(b"\x00")
    try:
        decoded = base64.b64decode(ekey, validate=False)
    except Exception as exc:
        raise ValueError("QQ 音乐密钥不是合法的 base64") from exc
    if decoded.startswith(_QMC_ENC_V2):
        blob = decoded[len(_QMC_ENC_V2):]
        try:
            stage1 = _tc_tea_decrypt(blob, _QMC_V2_KEY1)
            stage2 = _tc_tea_decrypt(stage1, _QMC_V2_KEY2)
        except ValueError as exc:
            raise ValueError(f"QQ 音乐 EncV2 密钥解包失败：{exc}") from exc
        decoded = base64.b64decode(stage2)
    if len(decoded) < 16:
        raise ValueError("QQ 音乐密钥长度不足 16 字节")
    header, body = decoded[:8], decoded[8:]
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[i * 2] = _QMC_TEA_SEED[i]
        tea_key[i * 2 + 1] = header[i]
    return header + _tea_decrypt_ecb(body, bytes(tea_key))


class _QmcStaticCipher:
    """QMCv1：固定密钥表 + 文件偏移异或。"""

    name = "QQ 音乐 QMCv1"

    def decrypt(self, buf: bytearray, offset: int) -> None:
        mask = _QMC_MASK
        n = len(buf)
        base = offset % 0x7FFF
        for start in range(0, n, 128):
            chunk = buf[start:start + 128]
            for i in range(len(chunk)):
                p = base + start + i
                if p >= 0x7FFF:
                    p %= 0x7FFF
                chunk[i] ^= mask[p & 0x7F]
            buf[start:start + len(chunk)] = chunk


class _QmcMapCipher:
    """QMCv2 映射表：256/128 字节密钥 → 128 字节掩码。"""

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("QQ 音乐密钥为空")
        self._key = key
        self._n = len(key)
        self.name = f"QQ 音乐 QMCv2（映射表，{self._n} 字节密钥）"

    def decrypt(self, buf: bytearray, offset: int) -> None:
        key = self._key
        n = self._n
        for i in range(len(buf)):
            pos = offset + i
            if pos >= 0x7FFF:
                pos %= 0x7FFF
            idx = (pos * pos + 71214) % n
            value = key[idx]
            rot = ((idx & 7) + 4) & 7
            if rot:
                buf[i] ^= ((value << rot) & 0xFF) | (value >> (8 - rot))
            else:
                buf[i] ^= value


class _QmcRc4Cipher:
    """QMCv2 加固 RC4：前 128 字节按 key 查表，之后每 5120 字节一段做变体 RC4。"""

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("QQ 音乐密钥为空")
        self._key = key
        self._n = len(key)
        self.name = f"QQ 音乐 QMCv2（加固 RC4，{self._n} 字节密钥）"
        n = self._n
        box = bytearray((i % n) & 0xFF for i in range(n))
        j = 0
        for i in range(n):
            j = (j + box[i] + key[i]) % n
            box[i], box[j] = box[j], box[i]
        self._box = box
        base = 1
        for value in key:
            if value == 0:
                continue
            nxt = (base * value) & 0xFFFFFFFF
            if nxt == 0 or nxt <= base:
                break
            base = nxt
        self._hash = base
        self._skip_cache: dict = {}

    def _skip(self, seg_id: int) -> int:
        """第 seg_id 段要预先丢弃多少个密钥流字节。"""
        cached = self._skip_cache.get(seg_id)
        if cached is not None:
            return cached
        seed = self._key[seg_id % self._n]
        divisor = (seg_id + 1) * seed
        value = 0 if divisor == 0 else int(self._hash / divisor * 100)
        result = value % self._n
        if len(self._skip_cache) < 4096:
            self._skip_cache[seg_id] = result
        return result

    def _segment(self, buf, start, take, pos):
        """解一段（不超过 0x1400 字节）的其它段。"""
        n = self._n
        seg = pos // _QMC_OTHER_SEGMENT
        box = self._box[:]
        j = 0
        k = 0
        for _ in range(self._skip(seg) + pos % _QMC_OTHER_SEGMENT):
            j = (j + 1) % n
            k = (box[j] + k) % n
            box[j], box[k] = box[k], box[j]
        for i in range(take):
            j = (j + 1) % n
            k = (box[j] + k) % n
            box[j], box[k] = box[k], box[j]
            buf[start + i] ^= box[(box[j] + box[k]) % n]

    def decrypt(self, buf: bytearray, offset: int) -> None:
        n = len(buf)
        if n == 0:
            return
        start = 0
        pos = offset
        if pos < _QMC_FIRST_SEGMENT:
            take = min(n, _QMC_FIRST_SEGMENT - pos)
            key = self._key
            for i in range(take):
                buf[i] ^= key[self._skip(pos + i)]
            start = take
            pos += take
        while start < n:
            take = min(n - start, _QMC_OTHER_SEGMENT - pos % _QMC_OTHER_SEGMENT)
            self._segment(buf, start, take, pos)
            start += take
            pos += take


def _parse_qmc(path: Path):
    """解析 QMC 尾部密钥区，返回 (audio_end, ekey, tag)。"""
    size = path.stat().st_size
    tail = _read_tail(path, 65536)
    if len(tail) < 8:
        raise ValueError("QQ 音乐文件太小，缺少尾部密钥区")
    magic = tail[-4:]
    if magic in (_QMC_MAGIC_Q, _QMC_MAGIC_S):
        meta_size = struct.unpack_from(">I", tail, len(tail) - 8)[0]
        end_of_meta = size - 8
        ekey_start = end_of_meta - meta_size
        if ekey_start < 0 or ekey_start >= end_of_meta:
            raise ValueError("QQ 音乐文件损坏：尾部元数据长度不合法")
        with path.open("rb") as fp:
            fp.seek(ekey_start)
            blob = fp.read(end_of_meta - ekey_start)
        comma = blob.find(b",")
        if comma < 0:
            raise ValueError("QQ 音乐文件损坏：尾部找不到密钥结束符（逗号）")
        tag = "QTag" if magic == _QMC_MAGIC_Q else "STag"
        return ekey_start, blob[:comma], tag
    key_size = _u32le(tail, len(tail) - 4)
    if 0 < key_size <= 0x400:
        ekey_start = size - 4 - key_size
        if ekey_start < 0:
            raise ValueError("QQ 音乐文件损坏：尾部密钥长度超出文件大小")
        with path.open("rb") as fp:
            fp.seek(ekey_start)
            blob = fp.read(key_size)
        if len(blob) != key_size:
            raise ValueError("QQ 音乐文件损坏：尾部密钥不完整")
        return ekey_start, blob, ""
    if magic == _QMC_MAGIC_M:
        raise ValueError("这是 QQ 音乐新格式（musicex）：密钥不写在文件里，"
                         "需要登录客户端从服务器取回 ekey 才能解密")
    # 普通 QMCv1（.qmc0/.qmc3/.qmcflac/.qmcogg）没有尾部密钥块，整份文件都是加密音频。
    return size, b"", ""


def _make_qmc_cipher(ekey: bytes, tag: str):
    """tag 非空表示 QTag/STag 里带的 ekey（base64 文本，需要 TEA 解包）。

    tag 为空表示尾部直接存了原始密钥（QMCv2 v1 尾部格式），原样使用。
    """
    if not ekey and not tag:
        return _QmcStaticCipher()
    if tag:
        key = _qmc_v2_decode_key(ekey)
    else:
        key = bytes(ekey).strip().rstrip(b"\x00")
    if len(key) > 300:
        return _QmcRc4Cipher(key[16:])
    if len(key) > 128:
        return _QmcMapCipher(key[16:])
    if key:
        return _QmcMapCipher(key)
    raise ValueError("QQ 音乐密钥解析结果为空，无法解密")



# ---------------------------------------------------------------- 酷狗 KGM / KGMA / VPR

_KGM_MAGIC = bytes((0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
                    0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14))
_VPR_MAGIC = bytes((0x05, 0x28, 0xBC, 0x96, 0xE9, 0xE4, 0x5A, 0x43,
                    0x91, 0xAA, 0xBD, 0xD0, 0x7A, 0xF5, 0x36, 0x31))
_VPR_MASK_DIFF = bytes((0x25, 0xDF, 0xE8, 0xA6, 0x75, 0x1E, 0x75, 0x0E, 0x2F,
                        0x80, 0xF3, 0x2D, 0xB8, 0xB6, 0xE3, 0x11, 0x00))

_KGM_MASK_PRE = bytes.fromhex(
    "b8d53db2e9af788c8333715176a0cd37" "2f3e358da9be98b7e78c22ce5a61df68"
    "6989fea5b6dea977fcc8bdbde56d3e5a" "36ef694ebee1e9661cf3d902b6f2129b"
    "44d06fb93589b6466d73820669c1edd7" "85c230dfa262be792d62623d0d7ebe48"
    "892302a0e4d57551320253fd163a213b" "160fc3b2bbb3e2ba3a3d13ecf6014584"
    "a5700f93490c64cd31d5cc4c07019e00" "1a2390bf881e3baba63ec47347107e3b"
    "5ebce30084ff09d4e0890f5b58704ffb" "65d85c531bd3c8c6bfef98b0504f0fea"
    "e583588c282c8467cdd09e47db2750ca" "f46363e8977f1b4b0cc2c1214ccc58f5"
    "9452a3f3d3e068f40023f35e0a7b93dd" "ab12b213e884d7a79f0f324c551d0436"
    "52dc03f3f94e42e93d61ef7cb6b39350")
_KGM_TABLE1 = bytes.fromhex(
    "00000000000000000000000000000000" "0001210161012101e101210161012101"
    "d223020242420202c2c2020242420202" "d3d3020363436303e3c3e30363436303"
    "94b49465040404048484848404040404" "9595959504052505e585a585e5052505"
    "d6b696b6d6270606c6c68686c6c60606" "d7d79797d7d70607e7c7e787e7c7e707"
    "18381878183818e90808080808080808" "19191919191919190809290969092909"
    "da3a1a3a5a3a1a3ada2b0a0a4a4a0a0a" "dbdb1b1b5b5b1b1bdbdb0a0b6b4b6b0b"
    "9cbc9c7c1c3c1c7c9cbc9c6d0c0c0c0c" "9d9d9d9d1d1d1d1d9d9d9d9d0c0d2d0d"
    "debe9ebede3e1e3edebe9ebede2f0e0e" "dfdf9f9fdfdf1f1fdfdf9f9fdfdf0e0f"
    "00200060002000e000200060002000f1")
_KGM_TABLE2 = bytes.fromhex(
    "00000000000000000000000000000000" "0001230167012301ef01230167012301"
    "df21020246460202cece020246460202" "dede020365476503edcfed0365476503"
    "9dbf9d63040404048c8c8c8c04040404" "9c9c9c9c04052705eb8daf8deb052705"
    "dbbd9fbddb250606caca8e8ecaca0606" "dada9e9edada0607e9cbe98fe9cbe907"
    "193b197f193b19e70808080808080808" "181818181818181808092b096f092b09"
    "d7391b395f391b39d7290a0a4e4e0a0a" "d6d61a1a5e5e1a1ad6d60a0b6d4f6d0b"
    "95b7957b1d3f1d7b95b7956b0c0c0c0c" "949494941c1c1c1c949494940c0d2f0d"
    "d3b597b5d33d1f3dd3b597b5d32d0e0e" "d2d29696d2d21e1ed2d29696d2d20e0f"
    "00220066002200ee00220066002200fe")

# nib(x) = x ^ ((x & 0x0F) << 4) 是 0..255 上的双射（相当于每半个字节乘 2），
# 解密要用它的逆。
_KGM_NIBBLE = bytes((b ^ ((b & 0x0F) << 4)) & 0xFF for b in range(256))


def _build_inv_nib() -> bytes:
    inv = bytearray(256)
    for x in range(256):
        inv[_KGM_NIBBLE[x]] = x
    return bytes(inv)


_KGM_INV_NIB = _build_inv_nib()


def _kgm_get_mask(pos: int) -> int:
    offset = pos >> 4
    value = 0
    while offset >= 0x11:
        value ^= _KGM_TABLE1[offset % 272]
        offset >>= 4
        value ^= _KGM_TABLE2[offset % 272]
        offset >>= 4
    return _KGM_MASK_PRE[pos % 272] ^ value


class _KgmCipher:
    """酷狗 KGM/KGMA/VPR：文件里的 16 字节 slot key + 静态掩码表。

    逐字节：e = nib(key[pos % 17] ^ (v ^ vpr_diff[pos % 17])) ^ nib(mask(pos))，
    其中 nib(x) = x ^ ((x & 0x0F) << 4)，vpr_diff 只在 VPR 里有（KGM 时全为 0）。
    nib 不是按位异或意义下的线性函数，所以解密要整体反推：

        v = key[pos % 17] ^ inv_nib((e ^ vpr_diff[pos % 17]) ^ nib(mask(pos)))

    key 下标 17 一循环；mask(pos) 没有短周期，所以按 4096 字节一块先把掩码算好，
    再逐字节异或，避免每字节都调用 _kgm_get_mask。
    """

    _CHUNK = 4096

    def __init__(self, key: bytes, is_vpr: bool):
        if len(key) != 16:
            raise ValueError("酷狗密钥长度不是 16 字节")
        self._key = key
        self._vpr = is_vpr
        self.name = "酷狗 VPR" if is_vpr else "酷狗 KGM"
        self._diff = _VPR_MASK_DIFF if is_vpr else None

    def decrypt(self, buf: bytearray, offset: int) -> None:
        n = len(buf)
        if n == 0:
            return
        inv = _KGM_INV_NIB
        mask_of = _kgm_get_mask
        key_stream = self._key_stream_block()
        diff = self._diff
        done = 0
        while done < n:
            take = min(self._CHUNK, n - done)
            pos = offset + done
            for i in range(take):
                e = buf[done + i]
                if diff is not None:
                    e ^= diff[(pos + i) % 17]
                mask_nib = _KGM_NIBBLE[mask_of(pos + i)]
                key = key_stream[(pos + i) % 17]
                buf[done + i] = key ^ inv[(e ^ mask_nib) & 0xFF]
            done += take

    def _key_stream_block(self) -> bytes:
        return bytes(self._key) + b"\x00"   # key[16] 隐含为 0


# ---------------------------------------------------------------- KWM（酷我音乐）

_KWM_ROOT = b"MoOtOiTvINGwd2E6n0E1i7L5t2IoOoNk"


class _KwmCipher:
    """酷我 KWM：与 32 字节掩码循环异或。"""

    name = "酷我 KWM"

    def __init__(self, mask: bytes):
        if len(mask) != 0x20:
            raise ValueError("酷我掩码长度不是 32 字节")
        self._mask = mask

    def decrypt(self, buf: bytearray, offset: int) -> None:
        mask = self._mask
        for i in range(len(buf)):
            buf[i] ^= mask[(offset + i) & 0x1F]


def _parse_kwm(path: Path):
    """解析酷我头部，返回 (mask, audio_offset)。"""
    size = path.stat().st_size
    with path.open("rb") as fp:
        head = fp.read(0x400)
        if len(head) < 0x400:
            raise ValueError("酷我文件太小，头部不完整")
        if not head.startswith(b"yeelion"):
            raise ValueError("不是有效的酷我加密文件：文件头不是 yeelion")
        seed = _u32le(head, 0x10)
        text = str(seed).encode("ascii")[:0x20]
        mask = bytearray(0x20)
        mask[:len(text)] = text
        for i in range(0x20):
            mask[i] ^= _KWM_ROOT[i]
        audio_offset = 0x400
        if audio_offset >= size:
            raise ValueError("酷我文件损坏：没有音频数据")
    return bytes(mask), audio_offset


# ---------------------------------------------------------------- 对外接口

def _ext(path) -> str:
    try:
        name = os.fspath(path)
    except TypeError:
        name = str(path)
    return os.path.splitext(str(name))[1].lower()


def _magic_of(head: bytes) -> str:
    if head[:8] == _NCM_MAGIC:
        return "ncm"
    if len(head) >= 16 and head[:16] in (_KGM_MAGIC, _VPR_MAGIC):
        return "kgm"
    if head.startswith(b"yeelion"):
        return "kwm"
    return ""


def _tail_key_size(tail: bytes) -> int:
    """尾部密钥区探测：-1 表示带 QTag/STag，>0 表示 v1 尾部密钥长度。"""
    if len(tail) < 8:
        return 0
    if tail[-4:] in (_QMC_MAGIC_Q, _QMC_MAGIC_S):
        return -1
    size = _u32le(tail, len(tail) - 4)
    return size if 0 < size <= 0x400 else 0


def is_encrypted(path) -> bool:
    """按扩展名 + 文件头判断能不能处理这个文件。"""
    p = Path(path)
    if not p.is_file():
        return False
    if _ext(p) not in ENCRYPTED_EXTENSIONS:
        return False
    try:
        head = _read_head(p, 16)
        tail = _read_tail(p, 4096)
    except OSError:
        return False
    if _magic_of(head) or _magic_of(tail[-16:]):
        return True
    if _tail_key_size(tail) != 0:
        return True
    ext = _ext(p)
    return ext.startswith(".qmc") or ext == ".tkm"


def sniff(path) -> str:
    """返回 'ncm' / 'qmc' / 'kgm' / 'kwm' / ''（空串表示不是已知的加密格式）。"""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        head = _read_head(p, 16)
        tail = _read_tail(p, 4096)
    except OSError:
        return ""
    magic = _magic_of(head) or _magic_of(tail[-16:])
    if magic:
        return magic
    if _tail_key_size(tail) != 0:
        return "qmc"
    ext = _ext(p)
    if (ext.startswith(".qmc") or ext == ".tkm"
            or ext.startswith(".mf") or ext.startswith(".mgg")):
        return "qmc"
    return ""


_PLATFORM = {"ncm": "netease", "qmc": "qmc", "kgm": "kugou", "kwm": "kuwo"}


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.parent / f"{path.stem} ({i}){path.suffix}"
        if not candidate.exists():
            return candidate
    return path


def _notify(progress, done: int, total: int, note: str) -> None:
    if progress is None:
        return
    try:
        try:
            progress(done, total, note)
            return
        except TypeError:
            pass
        try:
            progress(done, total)
            return
        except TypeError:
            pass
        progress(done / total if total else 0.0)
    except Exception:
        pass


def _decode_stream(src: Path, cipher, start: int, total: int, out_path: Path,
                   progress, note: str) -> tuple:
    """流式解密：边读边解边写，返回 (真实格式, 写入字节数)。"""
    fmt = ""
    written = 0
    with src.open("rb") as fp, out_path.open("wb") as out:
        fp.seek(start)
        left = total
        while left > 0:
            chunk = fp.read(min(_CHUNK, left))
            if not chunk:
                raise ValueError("文件损坏：音频数据提前结束")
            view = bytearray(chunk)
            cipher.decrypt(view, written)
            out.write(view)
            if not fmt:
                fmt = _probe_format(bytes(view[:64]))
                if not fmt:
                    raise ValueError("解密后的数据不是已知音频格式（flac/mp3/ogg/m4a），"
                                     "密钥不匹配，已放弃输出")
            written += len(view)
            left -= len(view)
            _notify(progress, written, total, note)
    return fmt, written


def decrypt(path, dest_dir=None, progress=None) -> dict:
    """就地解密一个平台加密音频。

    返回 {'ok': True, 'output': Path, 'format': str, 'meta': dict,
          'platform': str, 'note': str}；失败抛 ValueError（消息为人话）。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"文件不存在：{src}")
    platform = sniff(src)
    if not platform:
        raise ValueError("不是本模块能识别的加密音频文件（支持 .ncm / .qmc* / .mflac / "
                         ".mgg / .kgm / .kgma / .vpr / .kwm）")
    size = src.stat().st_size
    meta: dict = {}
    hint = ""

    if platform == "ncm":
        box, raw_meta, cover, start = _parse_ncm(src)
        meta = _ncm_meta_pick(raw_meta, cover)
        cipher = _NcmCipher(box)
        end = size
        hint = str(meta.get("format") or "").lower()
    elif platform == "qmc":
        end, ekey, tag = _parse_qmc(src)
        cipher = _make_qmc_cipher(ekey, tag)
        start = 0
    elif platform == "kgm":
        with src.open("rb") as fp:
            head = fp.read(0x2C)
        if len(head) < 0x2C:
            raise ValueError("酷狗文件太小，头部不完整")
        is_vpr = head[:16] == _VPR_MAGIC
        if not is_vpr and head[:16] != _KGM_MAGIC:
            raise ValueError("不是有效的酷狗加密文件：文件头不匹配")
        header_len = _u32le(head, 0x10)
        if header_len < 0x2C or header_len > size:
            raise ValueError(f"酷狗文件损坏：头部长度不合法（{header_len}）")
        cipher = _KgmCipher(head[0x1C:0x2C], is_vpr)
        start = header_len
        end = size
    else:  # kwm
        mask, start = _parse_kwm(src)
        cipher = _KwmCipher(mask)
        end = size

    total = max(0, end - start)
    out_dir = Path(dest_dir) if dest_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = _unique_path(out_dir / (src.stem + ".vgpart"))
    try:
        fmt, written = _decode_stream(src, cipher, start, total,
                                      tmp_path, progress, cipher.name)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if hint not in _FORMAT_EXT or hint != fmt:
        hint = ""
    final = _unique_path(out_dir / f"{src.stem}{_FORMAT_EXT.get(hint) or _FORMAT_EXT[fmt]}")
    tmp_path.replace(final)
    if not meta.get("format"):
        meta["format"] = fmt
    note = f"{cipher.name} 已还原为 {fmt.upper()}，共 {written} 字节"
    _notify(progress, written, written, "完成")
    return {
        "ok": True,
        "output": final,
        "format": fmt,
        "meta": meta,
        "platform": _PLATFORM[platform],
        "note": note,
    }
